"""bblist/config.py の試験。python3 -m unittest tests.test_config -v

config.json（置き場の中・権限 600）を読む。無い・壊れた項目は既定の値。サーバーは書かない（読むだけ）。
権限は、他人が書けるもの・他人のものは読まない（既定に戻して記録に出す）。読めるだけの緩さは知らせて読む。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bblist.config import Config, load_config


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.support = self.tmp / "support"
        self.support.mkdir(mode=0o700)

    def write(self, data, mode: int = 0o600) -> Path:
        p = self.support / "config.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        os.chmod(p, mode)
        return p

    def load(self) -> Config:
        return load_config(self.support, home=self.home)


class DefaultsTest(ConfigCase):
    def test_no_file_means_every_default(self):
        c = self.load()
        self.assertEqual((c.api_port, c.file_port), (8772, 8773))
        self.assertEqual(c.claude_dir, self.home / ".claude")
        self.assertEqual((c.show_git_ignored, c.preview_external), (False, True))
        self.assertEqual(c.launcher_roots, ())
        self.assertEqual((c.tailscale, c.python), (None, None))
        self.assertEqual(c.problems, ())                       # 無いのは問題ではない（任意の設定）

    def test_the_server_never_creates_the_file(self):
        self.load()
        self.assertFalse((self.support / "config.json").exists())

    def test_a_missing_support_dir_is_fine(self):
        c = load_config(self.tmp / "nowhere", home=self.home)
        self.assertEqual(c.api_port, 8772)
        self.assertFalse((self.tmp / "nowhere").exists())      # 読むだけ。置き場も作らない


class ConfigDefaultsAndBadValuesTest(ConfigCase):
    def test_config_defaults_and_bad_values(self):
        self.write({"apiPort": 9001, "filePort": 9002, "claudeDir": "~/dotfiles/claude", "showGitIgnored": True,
                    "previewExternal": False, "tailscale": "/opt/x/tailscale", "python": "/opt/x/python3",
                    "launcherRoots": [{"path": "~/Projects", "depth": 2}, {"path": "/srv/work", "depth": 1}]})
        c = self.load()
        self.assertEqual((c.api_port, c.file_port), (9001, 9002))
        self.assertEqual(c.claude_dir, self.home / "dotfiles" / "claude")
        self.assertEqual((c.show_git_ignored, c.preview_external), (True, False))
        self.assertEqual((c.tailscale, c.python), ("/opt/x/tailscale", "/opt/x/python3"))
        self.assertEqual(c.launcher_roots, ((self.home / "Projects", 2), (Path("/srv/work"), 1)))
        self.assertEqual(c.problems, ())
        # 1項目ずつ壊す：その項目だけ既定に戻り、ほかは読める。壊れた項目の名前は problems に出る（値は出さない）
        bad = {
            "apiPort": ["8772", 0, 65536, -1, 1.5, True, None, [8772]],
            "filePort": ["8773", 0, 65536, True],
            "claudeDir": [5, "", "relative/dir", "~other/x", "a\0b", None, True],
            "showGitIgnored": ["true", 1, 0, None],
            "previewExternal": ["false", 0, None],
            "tailscale": [5, "", "relative/tailscale", "a\0b", True, ["/x"]],
            "python": [5, "", "python3", True],
        }
        defaults = load_config(self.tmp / "nowhere", home=self.home)
        attr = {"apiPort": "api_port", "filePort": "file_port", "claudeDir": "claude_dir",
                "showGitIgnored": "show_git_ignored", "previewExternal": "preview_external",
                "tailscale": "tailscale", "python": "python"}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.write({"apiPort": 9001, key: value})
                    c = self.load()
                    if key == "apiPort":
                        self.assertEqual(c.api_port, 8772)
                    else:
                        self.assertEqual(c.api_port, 9001)             # ほかの項目は読めている
                        # 既定に戻る。ただし previewExternal は、真偽でない値のとき既定の true でなく false（厳しい側）
                        want = False if key == "previewExternal" else getattr(defaults, attr[key])
                        self.assertEqual(getattr(c, attr[key]), want)
                    self.assertIn(key, " ".join(c.problems))
                    if isinstance(value, str) and len(value) > 3:
                        self.assertNotIn(value, " ".join(c.problems))       # 値は出さない（名前だけ）

    def test_null_tailscale_and_python_are_the_default(self):
        self.write({"tailscale": None, "python": None})
        c = self.load()
        self.assertEqual((c.tailscale, c.python, c.problems), (None, None, ()))

    def test_same_ports_fall_back_to_the_defaults(self):
        self.write({"apiPort": 9000, "filePort": 9000})
        c = self.load()
        self.assertEqual((c.api_port, c.file_port), (8772, 8773))
        self.assertTrue(any("Port" in p for p in c.problems), c.problems)

    def test_not_json_or_not_an_object_means_all_defaults(self):
        for text in ("{broken", "", "[]", "null", "5", '"x"', "﻿{}x"):
            with self.subTest(text=text):
                self.write(text)
                c = self.load()
                self.assertEqual((c.api_port, c.file_port, c.launcher_roots), (8772, 8773, ()))
                self.assertTrue(c.problems)
        self.write("{}")
        self.assertEqual(self.load().problems, ())            # 空の設定は問題ない

    def test_unknown_keys_are_ignored(self):
        self.write({"apiPort": 9001, "somethingNew": {"a": 1}})
        c = self.load()
        self.assertEqual((c.api_port, c.problems), (9001, ()))

    def test_too_large_file_is_not_read(self):
        self.write(json.dumps({"apiPort": 9001, "pad": "x" * 100_000}))
        c = self.load()
        self.assertEqual(c.api_port, 8772)
        self.assertTrue(c.problems)


class LauncherRootsTest(ConfigCase):
    def roots(self, value):
        self.write({"launcherRoots": value})
        return self.load()

    def test_depth_must_be_1_or_2_and_a_bad_entry_is_dropped_alone(self):
        c = self.roots([{"path": "~/a", "depth": 2}, {"path": "~/b", "depth": 3}, {"path": "~/c", "depth": "2"},
                        {"path": "~/d", "depth": True}, {"path": "~/e", "depth": 1}, {"path": "", "depth": 1},
                        {"path": "relative", "depth": 1}, {"depth": 1}, "~/f", 5, None, {"path": 5, "depth": 1}])
        self.assertEqual(c.launcher_roots, ((self.home / "a", 2), (self.home / "e", 1)))
        self.assertIn("launcherRoots", " ".join(c.problems))

    def test_missing_depth_is_1(self):
        c = self.roots([{"path": "~/a"}])
        self.assertEqual((c.launcher_roots, c.problems), (((self.home / "a", 1),), ()))

    def test_not_a_list_means_none(self):
        for value in ({"path": "~/a", "depth": 1}, "~/a", 5, True):
            with self.subTest(value=value):
                c = self.roots(value)
                self.assertEqual(c.launcher_roots, ())
                self.assertIn("launcherRoots", " ".join(c.problems))

    def test_there_is_a_cap_on_the_number_of_roots(self):
        c = self.roots([{"path": f"~/r{i}", "depth": 1} for i in range(100)])
        self.assertEqual(len(c.launcher_roots), 32)
        self.assertIn("launcherRoots", " ".join(c.problems))

    def test_the_home_itself_is_a_valid_root_but_dots_are_not_listed_later(self):
        c = self.roots([{"path": "~", "depth": 1}])           # 見せてよいかは Catalog が決める（ここでは形だけ）
        self.assertEqual(c.launcher_roots, ((self.home, 1),))


class PermissionsTest(ConfigCase):
    """config.json は tailscale のパス（サーバーが実行する）や見せる範囲を決める。他人が書き換えられるものは信じない。"""

    def test_a_600_file_is_read_silently(self):
        self.write({"apiPort": 9001}, 0o600)
        c = self.load()
        self.assertEqual((c.api_port, c.problems), (9001, ()))

    def test_group_or_world_writable_is_refused(self):
        for mode in (0o660, 0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                self.write({"apiPort": 9001, "showGitIgnored": True, "tailscale": "/tmp/evil"}, mode)
                c = self.load()
                self.assertEqual((c.api_port, c.show_git_ignored, c.tailscale), (8772, False, None))   # 既定に戻る
                self.assertTrue(any("config.json" in p for p in c.problems), c.problems)

    def test_readable_by_others_is_read_with_a_note(self):
        for mode in (0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                self.write({"apiPort": 9001}, mode)
                c = self.load()
                self.assertEqual(c.api_port, 9001)
                self.assertTrue(any("config.json" in p for p in c.problems), c.problems)

    def test_a_symlink_is_refused(self):
        real = self.tmp / "elsewhere.json"
        real.write_text('{"apiPort": 9001}', encoding="utf-8")
        os.chmod(real, 0o600)
        (self.support / "config.json").symlink_to(real)
        c = self.load()
        self.assertEqual(c.api_port, 8772)
        self.assertTrue(c.problems)

    def test_a_directory_or_fifo_is_refused_without_hanging(self):
        (self.support / "config.json").mkdir()
        self.assertTrue(self.load().problems)
        (self.support / "config.json").rmdir()
        os.mkfifo(self.support / "config.json")                # 開くと書き手を待つ種類
        c = self.load()
        self.assertEqual(c.api_port, 8772)
        self.assertTrue(c.problems)

    def test_a_file_of_another_owner_is_refused(self):
        p = self.write({"apiPort": 9001})
        real_uid = os.geteuid()
        orig = os.fstat

        class Other:
            def __init__(self, st):
                self.st_mode, self.st_uid, self.st_size = st.st_mode, real_uid + 1, st.st_size

        with mock.patch("bblist.config.os.fstat", side_effect=lambda fd: Other(orig(fd))):
            c = self.load()
        self.assertEqual(c.api_port, 8772)
        self.assertTrue(any("config.json" in p for p in c.problems), c.problems)
        self.assertTrue(p.exists())

    def test_a_refused_file_means_the_strict_preview(self):
        """読めない・信じられないファイルでは、外への読み込みを許す側（緩い側）に倒さない。showGitIgnored も false のまま。"""
        self.write({"previewExternal": True, "showGitIgnored": True}, 0o666)                  # 他人が書ける
        c = self.load()
        self.assertEqual((c.preview_external, c.show_git_ignored), (False, False))
        self.assertTrue(c.problems)
        self.write("{broken")                                                                 # JSON でない
        self.assertEqual((self.load().preview_external, self.load().show_git_ignored), (False, False))
        self.write("[]")                                                                      # 辞書でない
        self.assertFalse(self.load().preview_external)
        (self.support / "config.json").unlink()
        real = self.tmp / "elsewhere.json"
        real.write_text('{"previewExternal": true}', encoding="utf-8")
        os.chmod(real, 0o600)
        (self.support / "config.json").symlink_to(real)                                       # リンク
        self.assertFalse(self.load().preview_external)

    def test_a_non_boolean_preview_external_means_the_strict_side(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                self.write({"previewExternal": value})
                c = self.load()
                self.assertFalse(c.preview_external)
                self.assertTrue(any("previewExternal" in p for p in c.problems), c.problems)
        self.write({"previewExternal": True})
        self.assertTrue(self.load().preview_external)                                         # 真偽ならそのとおり

    def test_a_missing_file_or_item_keeps_the_default_preview(self):
        self.assertTrue(self.load().preview_external)                                         # ファイルが無いのは既定（許す）
        self.write({})
        self.assertTrue(self.load().preview_external)                                         # 書いていない項目も既定

    def test_the_file_is_never_changed(self):
        p = self.write({"apiPort": 9001}, 0o644)
        before = (p.read_bytes(), os.stat(p).st_mode, os.stat(p).st_mtime_ns)
        self.load()
        self.assertEqual((p.read_bytes(), os.stat(p).st_mode, os.stat(p).st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
