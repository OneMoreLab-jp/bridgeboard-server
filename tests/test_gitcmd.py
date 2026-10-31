"""bblist/gitcmd.py（git を呼ぶ共通の口）の試験。python3 -m unittest discover -s tests -t . -p 'test_gitcmd.py' -v

sessions.run_git と gitignore.run_check_ignore の両方が、この口を通ることを確かめる。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bblist import gitcmd
from bblist.gitignore import run_check_ignore
from bblist.sessions import run_git
from tests.test_gitignore import init_repo, isolated_git_env


class GitCmdTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        patcher = mock.patch.dict(os.environ, isolated_git_env(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = init_repo(self.tmp / "repo", "*.log\n")

    def test_environment_keeps_no_inherited_git_variable(self):
        inherited = {"GIT_DIR": "/elsewhere", "GIT_WORK_TREE": "/elsewhere", "GIT_INDEX_FILE": "/elsewhere/i",
                     "GIT_CONFIG_GLOBAL": "/elsewhere/c", "GIT_CONFIG_COUNT": "1", "GIT_EXTERNAL_DIFF": "/bin/false",
                     "GIT_SSH_COMMAND": "x", "GIT_OPTIONAL_LOCKS": "1", "GIT_CEILING_DIRECTORIES": "/elsewhere",
                     "GIT_ANYTHING_ELSE": "x", "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, inherited):
            env = gitcmd.git_env()
            with_ceiling = gitcmd.git_env(["/a", "/b"])
        self.assertEqual({k: v for k, v in env.items() if k.startswith("GIT_")}, {"GIT_OPTIONAL_LOCKS": "0"})
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["PATH"], inherited["PATH"])                  # GIT_ 以外は引き継ぐ
        self.assertEqual(with_ceiling["GIT_CEILING_DIRECTORIES"], "/a:/b")
        self.assertEqual(sorted(k for k in with_ceiling if k.startswith("GIT_")),
                         ["GIT_CEILING_DIRECTORIES", "GIT_OPTIONAL_LOCKS"])

    def test_both_callers_use_the_same_hardening(self):
        done = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch.dict(os.environ, {"GIT_DIR": "/elsewhere", "GIT_CONFIG_GLOBAL": "/elsewhere"}), \
                mock.patch("bblist.gitcmd.subprocess.run", return_value=done) as run:
            run_git(str(self.repo))
            run_check_ignore(self.repo, ["a.log"])
        rev_parse, check_ignore = run.call_args_list
        for call in (rev_parse, check_ignore):
            args, kwargs = call[0][0], call[1]
            self.assertEqual(args[:4], ["git", "-c", "core.fsmonitor=false", "-C"], args)
            self.assertEqual(args[4], str(self.repo))
            self.assertTrue(kwargs["capture_output"])
            self.assertGreater(kwargs["timeout"], 0)
            env = kwargs["env"]
            self.assertEqual({k for k in env if k.startswith("GIT_")}, {"GIT_OPTIONAL_LOCKS"})
            self.assertEqual((env["GIT_OPTIONAL_LOCKS"], env["LC_ALL"]), ("0", "C"))
            self.assertNotIn("shell", kwargs)
        self.assertEqual(rev_parse[0][0][5], "rev-parse")
        self.assertEqual(check_ignore[0][0][5], "check-ignore")
        self.assertIs(rev_parse[1]["stdin"], subprocess.DEVNULL)          # 名前を渡さない呼び出しは stdin を閉じる
        self.assertNotIn("input", rev_parse[1])
        self.assertNotIn("stdin", check_ignore[1])                        # 渡すほうは input（管）
        self.assertEqual(check_ignore[1]["input"], b"./a.log\0")

    def test_inherited_git_variables_do_not_redirect_the_real_calls(self):
        # 本物の git で：継承した GIT_DIR が向き先を変えようとしても、-C のフォルダを調べる
        other = init_repo(self.tmp / "other", "*.md\n")
        (self.repo / "a.log").write_text("x", encoding="utf-8")
        (self.repo / "b.md").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            self.assertEqual(run_git(str(self.repo))[0], str(self.repo))
            self.assertEqual(run_check_ignore(self.repo, ["a.log", "b.md"], self.repo), {"a.log"})

    def test_run_passes_input_and_closes_stdin_otherwise(self):
        r = gitcmd.run(["rev-parse", "--is-inside-work-tree"], self.repo, timeout=5)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, b"true"))
        r = gitcmd.run(["check-ignore", "--stdin", "-z"], self.repo, timeout=5, input=b"./x.log\0")
        self.assertEqual((r.returncode, r.stdout), (0, b"./x.log\0"))


if __name__ == "__main__":
    unittest.main()
