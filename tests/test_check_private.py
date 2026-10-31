"""tools/check_private.sh の試験（公開の前の関所。見逃さないこと・調べきれなければ 2 で終わることが肝）。
python3 -m unittest discover -s tests -t . -p 'test_check_private.py' -v

使い捨ての git リポジトリを作り、個人の文字列のファイルは環境変数 BRIDGEBOARD_PRIVATE_STRINGS で tmp のものに向ける
（本物の ~/.appstoreconnect/private-strings.txt は読まない）。内蔵の印はこのファイル自身に当たらないよう、割って書く。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "tools" / "check_private.sh"

# 内蔵の印の例（check_private.sh の BUILTIN と同じもの。このファイル自身に当たらないよう割って書く）
BUILTINS = ["100.80" + ".1.2", "macbook" + "m1max", "taile" + "870a1", "/Users/" + "tomoaki/x", "21_One" + "MoreLab"]
# 濁点のあるかな（NFC と NFD で綴りが違う）の、架空の合言葉
SECRET = "ゲームバグの合言葉"
SECRET_NFC = unicodedata.normalize("NFC", SECRET)
SECRET_NFD = unicodedata.normalize("NFD", SECRET)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null"]


class CheckPrivateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.private = self.tmp / "private.txt"
        self.private.write_text(SECRET_NFC + "\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        (self.repo / "clean.txt").write_text("nothing to see here\n", encoding="utf-8")
        self.commit("clean")

    # ---- 道具

    def git(self, *args: str) -> None:
        subprocess.run([*GIT, *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def fake_tool(self, name: str, fail_when: str | None = None) -> Path:
        """PATH の先頭に置く偽の道具の置き場。name は呼ばれると 3 で失敗する。
        name が git のときは、fail_when（git -C <dir> <ここ> …）のサブコマンドだけ失敗し、ほかは本物の git に渡す。"""
        bin_dir = self.tmp / f"fake-{name}-{fail_when}"
        bin_dir.mkdir()
        tool = bin_dir / name
        if name == "git":
            real = shutil.which("git")
            tool.write_text(f'#!/bin/sh\nif [ "$3" = "{fail_when}" ]; then echo "fake failure" >&2; exit 3; fi\n'
                            f'exec "{real}" "$@"\n', encoding="utf-8")
        else:
            tool.write_text('#!/bin/sh\necho "fake failure" >&2\nexit 3\n', encoding="utf-8")
        tool.chmod(0o755)
        return bin_dir

    def check(self, *args: str, target=None, private="default", path_first=None, env_extra=None) -> subprocess.CompletedProcess:
        env = {**os.environ, **(env_extra or {})}
        if path_first is not None:
            env["PATH"] = f"{path_first}:{env['PATH']}"
        if private == "default":
            env["BRIDGEBOARD_PRIVATE_STRINGS"] = str(self.private)
        else:
            env["BRIDGEBOARD_PRIVATE_STRINGS"] = str(private)
        return subprocess.run(["/bin/bash", str(CHECK), *args, str(target or self.repo)], env=env,
                              capture_output=True, text=True, timeout=120)

    def assert_exit(self, r: subprocess.CompletedProcess, code: int) -> None:
        self.assertEqual(r.returncode, code, f"stdout={r.stdout!r} stderr={r.stderr!r}")

    def assert_nothing_printed(self, r: subprocess.CompletedProcess, *texts: str) -> None:
        out = unicodedata.normalize("NFC", r.stdout + r.stderr)
        for t in texts:
            self.assertNotIn(unicodedata.normalize("NFC", t), out, "一致した中身が出た")
            self.assertNotIn(t, r.stdout + r.stderr, "一致した中身が出た")

    # ---- 見つける

    def test_clean_repo_passes(self):
        r = self.check()
        self.assert_exit(r, 0)
        self.assertIn("ok", r.stdout)
        self.assertNotIn("not checked", r.stdout)

    def test_blank_lines_and_crlf_in_the_list_do_not_match_everything(self):
        self.private.write_bytes(b"\n\n   \n" + SECRET_NFC.encode("utf-8") + b"\r\n\r\n")
        self.assert_exit(self.check(), 0)                       # 空のパターンは全部に当たる。除かれていること
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        self.assert_exit(self.check(), 1)                       # CRLF でも当たる

    def test_planted_builtin_hit_exits_1(self):
        for s in BUILTINS:
            with self.subTest(s=s):
                leak = self.repo / "leak.txt"
                leak.write_text(f"see {s} here\n", encoding="utf-8")
                r = self.check()
                self.assert_exit(r, 1)
                self.assertIn("leak.txt:1", r.stdout)
                self.assert_nothing_printed(r, s)
                leak.unlink()
        self.assert_exit(self.check(), 0)

    def test_private_hit_is_found_in_either_unicode_form(self):
        for list_form in (SECRET_NFC, SECRET_NFD):
            for text_form in (SECRET_NFC, SECRET_NFD):
                with self.subTest(list=list_form == SECRET_NFC, text=text_form == SECRET_NFC):
                    self.private.write_text(list_form + "\n", encoding="utf-8")
                    leak = self.repo / "leak.txt"
                    leak.write_text(f"x {text_form} y\n", encoding="utf-8")
                    r = self.check()
                    self.assert_exit(r, 1)                     # NFC のリストで NFD の本文も、その逆も見つける
                    self.assertIn("leak.txt:1", r.stdout)
                    self.assert_nothing_printed(r, SECRET_NFC, SECRET_NFD)
                    leak.unlink()

    def test_private_hit_only_in_history_exits_1(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFD} y\n", encoding="utf-8")
        self.commit("add")
        (self.repo / "leak.txt").unlink()
        self.commit("remove")
        r = self.check()
        self.assert_exit(r, 1)                                  # 作業コピーはきれいでも、履歴にあれば見つける
        self.assertIn("leak.txt:1", r.stdout)
        self.assert_nothing_printed(r, SECRET_NFC, SECRET_NFD)

    def test_hit_in_a_commit_message_exits_1(self):
        self.commit(f"note {SECRET_NFD}")
        r = self.check()
        self.assert_exit(r, 1)
        self.assertIn("git history", r.stdout)
        self.assert_nothing_printed(r, SECRET_NFC, SECRET_NFD)
        self.commit("later")                                    # 後のコミットで直しても、履歴に残る
        self.assert_exit(self.check(), 1)

    def test_old_revisions_are_scanned_even_when_there_are_many(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        self.commit("add")
        (self.repo / "leak.txt").unlink()
        self.commit("remove")
        for i in range(120):                                    # 50 版ずつ渡す束が、3つ以上になる
            self.commit(f"n{i}")
        self.assert_exit(self.check(), 1)

    def test_not_a_git_directory_is_checked_without_history(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("clean\n", encoding="utf-8")
        r = self.check(target=plain)
        self.assert_exit(r, 0)
        self.assertIn("no .git", r.stderr)
        self.assertIn("no git history", r.stdout)                # 履歴を調べていないことを、成功の文でも言う
        (plain / "a.txt").write_text(f"{SECRET_NFD}\n", encoding="utf-8")
        self.assert_exit(self.check(target=plain), 1)

    # ---- ファイル名（パス）も照合する

    def test_a_builtin_mark_in_a_file_name_exits_1_and_is_not_printed(self):
        for s in (b for b in BUILTINS if "/" not in b):          # ファイル名に使えるもの
            with self.subTest(s=s):
                leak = self.repo / f"notes-{s}.txt"
                leak.write_text("clean content\n", encoding="utf-8")   # 中身はきれい。名前だけが当たる
                r = self.check()
                self.assert_exit(r, 1)
                self.assertIn("file name", r.stdout)
                self.assert_nothing_printed(r, s)                  # パスそのものに文字列が入っているので、パスは出さない
                leak.unlink()
        self.assert_exit(self.check(), 0)

    def test_a_private_string_in_a_file_or_folder_name_is_found_in_either_unicode_form(self):
        for list_form in (SECRET_NFC, SECRET_NFD):
            for name_form in (SECRET_NFC, SECRET_NFD):
                self.private.write_text(list_form + "\n", encoding="utf-8")
                folder = self.repo / f"d-{name_form}"
                folder.mkdir()
                (folder / "inner.txt").write_text("clean\n", encoding="utf-8")   # 当たるのは、フォルダの名前だけ
                with self.subTest(kind="folder", list=list_form == SECRET_NFC, name=name_form == SECRET_NFC):
                    r = self.check()
                    self.assert_exit(r, 1)
                    self.assertIn("file name", r.stdout)
                    self.assert_nothing_printed(r, SECRET_NFC, SECRET_NFD)
                shutil.rmtree(folder)
                single = self.repo / f"f-{name_form}.txt"
                single.write_text("clean\n", encoding="utf-8")
                with self.subTest(kind="file", list=list_form == SECRET_NFC, name=name_form == SECRET_NFC):
                    self.assert_exit(self.check(), 1)
                single.unlink()
        self.assert_exit(self.check(), 0)

    def test_a_file_name_in_a_plain_folder_is_found_too(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / f"{SECRET_NFC}.txt").write_text("clean\n", encoding="utf-8")
        self.assert_exit(self.check(target=plain), 1)

    def test_names_inside_dotgit_and_pycache_are_not_working_tree_names(self):
        # .git の中の名前（ブランチ名の ref ファイルなど）と __pycache__ は、作業コピーの名前に数えない（ほかの検査と同じ）
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / f"{SECRET_NFC}.pyc").write_text("x\n", encoding="utf-8")
        self.assert_exit(self.check(), 0)

    def test_history_file_names_are_checked_with_the_history_flag(self):
        (self.repo / f"x-{SECRET_NFC}.txt").write_text("clean content\n", encoding="utf-8")
        self.commit("add")
        (self.repo / f"x-{SECRET_NFC}.txt").unlink()
        self.commit("remove")
        r = self.check()
        self.assert_exit(r, 0)                                  # 旗なしでは、履歴のファイル名は調べない
        self.assertIn("--history", r.stdout)                    # 調べていないことを、成功の文で言う
        r = self.check("--history")
        self.assert_exit(r, 1)                                  # 作業コピーはきれいでも、履歴のどこかにあれば見つける
        self.assertIn("git history", r.stdout)
        self.assertIn("file name", r.stdout)
        self.assert_nothing_printed(r, SECRET_NFC, SECRET_NFD)

    def test_a_renamed_file_keeps_its_old_name_in_the_history_check(self):
        old = self.repo / f"old-{BUILTINS[1]}.txt"
        old.write_text("clean content\n", encoding="utf-8")
        self.commit("add")
        old.rename(self.repo / "new.txt")
        self.commit("rename")
        self.assert_exit(self.check("--history"), 1)            # 改名の前の名前も、全部の版のファイル名のうち
        self.assert_exit(self.check(), 0)

    def test_history_flag_on_a_clean_repository_is_ok_without_a_skip_note(self):
        r = self.check("--history")
        self.assert_exit(r, 0)
        self.assertIn("ok", r.stdout)
        self.assertNotIn("skipped", r.stdout)
        r = self.check("--history", "--builtin-only", private=self.tmp / "nope.txt")
        self.assert_exit(r, 0)                                  # 旗はどの順でも・--builtin-only とも組める
        self.assertIn("personal list not checked", r.stdout)

    def test_history_flag_without_a_git_history_says_there_is_nothing_to_check(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("clean\n", encoding="utf-8")
        r = self.check("--history", target=plain)
        self.assert_exit(r, 0)
        self.assertIn("no git history", r.stdout)

    def test_a_failing_file_name_listing_exits_2(self):
        # 名前の一覧を取る git log が失敗したら、きれいとは言わない（中身を調べる別の git log は通るようにしてある）
        real = shutil.which("git")
        bin_dir = self.tmp / "fake-git-names"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(f'#!/bin/sh\ncase "$*" in *--name-only*) echo "fake failure" >&2; exit 3;; esac\n'
                                     f'exec "{real}" "$@"\n', encoding="utf-8")
        (bin_dir / "git").chmod(0o755)
        r = self.check("--history", path_first=bin_dir)
        self.assert_exit(r, 2)
        self.assertIn("ERROR", r.stderr)
        self.assertNotIn("ok", r.stdout)
        self.assert_exit(self.check(path_first=bin_dir), 0)     # 旗なしなら、その git log は呼ばない

    # ---- 個人の文字列のファイルが使えないときは、黙って通さない

    def test_missing_empty_blank_or_directory_list_exits_2(self):
        empty = self.tmp / "empty.txt"
        empty.write_bytes(b"")
        blank = self.tmp / "blank.txt"
        blank.write_text("\n  \n\r\n", encoding="utf-8")
        directory = self.tmp / "dir"
        directory.mkdir()
        for name, path in {"missing": self.tmp / "nope.txt", "empty": empty, "blank": blank, "directory": directory}.items():
            with self.subTest(name):
                r = self.check(private=path)
                self.assert_exit(r, 2)
                self.assertIn("NOT checked", r.stderr)
                self.assertNotIn("ok", r.stdout)               # 「見つからなかった」と言わない

    def test_unreadable_list_exits_2(self):
        self.private.chmod(0)
        self.addCleanup(self.private.chmod, 0o600)
        if os.access(self.private, os.R_OK):
            self.skipTest("読めてしまう（root）")
        r = self.check()
        self.assert_exit(r, 2)
        self.assertIn("NOT checked", r.stderr)
        self.assertNotIn("ok", r.stdout)

    def test_missing_list_is_not_hidden_by_a_builtin_hit(self):
        (self.repo / "leak.txt").write_text("host " + BUILTINS[1] + "\n", encoding="utf-8")
        r = self.check(private=self.tmp / "nope.txt")
        self.assert_exit(r, 2)                                  # 個人のファイルが使えない時点で止まり、0 にも「見つからない」にもしない
        self.assertIn("NOT checked", r.stderr)

    def test_builtin_only_runs_without_the_list_and_says_so(self):
        r = self.check("--builtin-only", private=self.tmp / "nope.txt")
        self.assert_exit(r, 0)
        self.assertIn("personal list not checked", r.stdout)
        (self.repo / "leak.txt").write_text("host " + BUILTINS[1] + "\n", encoding="utf-8")
        self.assert_exit(self.check("--builtin-only", private=self.tmp / "nope.txt"), 1)

    def test_builtin_only_does_not_look_at_the_list(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        r = self.check("--builtin-only")                        # 個人の文字列はあるが、見ない（成功の文で、見ていないと言う）
        self.assert_exit(r, 0)
        self.assertIn("personal list not checked", r.stdout)

    # ---- 失敗を飲み込まない

    def test_unreadable_file_in_the_tree_exits_2(self):
        locked = self.repo / "locked.txt"
        locked.write_text("clean\n", encoding="utf-8")
        self.commit("add locked")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if os.access(locked, os.R_OK):
            self.skipTest("読めてしまう（root）")
        r = self.check()
        self.assert_exit(r, 2)                                  # 読めなかったファイルがあるのに、きれいとは言わない
        self.assertIn("ERROR", r.stderr)
        self.assertNotIn("ok", r.stdout)

    def test_corrupted_head_exits_2(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        self.commit("add")
        (self.repo / "leak.txt").unlink()
        self.commit("remove")
        self.assert_exit(self.check(), 1)                       # 壊す前は、履歴の中の文字列を見つける
        (self.repo / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
        r = self.check()
        self.assert_exit(r, 2)                                  # git が読めないのに「git ではないフォルダ」とは言わない
        self.assertIn("NOT checked", r.stderr)
        self.assertNotIn("ok", r.stdout)

    def test_dotgit_that_points_nowhere_exits_2(self):
        shutil.rmtree(self.repo / ".git")
        for name, make in {"gitfile": lambda: (self.repo / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8"),
                           "symlink": lambda: (self.repo / ".git").symlink_to(self.tmp / "no-such-gitdir")}.items():
            with self.subTest(name):
                make()
                r = self.check()
                self.assert_exit(r, 2)
                self.assertIn("NOT checked", r.stderr)
                self.assertNotIn("ok", r.stdout)
                (self.repo / ".git").unlink()

    def test_broken_git_does_not_fall_back_to_the_parent_repository(self):
        outer = self.tmp / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        for d in (outer, inner):
            subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        (inner / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        for d in (inner, outer):
            subprocess.run([*GIT, "add", "-A"], cwd=d, check=True, capture_output=True)
            subprocess.run([*GIT, "commit", "-q", "-m", "c"], cwd=d, check=True, capture_output=True)
        (inner / "leak.txt").unlink()                           # 作業コピーはきれい。文字列は inner の履歴にだけある
        subprocess.run([*GIT, "add", "-A"], cwd=inner, check=True, capture_output=True)
        subprocess.run([*GIT, "commit", "-q", "-m", "remove"], cwd=inner, check=True, capture_output=True)
        self.assert_exit(self.check(target=inner), 1)
        (inner / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")   # 親の outer は無事
        self.assert_exit(self.check(target=inner), 2)           # 親の履歴（きれい）を読んで 0 にしない

    def test_git_env_cannot_redirect_the_history_check(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFD} y\n", encoding="utf-8")
        self.commit("add")
        (self.repo / "leak.txt").unlink()
        self.commit("remove")
        other = self.tmp / "other"
        other.mkdir()
        subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=other, check=True, capture_output=True)
        subprocess.run([*GIT, "commit", "-q", "--allow-empty", "-m", "clean"], cwd=other, check=True, capture_output=True)
        r = self.check(env_extra={"GIT_DIR": str(other / ".git")})   # hook の中のように GIT_DIR が付いていても
        self.assert_exit(r, 1)

    def test_a_failing_git_or_tool_exits_2_not_a_clean_result(self):
        cases = [("git", "rev-list"), ("git", "log"), ("git", "for-each-ref"), ("git", "grep"), ("split", None)]
        for name, sub in cases:
            with self.subTest(name=name, sub=sub):
                r = self.check(path_first=self.fake_tool(name, sub))
                self.assert_exit(r, 2)
                self.assertIn("ERROR", r.stderr)
                self.assertNotIn("ok", r.stdout)
        (self.repo / "leak.txt").write_text("host " + BUILTINS[1] + "\n", encoding="utf-8")
        for name in ("awk", "sed"):                              # 出力を整える道具が壊れても、通さない
            with self.subTest(name=name):
                r = self.check(path_first=self.fake_tool(name))
                self.assert_exit(r, 2)
                self.assertNotIn("ok", r.stdout)

    # ---- 調べるフォルダの渡し方・リストの書き方

    def test_symlinked_directory_is_scanned(self):
        link = self.tmp / "link"
        link.symlink_to(self.repo)
        self.assert_exit(self.check(target=link), 0)
        (self.repo / "leak.txt").write_text("host " + BUILTINS[1] + "\n", encoding="utf-8")
        r = self.check(target=link)                              # 末尾の / なしのリンクでも、中を読む
        self.assert_exit(r, 1)
        self.assertIn("leak.txt:1", r.stdout)
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text(f"{SECRET_NFD}\n", encoding="utf-8")
        plain_link = self.tmp / "plain-link"
        plain_link.symlink_to(plain)
        self.assert_exit(self.check(target=plain_link), 1)

    def test_bom_and_crlf_in_the_list_still_match(self):
        (self.repo / "leak.txt").write_text(f"x {SECRET_NFC} y\n", encoding="utf-8")
        bom = b"\xef\xbb\xbf"
        for name, raw in {"bom first line": bom + SECRET_NFC.encode("utf-8") + b"\n",
                          "bom + crlf": bom + SECRET_NFC.encode("utf-8") + b"\r\n",
                          "bom on a later line": b"other\n" + bom + SECRET_NFC.encode("utf-8") + b"\n"}.items():
            with self.subTest(name):
                self.private.write_bytes(raw)
                self.assert_exit(self.check(), 1)                # 先頭の BOM で、その行が一生当たらなくならない

    def test_utf16_list_exits_2(self):
        self.private.write_bytes(SECRET_NFC.encode("utf-16"))    # バイトで探しても当たらない文字列になる
        r = self.check()
        self.assert_exit(r, 2)
        self.assertIn("NOT checked", r.stderr)

    def test_usage_errors_exit_64(self):
        env = {**os.environ, "BRIDGEBOARD_PRIVATE_STRINGS": str(self.private)}
        for args in ([], [str(self.tmp / "no-such-dir")], ["--bogus", str(self.repo)], [str(self.repo), str(self.repo)]):
            with self.subTest(args=args):
                r = subprocess.run(["/bin/bash", str(CHECK), *args], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 64, r.stderr)


if __name__ == "__main__":
    unittest.main()
