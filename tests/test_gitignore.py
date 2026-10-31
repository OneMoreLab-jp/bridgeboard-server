"""bblist/gitignore.py の試験。python3 -m unittest discover -s tests -t . -p 'test_gitignore.py' -v

git の答えは、本物の git を tmp に作って確かめる（無視の規則・追跡済みのファイル・サブモジュール・NFD の綴り）。
呼ぶ回数・失敗の扱いは偽物で確かめる。利用者の git の設定（~/.gitconfig・全体の無視の一覧）が答えを変えないよう、
環境変数で外す（isolated_git_env）。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from bblist.gitignore import GitIgnore, run_check_ignore


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def isolated_git_env(tmp: Path) -> dict:
    """利用者の ~/.gitconfig・全体の無視の一覧（~/.config/git/ignore）を読まない環境変数。
    サーバーの git の呼び出しは GIT_ で始まる環境変数を渡さない（bblist/gitcmd.py）ので、GIT_CONFIG_GLOBAL では外せない。
    HOME と XDG_CONFIG_HOME を空のフォルダに向ける。"""
    home, xdg = tmp / "home", tmp / "xdg"
    home.mkdir(exist_ok=True)
    xdg.mkdir(exist_ok=True)
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}


def git(cwd, *args) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def init_repo(path: Path, ignore: str = "") -> Path:
    """git の作業コピー。.gitignore の中身を渡せる（コミットはしない：無視の判定は索引と規則だけで足りる）。"""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("core.hooksPath", str(path / "no-hooks"))):
        git(path, "config", k, v)
    if ignore:
        (path / ".gitignore").write_text(ignore, encoding="utf-8")
    return path


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class GitCase(unittest.TestCase):
    """tmp と、利用者の git の設定を外した環境変数。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        patcher = mock.patch.dict(os.environ, isolated_git_env(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "proj"


class Counting:
    """git の偽物 git(フォルダ, 名前の並び, 起点)。呼ばれた (フォルダ, 名前の並び) と起点を覚え、answer が返す。"""

    def __init__(self, answer=lambda directory, names: set()):
        self.calls = []
        self.roots = []
        self.answer = answer

    def __call__(self, directory, names, root=None):
        self.calls.append((Path(directory), list(names)))
        self.roots.append(root)
        return self.answer(directory, names)


class IgnoredTest(GitCase):
    def test_gitignored_hidden_by_default(self):
        init_repo(self.root, "out/\n*.log\nsecret/\n")
        for rel in ("out/a.html", "out/sub/b.html", "src/k.txt", "top.log", "secret/s.txt", "docs/x.md"):
            touch(self.root / rel)
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(self.root, ["out", "src", "top.log", "secret", "docs", ".gitignore"]),
                         {"out", "top.log", "secret"})
        # 無視されたフォルダの下は、中の1つ1つも無視（フォルダの下の下まで）
        self.assertEqual(g.ignored(self.root, ["out/a.html", "out/sub", "out/sub/b.html", "src/k.txt", "secret/s.txt",
                                               "docs/x.md"]),
                         {"out/a.html", "out/sub", "out/sub/b.html", "secret/s.txt"})
        self.assertIs(g.available, True)

    def test_gitignored_shown_when_flag_on(self):
        init_repo(self.root, "out/\n")
        touch(self.root / "out/a.html")
        fake = Counting(lambda d, n: set(n))
        g = GitIgnore(show_ignored=True, git=fake)
        self.assertEqual(g.ignored(self.root, ["out", "out/a.html"]), set())
        self.assertEqual(fake.calls, [])                       # 返すと決めたなら、git も呼ばない
        self.assertIs(g.available, True)
        self.assertEqual(GitIgnore(show_ignored=True).ignored(self.root, ["out"]), set())   # 本物の git でも同じ

    def test_non_git_folder_unaffected(self):
        touch(self.root / ".gitignore", "*\n")                 # git でないので、この規則は効かない
        touch(self.root / "a.md")
        fake = Counting(lambda d, n: set(n))
        g = GitIgnore(show_ignored=False, git=fake)
        self.assertEqual(g.ignored(self.root, [".gitignore", "a.md"]), set())
        self.assertEqual(fake.calls, [])
        self.assertEqual(GitIgnore(show_ignored=False).ignored(self.root, ["a.md"]), set())
        self.assertIs(g.available, True)

    def test_non_git_folder_inside_a_repo_is_unaffected(self):
        # 上のフォルダが git で「全部無視」でも、起点が git でなければ効かない（ホームを dotfiles の git にしている人）
        outer = init_repo(self.tmp / "outer", "/*\n")
        touch(outer / "inner" / "a.md")
        self.assertEqual(GitIgnore(show_ignored=False).ignored(outer / "inner", ["a.md"]), set())
        self.assertEqual(GitIgnore(show_ignored=False).ignored(outer, ["inner"]), {"inner"})   # 対照：git の起点には効く

    def test_broken_dot_git_is_not_a_failure(self):
        (self.root / ".git").mkdir(parents=True)               # .git があるが git の作業コピーではない
        touch(self.root / ".git" / "config", "[core]\n")
        touch(self.root / "a.md")
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(self.root, ["a.md"]), set())
        self.assertIs(g.available, True)

    def test_gitignore_unavailable_shows_and_reports(self):
        init_repo(self.root, "out/\n")
        touch(self.root / "out/a.html")
        fail = Counting(lambda d, n: None)                     # git の実行が失敗した
        g = GitIgnore(show_ignored=False, git=fail)
        self.assertIs(g.available, True)                       # まだ試していない
        with self.assertLogs("bblist.gitignore", "WARNING") as logs:
            self.assertEqual(g.ignored(self.root, ["out"]), set())            # 隠さない
            self.assertIs(g.available, False)
            self.assertEqual(g.ignored(self.root, ["out", "out/a.html"]), set())   # 失敗は覚えない：また試す
        self.assertEqual(len(fail.calls), 3)                   # "out"（起点）・"out"（起点）・"a.html"（out の中）
        self.assertEqual(len(logs.records), 1)                 # 動かなくなったときの1行だけ（毎回は書かない）
        for rec in logs.records:                               # 英語。パスも名前も書かない
            self.assertTrue(rec.getMessage().isascii(), rec.getMessage())
            self.assertIn("git check-ignore", rec.getMessage())
            self.assertNotIn("a.html", rec.getMessage())
            self.assertNotIn(str(self.root), rec.getMessage())
            self.assertEqual(rec.args, ())
        fail.answer = lambda d, n: {"out"}                     # 直ったら、次の問い合わせで取り戻す
        self.assertEqual(g.ignored(self.root, ["out"]), {"out"})
        self.assertIs(g.available, True)

    def test_git_binary_missing_shows_and_reports(self):
        init_repo(self.root, "out/\n")
        touch(self.root / "out/a.html")
        g = GitIgnore(show_ignored=False)
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=FileNotFoundError("git")), \
                self.assertLogs("bblist.gitignore", "WARNING"):
            self.assertEqual(g.ignored(self.root, ["out"]), set())
        self.assertIs(g.available, False)

    def test_tracked_file_matching_a_pattern_is_not_ignored(self):
        # git が無視しているのは追跡されていないものだけ。add -f したファイルは git の履歴にあり、無視ではない
        init_repo(self.root, "out/\n*.log\n")
        touch(self.root / "out/keep.html")
        touch(self.root / "out/a.html")
        touch(self.root / "tests/sample.log")
        git(self.root, "add", "-f", "out/keep.html", "tests/sample.log")
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(self.root, ["out/keep.html", "out/a.html", "tests/sample.log"]), {"out/a.html"})
        self.assertEqual(g.ignored(self.root, ["out"]), set())   # 追跡済みを含むフォルダは隠さない

    def test_names_in_nfc_and_nfd_spellings(self):
        # git は規則を NFC で比べる（macOS の core.precomposeunicode）。ディスクの名前が NFD でも、渡す綴りが NFC・NFD の
        # どちらでも、規則（NFC で書く）に当たったものは隠す。フォルダの綴りも問わない
        init_repo(self.root, nfc("データ/") + "\n" + nfc("ぱ.log") + "\n")
        touch(self.root / nfd("データ") / "a.md")
        touch(self.root / nfd("ぱ.log"))
        touch(self.root / nfd("ぷ.md"))
        self.assertIn(nfd("データ"), os.listdir(self.root))    # ディスク上が NFD で残っている
        for spell in (nfd, nfc):
            g = GitIgnore(show_ignored=False)
            got = g.ignored(self.root, [spell("データ"), spell("ぱ.log"), spell("ぷ.md"), spell("データ") + "/a.md"])
            self.assertEqual(got, {spell("データ"), spell("ぱ.log"), spell("データ") + "/a.md"}, spell.__name__)

    def test_directory_that_is_a_nested_repo_uses_its_own_rules(self):
        # 入れ子のリポジトリ（サブモジュール）の中は、そのリポジトリの規則で判定する。外から丸ごと
        # `git check-ignore lib/x` を呼ぶと「is in submodule」で全体が止まるので、フォルダごとにそこで呼ぶ
        init_repo(self.root, "*.tmp\n")
        lib = init_repo(self.root / "lib", "zz/\n")
        touch(lib / "f.txt")
        touch(lib / "zz" / "z.txt")
        touch(lib / "a.tmp")
        git(lib, "add", "f.txt")
        git(lib, "commit", "-q", "--no-verify", "-m", "i")
        git(self.root, "add", "lib")                           # 索引に gitlink（サブモジュールの印）が入る
        outer = subprocess.run(["git", "-C", str(self.root), "check-ignore", "lib/f.txt"], capture_output=True)
        self.assertEqual(outer.returncode, 128)                # 前提：外から呼ぶと、本当に止まる
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(self.root, ["lib/zz", "lib/zz/z.txt", "lib/f.txt", "lib/a.tmp"]),
                         {"lib/zz", "lib/zz/z.txt"})           # a.tmp は外の *.tmp ではなく lib の規則で判定する
        self.assertIs(g.available, True)

    def test_names_that_look_like_pathspec_magic_are_plain_names(self):
        # `:!x` `:^x` `:(glob)y` を名前のまま git に渡すと、git が「pathspec magic」と言って 128 で止まり、一覧ぜんぶが
        # 隠されなくなる。`:foo.log` は黙って `foo.log` として調べられる。名前の頭に ./ を付けて渡せば、ただの名前になる
        init_repo(self.root, "*.log\nfoo.log\n")
        weird = [":!x", ":^x", ":(glob)y", ":/z", ":foo.log", ":bar.log"]
        for name in weird + ["foo.log", "a.log", "b.md"]:
            touch(self.root / name)
        g = GitIgnore(show_ignored=False)
        got = g.ignored(self.root, weird + ["foo.log", "a.log", "b.md"])
        self.assertEqual(got, {":foo.log", ":bar.log", "foo.log", "a.log"})   # 並んだ名前が止めない・黙って読み替えない
        self.assertIs(g.available, True)
        # `:foo.log` は規則 `foo.log` だけでは無視されない（`:foo.log` を `foo.log` に読み替えない）
        init_repo(self.tmp / "only", "foo.log\n")
        for name in (":foo.log", "foo.log"):
            touch(self.tmp / "only" / name)
        self.assertEqual(GitIgnore(show_ignored=False).ignored(self.tmp / "only", [":foo.log", "foo.log"]), {"foo.log"})

    def test_names_with_glob_characters_are_hidden_next_to_a_tracked_match(self):
        # 2段目（索引つき）は、渡した名前を glob として索引に当てる。追跡済みの tracked.log があると、`*.log` `t?acked.log`
        # `[t]racked.log` という名前の無視されたファイルが「追跡済み」と読まれて見えてしまう。glob の文字を含む名前は
        # 2段目に回さず、1段目（--no-index。名前は文字どおりに当たる）の答えを使う
        init_repo(self.root, "*.log\n")
        weird = ["*.log", "t?acked.log", "[t]racked.log", "a\\b.log"]
        for name in weird + ["tracked.log", "other.log", "plain.md"]:
            touch(self.root / name)
        git(self.root, "add", "-f", "tracked.log")
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain", "--ignored"], capture_output=True)
        for name in weird + ["other.log"]:                     # 前提：git 自身は、これらを無視と扱っている
            self.assertIn(("!! " + name).encode(), status.stdout.replace(b'"', b"").replace(b"\\\\", b"\\"), name)
        g = GitIgnore(show_ignored=False)
        got = g.ignored(self.root, weird + ["tracked.log", "other.log", "plain.md"])
        self.assertEqual(got, set(weird) | {"other.log"})       # 追跡済みの tracked.log と plain.md は今までどおり見える
        self.assertIs(g.available, True)

    def test_files_in_a_separate_repo_under_an_ignored_folder_are_ignored(self):
        # vendor/ を無視していて、その中に git clone したリポジトリがあると、git は clone の中のリポジトリで判定し、
        # 外の vendor/ の規則を見ない。別のリポジトリになっている祖先のフォルダ自身も、外のリポジトリで聞く
        init_repo(self.root, "vendor/\n")
        clone = init_repo(self.root / "vendor" / "clone")
        touch(clone / "secret.txt")
        touch(clone / "sub" / "x.txt")
        touch(self.root / "src" / "ok.txt")
        inner = init_repo(self.root / "tools" / "lib", "gen/\n")       # 無視されたフォルダの下ではない別のリポジトリ
        touch(inner / "keep.txt")
        touch(inner / "gen" / "g.txt")
        inner_probe = subprocess.run(["git", "-C", str(clone), "check-ignore", "secret.txt"], capture_output=True)
        self.assertEqual(inner_probe.returncode, 1)                      # 前提：中のリポジトリだけを見ると、無視ではない
        g = GitIgnore(show_ignored=False)
        got = g.ignored(self.root, ["vendor", "vendor/clone", "vendor/clone/secret.txt", "vendor/clone/sub",
                                    "vendor/clone/sub/x.txt", "src/ok.txt", "tools/lib", "tools/lib/keep.txt",
                                    "tools/lib/gen", "tools/lib/gen/g.txt"])
        self.assertEqual(got, {"vendor", "vendor/clone", "vendor/clone/secret.txt", "vendor/clone/sub",
                               "vendor/clone/sub/x.txt", "tools/lib/gen", "tools/lib/gen/g.txt"})
        self.assertIs(g.available, True)

    def test_repo_inside_a_repo_inside_an_ignored_folder(self):
        init_repo(self.root, "vendor/\n")
        one = init_repo(self.root / "vendor" / "one")
        two = init_repo(one / "two")
        touch(two / "deep.txt")
        self.assertEqual(GitIgnore(show_ignored=False).ignored(self.root, ["vendor/one/two/deep.txt"]),
                         {"vendor/one/two/deep.txt"})

    def test_separate_repo_that_is_not_ignored_from_outside_is_shown(self):
        # 外のリポジトリが無視していない clone の中は、今までどおり見える（祖先を聞くだけで、隠し過ぎない）
        init_repo(self.root, "build-output/\n")
        clone = init_repo(self.root / "vendor" / "clone", "*.log\n")
        touch(clone / "ok.txt")
        touch(clone / "x.log")
        self.assertEqual(GitIgnore(show_ignored=False).ignored(self.root, ["vendor/clone/ok.txt", "vendor/clone/x.log"]),
                         {"vendor/clone/x.log"})

    def test_second_pass_failure_falls_back_to_the_first_pass(self):
        # 2段目（索引つき）が時間切れ・失敗でも、1段目の答え（規則に当たるもの）は安全側に使う。フォルダごと見せない
        init_repo(self.root, "*.log\n")
        touch(self.root / "a.log")
        touch(self.root / "b.md")
        real_run = subprocess.run
        seen = []

        def flaky(args, **kwargs):
            seen.append(args)
            if "--no-index" not in args:
                raise subprocess.TimeoutExpired(args, 5)
            return real_run(args, **kwargs)

        g = GitIgnore(show_ignored=False)
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=flaky):
            self.assertEqual(g.ignored(self.root, ["a.log", "b.md"]), {"a.log"})
        self.assertEqual(len(seen), 2)                                  # 2段目を試したうえでの答え
        self.assertIs(g.available, True)                                # 安全側の答えが出ているので、動いていない扱いにはしない

    def test_broken_dot_git_does_not_fall_through_to_a_repo_above(self):
        # 起点に .git があっても git の作業コピーでない（空・壊れている）とき、git は上のフォルダへ登って別のリポジトリを使う。
        # ホームを dotfiles の git にして `*` を書いている人では、ぜんぶ無視になってしまう。起点の親より上へは登らせない
        outer = init_repo(self.tmp / "outer", "/*\n")
        root = outer / "inner"
        (root / ".git").mkdir(parents=True)                             # 空の .git
        touch(root / "a.md")
        probe = subprocess.run(["git", "-C", str(root), "check-ignore", "a.md"], capture_output=True)
        self.assertEqual(probe.returncode, 0)                           # 前提：何も防がないと、上のリポジトリの * に当たる
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(root, ["a.md"]), set())
        self.assertIs(g.available, True)
        touch(root / ".git" / "config", "[core]\n")                    # 設定だけの偽の .git でも同じ
        self.assertEqual(GitIgnore(show_ignored=False).ignored(root, ["a.md"]), set())

    def test_nfd_root_parent_is_also_a_ceiling(self):
        # 起点の親の綴りが NFC・NFD のどちらで頼まれても、天井が効く（git は綴りをそのまま比べるため、両方を渡す）
        outer = init_repo(self.tmp / nfd("データ"), "/*\n")
        root = outer / "inner"
        (root / ".git").mkdir(parents=True)
        touch(root / "a.md")
        for spelled in (self.tmp / nfc("データ") / "inner", self.tmp / nfd("データ") / "inner"):
            self.assertEqual(GitIgnore(show_ignored=False).ignored(spelled, ["a.md"]), set(), str(spelled))

    def test_repo_config_cannot_run_a_command(self):
        # 索引を読む git は、リポジトリの設定の core.fsmonitor の命令を走らせる。他人のリポジトリ（git clone したもの）の
        # .git/config で、このサーバーの権限の命令が動いてはいけない
        init_repo(self.root, "out/\n")
        touch(self.root / "b.txt")
        touch(self.root / "out/a")
        git(self.root, "add", "b.txt", ".gitignore")
        marker = self.tmp / "RAN"
        hook = touch(self.tmp / "hook.sh", f'#!/bin/sh\ntouch "{marker}"\nprintf "\\0"\n')
        os.chmod(hook, 0o755)
        git(self.root, "config", "core.fsmonitor", str(hook))
        subprocess.run(["git", "-C", str(self.root), "update-index", "--fsmonitor"], capture_output=True)
        marker.unlink(missing_ok=True)
        # 前提：命令を封じないふつうの check-ignore は、この設定で命令を走らせる（試験が空振りでないことを確かめる）
        subprocess.run(["git", "-C", str(self.root), "check-ignore", "out/a", "b.txt"], capture_output=True)
        if not marker.exists():
            self.skipTest("この git は fsmonitor の命令を check-ignore で走らせない")
        marker.unlink()
        self.assertEqual(GitIgnore(show_ignored=False).ignored(self.root, ["out", "b.txt"]), {"out"})
        self.assertFalse(marker.exists(), "リポジトリの設定の命令が走った")

    def test_folder_that_cannot_be_entered_or_is_missing_is_not_a_failure(self):
        init_repo(self.root, "out/\n")
        g = GitIgnore(show_ignored=False)
        self.assertEqual(g.ignored(self.root, ["nope/x", "nope/y/z"]), set())   # 無いフォルダの中
        self.assertIs(g.available, True)

    def test_odd_paths_are_never_passed_to_git(self):
        fake = Counting(lambda d, n: set(n))
        init_repo(self.root)
        g = GitIgnore(show_ignored=False, git=fake)
        self.assertEqual(g.ignored(self.root, ["", "/", "../x", "a/../b", "a//b", "./c", "a/"]), set())
        self.assertEqual(fake.calls, [])


