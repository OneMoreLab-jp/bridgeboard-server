"""bblist/files.py・usage.py の試験。python3 -m unittest discover -s tests -t . -p 'test_files.py' -v

NFD の名前・読めないフォルダ（chmod 000）は、ディスクに本当に作って試す（APFS は書いた形のまま残す）。
「テスト」は NFC と NFD が同じ文字列なので使えない。濁点・半濁点の付く名前を使う。
chmod 000 は、後始末（tmp の削除）のため、必ず権限を戻す。
"""
from __future__ import annotations

import json
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path

from bblist.files import content_type, list_dir, locate
from bblist.paths import nfc, resolve_in_root
from bblist.usage import read_usage


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def names(result: dict) -> list:
    return [e["name"] for e in result["entries"]]


class ListDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def test_one_level_dirs_first_then_name(self):
        write(self.root / "b.txt", "bb")
        write(self.root / "a.md", "aaa")
        write(self.root / "z" / "deep.md")
        write(self.root / "docs" / "inner" / "x.md")
        r = list_dir(self.root, "")
        self.assertEqual(r["path"], "")
        self.assertEqual(names(r), ["docs", "z", "a.md", "b.txt"])  # 1段だけ（deep.md・inner は出ない）
        self.assertEqual(r["entries"][0], {"name": "docs", "kind": "dir"})  # フォルダは name と kind だけ
        st = os.stat(self.root / "a.md")
        self.assertEqual(r["entries"][2], {"name": "a.md", "kind": "file", "size": 3,
                                           "mtime": st.st_mtime_ns // 1_000_000, "preview": "md"})
        self.assertIsInstance(r["entries"][2]["mtime"], int)        # Unix ミリ秒の整数
        self.assertNotIn("truncated", r)                            # 超えていなければ、キーごと出さない

    def test_subdirectory_path(self):
        write(self.root / "docs" / "decisions" / "d.md")
        write(self.root / "docs" / "README.md")
        for rel in ("docs", "docs/", "/docs//"):
            r = list_dir(self.root, rel)
            self.assertEqual(r["path"], "docs", rel)                # 区切りの余りを整えて返す
            self.assertEqual(names(r), ["decisions", "README.md"], rel)

    def test_sort_ignores_case_and_normalization_form(self):
        # 大文字小文字・NFC/NFD で並びが割れない（名前順は NFC にそろえた casefold）
        write(self.root / "B.md")
        write(self.root / "a.md")
        self.assertEqual(names(list_dir(self.root, "")), ["a.md", "B.md"])
        pa, hi = "ぱ.md", "ひ.md"                                   # NFC は ぱ(U+3071) < ひ(U+3072)
        write(self.root / "n" / nfd(pa))                            # NFD の ぱ は ひ + U+309A で、素の並びなら ひ の後ろ
        write(self.root / "n" / hi)
        self.assertNotEqual(nfd(pa), pa)
        self.assertEqual([nfc(n) for n in names(list_dir(self.root, "n"))], [pa, hi])

    def test_hides_dot_denied_and_skip_dirs(self):
        for name in (".env", "credentials.json", "id_rsa", "notes.db", "key.pem", "page.html.bak-1", ".DS_Store"):
            write(self.root / name)
        for name in (".git", "node_modules", "build", "__pycache__", "DerivedData"):
            write(self.root / name / "inside.md")
        write(self.root / "a.md")
        write(self.root / "page.html")
        write(self.root / "pic.PNG")
        write(self.root / "data.bin")
        write(self.root / "src" / "main.py")
        r = list_dir(self.root, "")
        self.assertEqual(names(r), ["src", "a.md", "data.bin", "page.html", "pic.PNG"])
        previews = {e["name"]: e["preview"] for e in r["entries"] if e["kind"] == "file"}
        self.assertEqual(previews, {"a.md": "md", "data.bin": None, "page.html": "html", "pic.PNG": "image"})

    def test_denied_and_skip_dirs_cannot_be_listed_either(self):
        write(self.root / ".git" / "config")
        write(self.root / "docs" / ".secret" / "x.md")
        write(self.root / "node_modules" / "x.js")
        self.assertIsNone(list_dir(self.root, ".git"))
        self.assertIsNone(list_dir(self.root, "docs/.secret"))      # 拒む名前の下は引かない
        self.assertEqual(names(list_dir(self.root, "node_modules")), ["x.js"])  # 出さないだけ。名指しなら返す

    def test_truncated_over_limit(self):
        for n in "abcde":
            write(self.root / (n + ".md"))
        (self.root / "dir").mkdir()
        r = list_dir(self.root, "", limit=3)
        self.assertEqual(len(r["entries"]), 3)
        self.assertTrue(r["truncated"])
        self.assertEqual(names(r), ["dir", "a.md", "b.md"])         # 並べてから先頭を切る
        r = list_dir(self.root, "", limit=6)                        # ちょうどなら超えていない
        self.assertEqual(len(r["entries"]), 6)
        self.assertNotIn("truncated", r)
        self.assertEqual(len(list_dir(self.root, "")["entries"]), 6)  # 既定の 2,000 件には余裕

    def test_missing_or_outside_is_none(self):
        write(self.root / "a.md")
        outside = Path(self.tmp.name) / "outside"
        write(outside / "o.md")
        os.symlink(outside, self.root / "link-out")
        self.assertIsNone(list_dir(self.root, "nope"))              # 無い
        self.assertIsNone(list_dir(self.root, "a.md"))              # ファイル
        self.assertIsNone(list_dir(self.root, ".."))                # 外
        self.assertIsNone(list_dir(self.root, "../outside"))
        self.assertIsNone(list_dir(self.root, str(outside)))        # 絶対パスは root の下の名前として探す
        self.assertIsNone(list_dir(self.root, "link-out"))          # 外へ出るシンボリックリンク
        self.assertIsNone(list_dir(self.root, "a\0b"))
        self.assertIsNone(list_dir(Path(self.tmp.name) / "no-such-root", ""))

    def test_symlink_entries(self):
        outside = Path(self.tmp.name) / "outside"
        write(outside / "o.md")
        write(self.root / "real" / "x.md")
        write(self.root / "file.md", "abc")
        os.symlink(outside, self.root / "to-outside")               # 外へ出る → 出さない
        os.symlink(outside / "o.md", self.root / "to-outside-file.md")
        os.symlink(self.root / "real", self.root / "to-real")       # 中へ向かう → 出す（フォルダ）
        os.symlink(self.root / "file.md", self.root / "to-file.md")  # 中へ向かう → 出す（ファイル、先の大きさ）
        os.symlink(self.root / "nowhere", self.root / "broken")     # 行き先が無い → 飛ばす
        os.symlink(self.root / ".." / "proj" / ".hidden-target", self.root / "to-dot")
        write(self.root / ".hidden-target")
        r = list_dir(self.root, "")
        self.assertEqual(names(r), ["real", "to-real", "file.md", "to-file.md"])
        kinds = {e["name"]: e["kind"] for e in r["entries"]}
        self.assertEqual(kinds["to-real"], "dir")
        self.assertEqual(r["entries"][3]["size"], 3)

    def test_names_returned_as_on_disk(self):
        name = "ガイド"                                              # 濁点があるので NFC と NFD が別の文字列
        self.assertNotEqual(nfd(name), nfc(name))
        write(self.root / nfd(name) / nfd("ぱ.md"))
        write(self.root / nfc("ぴ.md"))
        self.assertIn(nfd(name), os.listdir(self.root))             # 試験の前提：ディスク上が NFD で残っている
        r = list_dir(self.root, "")
        self.assertIn(nfd(name), names(r))                          # NFD のまま
        self.assertNotIn(nfc(name), names(r))
        self.assertIn(nfc("ぴ.md"), names(r))                       # NFC のものは NFC のまま
        # アプリが NFC で送り返しても、NFD で送り返しても、同じフォルダが開く（APFS は形を区別しない）
        for rel in (nfd(name), nfc(name)):
            r = list_dir(self.root, rel)
            self.assertEqual(names(r), [nfd("ぱ.md")], rel)

    def test_permission_error_propagates(self):
        if os.geteuid() == 0:
            self.skipTest("root は chmod 000 でも読める")
        write(self.root / "ok" / "a.md")
        locked = self.root / "locked"
        write(locked / "secret.md")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)                    # tmp を消せるように戻す
        with self.assertRaises(PermissionError):
            list_dir(self.root, "locked")
        self.assertEqual(names(list_dir(self.root, "")), ["locked", "ok"])  # 親の一覧は、読めない子を載せるだけ
        self.assertEqual(names(list_dir(self.root, "ok")), ["a.md"])


class ListDirIgnoredTest(unittest.TestCase):
    """list_dir の ignored（git で無視されたものを隠す関数）。root と root からの相対パスの並びを受け、隠すものの集まりを返す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name)) / "proj"
        for rel in ("out/a.html", "out/sub/b.html", "keep/c.html", "top.log", "z.md"):
            write(self.root / rel)
        self.asked = []

    def hide(self, *hidden):
        def ignored(root, rels):
            self.asked.append((Path(root), list(rels)))
            return {r for r in rels if r in hidden}
        return ignored

    def test_hidden_entries_are_dropped_dirs_too(self):
        r = list_dir(self.root, "", ignored=self.hide("out", "top.log"))
        self.assertEqual(names(r), ["keep", "z.md"])
        self.assertEqual(len(self.asked), 1)                        # フォルダ1つの一覧で、聞くのは1回
        self.assertEqual(sorted(self.asked[0][1]), ["keep", "out", "top.log", "z.md"])
        self.assertEqual(list_dir(self.root, "")["entries"][0]["name"], "keep")     # 対照：ignored なしでは出る

    def test_relative_paths_are_below_root(self):
        r = list_dir(self.root, "out", ignored=self.hide("out/sub"))
        self.assertEqual(names(r), ["a.html"])
        self.assertEqual(sorted(self.asked[-1][1]), ["out/a.html", "out/sub"])
        self.assertEqual(self.asked[-1][0], self.root)

    def test_the_folder_itself_ignored_is_none(self):
        self.assertIsNone(list_dir(self.root, "out", ignored=self.hide("out")))
        self.assertIsNone(list_dir(self.root, "out/sub", ignored=self.hide("out/sub")))
        self.assertIsNotNone(list_dir(self.root, "keep", ignored=self.hide("out")))
        self.assertIsNotNone(list_dir(self.root, "", ignored=self.hide("")))      # 起点そのものは聞かない

    def test_symlink_to_an_ignored_folder_is_hidden_and_cannot_be_listed(self):
        os.symlink(self.root / "out", self.root / "latest")
        r = list_dir(self.root, "", ignored=self.hide("out"))
        self.assertEqual(names(r), ["keep", "top.log", "z.md"])       # latest -> out も出さない
        self.assertIsNone(list_dir(self.root, "latest", ignored=self.hide("out")))
        # 実体の相対パスで聞く（リンクの綴りの下を git に渡すと、git が「リンクの先」と言って止まる）
        listed = list_dir(self.root, "latest", ignored=self.hide("nothing"))
        self.assertEqual(names(listed), ["sub", "a.html"])
        self.assertEqual(sorted(self.asked[-1][1]), ["out/a.html", "out/sub"])

    def test_limit_counts_what_is_shown(self):
        for i in range(5):
            write(self.root / "many" / f"f{i}.log" if i < 3 else self.root / "many" / f"f{i}.md")
        r = list_dir(self.root, "many", limit=2, ignored=self.hide("many/f0.log", "many/f1.log"))
        self.assertEqual(names(r), ["f2.log", "f3.md"])
        self.assertIs(r.get("truncated"), True)                     # 隠した後に 3 件（f2.log f3.md f4.md）残り、2 件に切った
        r = list_dir(self.root, "many", limit=3, ignored=self.hide("many/f0.log", "many/f1.log"))
        self.assertNotIn("truncated", r)                            # 隠した分は数えない

    def test_folder_in_the_requested_spelling_and_names_as_on_disk(self):
        # フォルダは resolve_in_root が返す綴り（頼まれたまま。NFC）、中の名前はディスクの綴り（NFD）。
        # どちらの綴りでも git は同じフォルダに入れ、規則は NFC で比べる（GitIgnore が名前を NFC でも聞く）
        write(self.root / nfd("データ") / nfd("ぱ.md"))
        r = list_dir(self.root, nfc("データ"), ignored=self.hide())
        self.assertEqual(names(r), [nfd("ぱ.md")])
        self.assertEqual(self.asked[-1][1], [nfc("データ") + "/" + nfd("ぱ.md")])


class ContentTypeTest(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(content_type("a.md"), "text/markdown; charset=utf-8")
        self.assertEqual(content_type("a.html"), "text/html; charset=utf-8")
        self.assertEqual(content_type("a.css"), "text/css; charset=utf-8")
        self.assertEqual(content_type("a.txt"), "text/plain; charset=utf-8")
        self.assertEqual(content_type("a.js"), "application/javascript; charset=utf-8")
        self.assertEqual(content_type("a.json"), "application/json; charset=utf-8")
        self.assertEqual(content_type("a.svg"), "image/svg+xml; charset=utf-8")
        self.assertEqual(content_type("a.png"), "image/png")        # 文字でないものには charset を付けない
        self.assertEqual(content_type("a.webp"), "image/webp")      # Python 3.9 の表には無い
        self.assertEqual(content_type("a.PDF"), "application/pdf")
        self.assertEqual(content_type("ガイド.MD"), "text/markdown; charset=utf-8")

    def test_unknown_is_octet_stream(self):
        for name in ("a.xyz", "README", "", ".env", "a.md.gz"):    # 圧縮の入れ物は、中の種類を名乗らない
            self.assertEqual(content_type(name), "application/octet-stream", name)


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.proj = self.base / "proj"
        write(self.proj / "docs" / "a.md")
        self.roots = {"main": self.proj}
        self.own = ("main", self.proj)

    def test_locate_absolute_relative_and_outside(self):
        cwd = self.proj
        r = locate("docs/a.md", str(cwd), self.own, self.roots)
        self.assertEqual(r, {"rootId": "main", "relPath": "docs/a.md", "preview": "md", "exists": True})
        self.assertEqual(locate(str(self.proj / "docs" / "a.md"), str(cwd), self.own, self.roots), r)
        self.assertEqual(locate("a.md", str(cwd / "docs"), self.own, self.roots), r)       # cwd からの相対
        self.assertEqual(locate("../docs/./a.md", str(cwd / "docs"), self.own, self.roots), r)
        self.assertIsNone(locate("/etc/hosts", str(cwd), self.own, self.roots))
        self.assertIsNone(locate("../outside.md", str(cwd), self.own, self.roots))         # 相対でも外へ出れば None
        self.assertIsNone(locate(str(self.base / "proj2" / "x.md"), str(cwd), self.own, self.roots))  # 前方一致は中ではない
        self.assertIsNone(locate("docs/a.md", "", self.own, self.roots))                   # cwd が無ければ相対は引かない
        self.assertIsNone(locate("docs/a\0.md", str(cwd), self.own, self.roots))

    def test_locate_nonexistent_file_inside_root(self):
        r = locate("docs/new.md", str(self.proj), self.own, self.roots)
        self.assertEqual(r, {"rootId": "main", "relPath": "docs/new.md", "preview": "md", "exists": False})
        r = locate("notes/memo.txt", str(self.proj), self.own, self.roots)                 # 親フォルダも無い
        self.assertEqual((r["relPath"], r["preview"], r["exists"]), ("notes/memo.txt", None, False))

    def test_locate_exists_is_null_when_the_folder_cannot_be_read(self):
        # 読めないフォルダ（chmod 000・macOS の守り）の中は、あるか分からない。false（消えた）と言わず null
        if os.geteuid() == 0:
            self.skipTest("root は chmod 000 でも読める")
        locked = self.proj / "locked"
        write(locked / "a.md")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)                    # tmp を消せるように戻す
        r = locate("locked/a.md", str(self.proj), self.own, self.roots)
        self.assertEqual(r, {"rootId": "main", "relPath": "locked/a.md", "preview": "md", "exists": None})
        self.assertIsNone(locate("locked/none.md", str(self.proj), self.own, self.roots)["exists"])
        # 読めるところで無いものは今までどおり false（親がファイルのものも）
        self.assertIs(locate("docs/none.md", str(self.proj), self.own, self.roots)["exists"], False)
        self.assertIs(locate("docs/a.md/x.md", str(self.proj), self.own, self.roots)["exists"], False)
        self.assertIs(locate("docs/a.md", str(self.proj), self.own, self.roots)["exists"], True)

    def test_locate_denied_names_are_none(self):
        write(self.proj / ".env")
        write(self.proj / "docs" / ".git" / "config")
        for p in (".env", "docs/.git/config", "credentials.json", "docs/key.pem"):
            self.assertIsNone(locate(p, str(self.proj), self.own, self.roots), p)

    def test_locate_symlink_out_of_root_is_none(self):
        outside = write(self.base / "outside" / "o.md")
        os.symlink(outside, self.proj / "docs" / "link.md")
        self.assertIsNone(locate("docs/link.md", str(self.proj), self.own, self.roots))

    def test_locate_worktree_file_prefers_own_root(self):
        wt = self.base / "proj-wt"                                  # git worktree add ../proj-wt（兄弟）
        write(wt / "docs" / "a.md")
        nested = self.proj / "wtdir"                                # プロジェクトの中にある worktree
        write(nested / "docs" / "a.md")
        roots = {"main": self.proj, "wt": wt, "nested": nested}
        # 自分の起点の中なら、それが先。より深い起点があっても
        r = locate(str(nested / "docs" / "a.md"), str(self.proj), ("main", self.proj), roots)
        self.assertEqual((r["rootId"], r["relPath"]), ("main", "wtdir/docs/a.md"))
        r = locate(str(nested / "docs" / "a.md"), str(nested), ("nested", nested), roots)
        self.assertEqual((r["rootId"], r["relPath"]), ("nested", "docs/a.md"))
        # 自分の起点の外なら、他の起点の中で最も深いもの
        r = locate(str(wt / "docs" / "a.md"), str(self.proj), ("main", self.proj), roots)
        self.assertEqual((r["rootId"], r["relPath"], r["exists"]), ("wt", "docs/a.md", True))
        r = locate(str(nested / "docs" / "a.md"), str(wt), ("wt", wt), roots)
        self.assertEqual((r["rootId"], r["relPath"]), ("nested", "docs/a.md"))             # main と nested のうち深い方
        # 自分の起点が無い（見せてよいフォルダでない）ときも、roots から探す
        r = locate(str(wt / "docs" / "a.md"), str(wt), None, roots)
        self.assertEqual((r["rootId"], r["relPath"]), ("wt", "docs/a.md"))
        self.assertIsNone(locate(str(wt / "docs" / "a.md"), str(wt), None, {}))

    def test_locate_falls_through_when_own_root_refuses_the_path(self):
        # 自分の起点の中でも、途中に拒む名前（secrets… など）があれば拒まれる。その下に別の起点があれば、そちらで引く
        wt = self.proj / "secrets-wt" / "feat"
        write(wt / "docs" / "a.md")
        roots = {"main": self.proj, "wt": wt}
        r = locate(str(wt / "docs" / "a.md"), str(self.proj), ("main", self.proj), roots)
        self.assertEqual((r["rootId"], r["relPath"]), ("wt", "docs/a.md"))
        self.assertIsNone(locate(str(wt / "docs" / "a.md"), str(self.proj), ("main", self.proj), {"main": self.proj}))

    def test_locate_matches_nfc_and_nfd_spellings(self):
        # Claude が NFC で書いたパスが NFD のフォルダ・ファイルを指す（逆も）。同じファイルとして引く
        folder = "デバイス"
        self.assertNotEqual(nfd(folder), nfc(folder))
        root_nfd = self.base / nfd(folder)
        write(root_nfd / nfd("ガイド") / nfd("ぱ.md"))
        self.assertIn(nfd(folder), os.listdir(self.base))           # ディスク上が NFD
        roots = {"r": root_nfd}
        for root_spelling in (root_nfd, self.base / nfc(folder)):
            for file_spelling in (nfc, nfd):
                p = str(self.base / file_spelling(folder) / file_spelling("ガイド") / file_spelling("ぱ.md"))
                r = locate(p, str(root_spelling), ("r", root_spelling), {"r": root_spelling})
                self.assertIsNotNone(r, (root_spelling, file_spelling))
                self.assertEqual((r["rootId"], r["exists"], r["preview"]), ("r", True, "md"))
                self.assertEqual(nfc(r["relPath"]), nfc("ガイド/ぱ.md"))
                # relPath を resolve_in_root に戻すと同じファイルに着く（ファイル口・ツリーが使う道）
                self.assertTrue(os.path.samefile(resolve_in_root(root_nfd, r["relPath"]), root_nfd / nfd("ガイド") / nfd("ぱ.md")))
        r = locate(nfc("ぱ.md"), str(root_nfd / nfd("ガイド")), ("r", root_nfd), roots)   # 相対も同様
        self.assertTrue(r["exists"])


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage.json"

    def put(self, obj) -> None:
        self.path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

    def test_usage_missing_and_present(self):
        self.assertEqual(read_usage(self.path), {"available": False})
        self.put({"capturedAt": 1792941510,
                  "rate_limits": {"five_hour": {"used_percentage": 41.2, "resets_at": 1792950000},
                                  "seven_day": {"used_percentage": 18.0, "resets_at": 1793400000}}})
        self.assertEqual(read_usage(self.path), {
            "available": True, "capturedAt": 1792941510,
            "fiveHour": {"usedPercentage": 41.2, "resetsAt": 1792950000},
            "sevenDay": {"usedPercentage": 18.0, "resetsAt": 1793400000}})

    def test_missing_window_is_null(self):
        self.put({"capturedAt": 1, "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 9}}})
        r = read_usage(self.path)
        self.assertEqual(r, {"available": True, "capturedAt": 1,
                             "fiveHour": {"usedPercentage": 5, "resetsAt": 9}, "sevenDay": None})
        self.put({"capturedAt": 1})                                 # rate_limits ごと無い（取得時刻だけは確か）
        self.assertEqual(read_usage(self.path), {"available": True, "capturedAt": 1, "fiveHour": None, "sevenDay": None})
        self.put({"capturedAt": 1, "rate_limits": {"five_hour": "x", "seven_day": {"resets_at": 9}}})
        self.assertEqual(read_usage(self.path), {"available": True, "capturedAt": 1, "fiveHour": None, "sevenDay": None})
        self.put({"capturedAt": 1, "rate_limits": {"seven_day": {"used_percentage": 7}}})  # resets_at だけ無い
        self.assertEqual(read_usage(self.path)["sevenDay"], {"usedPercentage": 7, "resetsAt": None})

    def test_broken_files_are_unavailable(self):
        for text in ("", "{", "[1, 2]", "null", '"text"', "{}", '{"rate_limits": {}}',
                     '{"capturedAt": "yesterday"}', '{"capturedAt": true}', '{"capturedAt": NaN}'):
            self.put(text)
            self.assertEqual(read_usage(self.path), {"available": False}, text)
        self.path.write_bytes(b"\xff\xfe\x00broken")                # UTF-8 でない
        self.assertEqual(read_usage(self.path), {"available": False})
        self.assertEqual(read_usage(Path(self.tmp.name)), {"available": False})  # フォルダ（読めない）

    def test_non_finite_numbers_are_null(self):
        self.put('{"capturedAt": 1, "rate_limits": {"five_hour": {"used_percentage": NaN, "resets_at": 9}}}')
        self.assertIsNone(read_usage(self.path)["fiveHour"])        # NaN は JSON に出せない

    def test_unreadable_file_is_unavailable(self):
        if os.geteuid() == 0:
            self.skipTest("root は chmod 000 でも読める")
        self.put({"capturedAt": 1})
        os.chmod(self.path, 0o000)
        self.addCleanup(os.chmod, self.path, 0o644)
        self.assertEqual(read_usage(self.path), {"available": False})


if __name__ == "__main__":
    unittest.main()