class BatchTest(GitCase):
    def setUp(self):
        super().setUp()
        init_repo(self.root)
        self.now = [1000.0]
        self.fake = Counting(lambda d, n: {x for x in n if x.endswith(".log")})
        self.g = GitIgnore(show_ignored=False, git=self.fake, ttl=30.0, clock=lambda: self.now[0])

    def test_gitignore_batched(self):
        names = ["a.log", "b.md", "c.log", "d.md"]
        self.assertEqual(self.g.ignored(self.root, names), {"a.log", "c.log"})
        self.assertEqual(len(self.fake.calls), 1)              # 1つのフォルダの判定で git は1回
        self.assertEqual(self.fake.calls[0], (self.root, names))
        self.assertEqual(self.fake.roots, [self.root])         # 天井（起点の親）を決めるため、起点も渡す
        self.now[0] += 29.9
        self.assertEqual(self.g.ignored(self.root, names), {"a.log", "c.log"})
        self.assertEqual(len(self.fake.calls), 1)              # 30 秒以内の2回目は呼ばない
        self.now[0] += 0.2
        self.assertEqual(self.g.ignored(self.root, names), {"a.log", "c.log"})
        self.assertEqual(len(self.fake.calls), 2)              # 30 秒を過ぎたら引き直す

    def test_one_call_per_folder_and_cache_is_per_folder(self):
        rels = ["a.log", "x/b.log", "x/c.md", "y/z/d.log", "y/z/e.md"]
        self.assertEqual(self.g.ignored(self.root, rels), {"a.log", "x/b.log", "y/z/d.log"})
        self.assertEqual(sorted(str(d.relative_to(self.root)) for d, _ in self.fake.calls), [".", "x", "y/z"])
        self.assertEqual(self.g.ignored(self.root, ["x/b.log"]), {"x/b.log"})   # x の分は覚えている
        self.assertEqual(len(self.fake.calls), 3)

    def test_roots_are_cached_separately(self):
        other = init_repo(self.tmp / "other")
        self.g.ignored(self.root, ["a.log"])
        self.g.ignored(other, ["a.log"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_names_not_asked_yet_are_asked_for_alone(self):
        self.g.ignored(self.root, ["a.log", "b.md"])
        self.assertEqual(self.g.ignored(self.root, ["a.log", "n.log"]), {"a.log", "n.log"})
        self.assertEqual(self.fake.calls[1][1], ["n.log"])     # 新しい名前の分だけ聞く
        self.assertEqual(len(self.fake.calls), 2)
        self.now[0] += 31
        self.g.ignored(self.root, ["a.log"])                   # 古い答えは混ぜず、引き直す
        self.assertEqual(self.fake.calls[2][1], ["a.log"])

    def test_failure_keeps_earlier_answers_and_is_not_cached(self):
        self.g.ignored(self.root, ["a.log"])
        self.fake.answer = lambda d, n: None
        with self.assertLogs("bblist.gitignore", "WARNING"):
            self.assertEqual(self.g.ignored(self.root, ["a.log", "n.log"]), {"a.log"})   # 覚えている分は使う
        self.assertIs(self.g.available, False)

    def test_cache_is_bounded(self):
        for i in range(2000):
            self.g.ignored(self.root, [f"d{i}/a.log"])
        self.assertLessEqual(len(self.g._cache), GitIgnore.MAX_FOLDERS)

    def test_concurrent_callers_are_safe(self):
        errors = []

        def work(k):
            try:
                for _ in range(20):
                    got = self.g.ignored(self.root, [f"f{k % 3}/a.log", f"f{k % 3}/b.md", "top.log"])
                    if got != {f"f{k % 3}/a.log", "top.log"}:
                        errors.append(got)
            except Exception as e:                             # noqa: BLE001 試験：どの例外でも失敗にする
                errors.append(e)

        threads = [threading.Thread(target=work, args=(k,)) for k in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.fake.calls), 12 * 2)     # 全員が聞いても、重なった分の無駄で済む
        self.assertEqual(sorted({str(d.relative_to(self.root)) for d, _ in self.fake.calls}), [".", "f0", "f1", "f2"])


class RunCheckIgnoreTest(GitCase):
    def check_args(self, args, kwargs):
        self.assertEqual(args[0], "git")
        self.assertIsInstance(args, list)                      # シェルを通さず、引数の並びで
        for want in ("check-ignore", "--stdin", "-z", "-c", "core.fsmonitor=false"):   # 設定の命令を走らせない
            self.assertIn(want, args)
        self.assertLess(args.index("-c"), args.index("check-ignore"))
        self.assertEqual(args[args.index("-C") + 1], str(self.root))
        self.assertEqual(kwargs["timeout"], 5)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")

    def test_arguments_when_nothing_matches(self):
        init_repo(self.root, "out/\n")
        done = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch("bblist.gitignore.subprocess.run", return_value=done) as run:
            self.assertEqual(run_check_ignore(self.root, ["a.md", "b c.md"]), set())
        self.assertEqual(run.call_count, 1)                    # 規則に当たるものがなければ、聞き直さない
        (args,), kwargs = run.call_args
        self.check_args(args, kwargs)
        self.assertIn("--no-index", args)                      # 1段目は索引を読まない（索引が大きいと遅い）
        # NUL 区切り（空白・改行のある名前でも崩れない）。名前の頭に ./ を付ける（`:!x` のような名前を pathspec と読ませない）
        self.assertEqual(kwargs["input"], b"./a.md\0./b c.md\0")

    def test_candidates_are_asked_again_with_the_index(self):
        # 1段目（--no-index）で規則に当たったものだけを、索引つきで聞き直す。追跡済みのファイルを無視に数えないため
        init_repo(self.root, "out/\n")
        answers = iter([subprocess.CompletedProcess([], 0, b"./b.log\0./d.log\0", b""),
                        subprocess.CompletedProcess([], 0, b"./d.log\0", b"")])
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=lambda *a, **k: next(answers)) as run:
            self.assertEqual(run_check_ignore(self.root, ["a.md", "b.log", "c.md", "d.log"]), {"d.log"})
        self.assertEqual(run.call_count, 2)
        first, second = run.call_args_list
        self.check_args(first[0][0], first[1])
        self.check_args(second[0][0], second[1])
        self.assertIn("--no-index", first[0][0])
        self.assertNotIn("--no-index", second[0][0])           # 2段目は索引を見る
        self.assertEqual(first[1]["input"], b"./a.md\0./b.log\0./c.md\0./d.log\0")
        self.assertEqual(second[1]["input"], b"./b.log\0./d.log\0")   # 候補だけを、元の並びのまま

    def test_glob_named_candidates_skip_the_second_pass(self):
        init_repo(self.root, "*.log\n")
        answers = iter([subprocess.CompletedProcess([], 0, b"./b.log\0./t?.log\0./[x].log\0./c.log\0", b""),
                        subprocess.CompletedProcess([], 0, b"./c.log\0", b"")])
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=lambda *a, **k: next(answers)) as run:
            got = run_check_ignore(self.root, ["a.md", "b.log", "t?.log", "[x].log", "c.log"])
        self.assertEqual(got, {"t?.log", "[x].log", "c.log"})   # b.log は追跡済みと分かった。glob の名前は 1 段目のまま
        self.assertEqual(run.call_args_list[1][1]["input"], b"./b.log\0./c.log\0")   # 2段目は文字どおりの名前だけ
        with mock.patch("bblist.gitignore.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, b"./t?.log\0./[x].log\0./a\\b.log\0", b"")) as run:
            self.assertEqual(run_check_ignore(self.root, ["t?.log", "[x].log", "a\\b.log"]), {"t?.log", "[x].log", "a\\b.log"})
        self.assertEqual(run.call_count, 1)                     # 文字どおりの候補がなければ、2段目は呼ばない

    def test_second_pass_failure_keeps_the_first_pass_answer(self):
        init_repo(self.root, "out/\n")
        answers = iter([subprocess.CompletedProcess([], 0, b"./b.log\0./d.log\0", b""),
                        subprocess.CompletedProcess([], 128, b"", b"fatal: index file corrupt\n")])
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=lambda *a, **k: next(answers)):
            # 索引が読めない：追跡済みかを確かめられないので、規則に当たるものを隠す（安全側）。動かなかったことにはしない
            self.assertEqual(run_check_ignore(self.root, ["b.log", "d.log", "e.md"]), {"b.log", "d.log"})

    def test_ceiling_is_the_parent_of_the_root(self):
        # 起点の親より上のリポジトリへ登らせない（空の .git の起点が、上の dotfiles の git に落ちない）
        init_repo(self.root, "out/\n")
        (self.root / "sub").mkdir()
        done = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch("bblist.gitignore.subprocess.run", return_value=done) as run:
            run_check_ignore(self.root / "sub", ["a"], self.root)
            run_check_ignore(self.root, ["a"])
        with_root, without = run.call_args_list
        ceilings = with_root[1]["env"]["GIT_CEILING_DIRECTORIES"].split(":")
        self.assertIn(str(self.tmp), ceilings)                            # 起点の親（実体のパス）
        self.assertIn(nfd(str(self.tmp)), ceilings)                       # 綴りの違いでも効くよう NFC・NFD を渡す
        self.assertIn(nfc(str(self.tmp)), ceilings)
        self.assertNotIn(str(self.root), ceilings)                        # 起点そのものは天井にしない（起点の .git は見つける）
        self.assertNotIn("GIT_CEILING_DIRECTORIES", without[1]["env"])    # 起点を知らされなければ付けない

    def test_ceiling_is_not_set_when_it_cannot_be_written_safely(self):
        # ":" が入るパスは区切りと区別できない。天井を付けず、.git の有無の確認（GitIgnore.ignored）だけに頼る
        done = subprocess.CompletedProcess([], 1, b"", b"")
        odd = self.tmp / "a:b" / "proj"
        odd.mkdir(parents=True)
        with mock.patch("bblist.gitignore.subprocess.run", return_value=done) as run:
            run_check_ignore(odd, ["a"], odd)
        self.assertNotIn("GIT_CEILING_DIRECTORIES", run.call_args[1]["env"])

    def test_results(self):
        def run(code, out=b"", err=b""):
            return mock.patch("bblist.gitignore.subprocess.run",
                              return_value=subprocess.CompletedProcess([], code, out, err))

        init_repo(self.root)
        with run(0, b"./a b\0./\xe3\x81\x82\0"):
            self.assertEqual(run_check_ignore(self.root, ["a b", "あ", "c"]), {"a b", "あ"})
        with run(1):
            self.assertEqual(run_check_ignore(self.root, ["c"]), set())
        with run(128, b"", b"fatal: not a git repository (or any of the parent directories): .git\n"):
            self.assertEqual(run_check_ignore(self.root, ["c"]), set())        # git でないのは失敗ではない
        with run(128, b"", b"fatal: detected dubious ownership in repository at '/x'\n"):
            self.assertIsNone(run_check_ignore(self.root, ["c"]))
        with run(129):
            self.assertIsNone(run_check_ignore(self.root, ["c"]))
        for exc in (FileNotFoundError("git"), PermissionError("git"), subprocess.TimeoutExpired("git", 5),
                    ValueError("embedded null")):
            with mock.patch("bblist.gitignore.subprocess.run", side_effect=exc):
                self.assertIsNone(run_check_ignore(self.root, ["c"]), repr(exc))

    def test_no_names_and_no_folder_do_not_call_git(self):
        with mock.patch("bblist.gitignore.subprocess.run", side_effect=AssertionError("git を呼んだ")):
            self.assertEqual(run_check_ignore(self.root, []), set())
            self.assertEqual(run_check_ignore(self.root / "nope", ["a"]), set())

    def test_two_thousand_names_in_one_batch(self):
        init_repo(self.root, "*.log\n")
        d = self.root / "big"
        d.mkdir()
        names = []
        for i in range(2000):
            name = f"f{i:04d}.{'log' if i % 4 == 0 else 'md'}"
            (d / name).write_text("x", encoding="utf-8")
            names.append(name)
        with mock.patch("bblist.gitignore.subprocess.run", wraps=subprocess.run) as run:
            got = GitIgnore(show_ignored=False).ignored(self.root, [f"big/{n}" for n in names])
        self.assertEqual(got, {f"big/{n}" for n in names if n.endswith(".log")})
        self.assertEqual(run.call_count, 2)                    # 名前の数によらず、1段目と2段目の2回
        self.assertEqual(run.call_args_list[0][1]["input"].count(b"\0"), 2000)
        self.assertEqual(run.call_args_list[1][1]["input"].count(b"\0"), 500)   # 聞き直すのは候補（*.log）だけ


if __name__ == "__main__":
    unittest.main()
