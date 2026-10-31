"""bblist/devices.py の試験。python3 -m unittest tests.test_devices -v

端末ごとの鍵の置き場（devices.json）。鍵の文字列は置き場に残さず（ハッシュだけ）、試験の出力にも出さない。
置き場は別のプロセス（bbctl）からも書かれるので、別の DeviceStore から見ても同じに見える（取り消しがすぐ効く）ことを確かめる。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bblist import devices
from bblist.devices import DeviceStore, clean_name


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Clock:
    """壁の時計（秒）と単調な時計を、試験が進める。"""

    def __init__(self):
        self.wall = 1_792_000_000.0
        self.mono = 5000.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class DeviceStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.support = Path(self._tmp.name) / "support"
        self.clock = Clock()
        self.store = self.make()

    def make(self) -> DeviceStore:
        return DeviceStore(self.support, wall=lambda: self.clock.wall, mono=lambda: self.clock.mono)

    def file_rows(self) -> list:
        return json.loads((self.support / "devices.json").read_text(encoding="utf-8"))

    # ---- 作る・照らす

    def test_add_returns_id_and_token(self):
        dev, token = self.store.add("My iPad")
        self.assertRegex(dev, r"\A[0-9a-f]{16}\Z")
        self.assertGreaterEqual(len(token), 43)                    # secrets.token_urlsafe(32) 以上
        self.assertRegex(token, r"\A[A-Za-z0-9_-]+\Z")
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {"id": dev, "name": "My iPad", "createdAt": int(self.clock.wall * 1000),
                                   "lastSeenAt": None})

    def test_store_has_hash_only(self):
        dev, token = self.store.add("My iPad")
        text = (self.support / "devices.json").read_text(encoding="utf-8")
        self.assertNotIn(token, text)                              # 鍵の文字列が置き場に無い
        self.assertEqual(self.file_rows(), [{"id": dev, "name": "My iPad", "hash": hashlib.sha256(token.encode()).hexdigest(),
                                             "createdAt": int(self.clock.wall * 1000), "lastSeenAt": None}])
        for row in self.store.list():                              # 一覧はハッシュも出さない
            self.assertNotIn("hash", row)

    def test_secret_files_mode_600(self):
        self.store.add("a")
        self.assertEqual(mode(self.support / "devices.json"), 0o600)
        self.assertEqual(mode(self.support), 0o700)                # 作ったフォルダは 700
        os.chmod(self.support / "devices.json", 0o644)
        self.store.add("b")                                        # 書き直すと 600 に戻る
        self.assertEqual(mode(self.support / "devices.json"), 0o600)
        self.assertEqual(sorted(p.name for p in self.support.iterdir()), ["devices.json"])   # 一時ファイルを残さない

    def test_verify_accepts_the_token_and_nothing_else(self):
        dev, token = self.store.add("a")
        other, other_token = self.store.add("b")
        self.assertEqual(self.store.verify(token), dev)
        self.assertEqual(self.store.verify(other_token), other)
        for bad in ("", "x", token + "x", token[:-1], token.upper(), " " + token, token + "\n", "トークン",
                    hashlib.sha256(token.encode()).hexdigest(), dev, None, 123, b"x"):
            self.assertIsNone(self.store.verify(bad), repr(bad))

    def test_verify_compares_every_entry_with_compare_digest(self):
        ids = [self.store.add(f"d{i}") for i in range(3)]
        with mock.patch.object(devices.hmac, "compare_digest", wraps=devices.hmac.compare_digest) as cmp:
            self.assertEqual(self.store.verify(ids[0][1]), ids[0][0])          # 先頭に当たっても全件を比べる
            self.assertEqual(cmp.call_count, 3)
            cmp.reset_mock()
            self.assertIsNone(self.store.verify("nope"))
            self.assertEqual(cmp.call_count, 3)

    def test_ids_are_unique_and_tokens_differ(self):
        got = [self.store.add("same name") for _ in range(20)]
        self.assertEqual(len({d for d, _ in got}), 20)
        self.assertEqual(len({t for _, t in got}), 20)

    # ---- 取り消し

    def test_revoke(self):
        a, ta = self.store.add("a")
        b, tb = self.store.add("b")
        self.assertTrue(self.store.revoke(a))
        self.assertIsNone(self.store.verify(ta))
        self.assertEqual(self.store.verify(tb), b)
        self.assertEqual([r["id"] for r in self.store.list()], [b])
        self.assertFalse(self.store.revoke(a))                     # 二度目・知らない id は False
        self.assertFalse(self.store.revoke("0" * 16))
        self.assertFalse(self.store.revoke(""))
        self.assertFalse(self.store.exists(a))
        self.assertTrue(self.store.exists(b))
        self.assertNotIn(ta, (self.support / "devices.json").read_text(encoding="utf-8"))

    def test_another_store_object_sees_adds_and_revokes_at_once(self):
        # 動いているサーバーの DeviceStore と、bbctl の DeviceStore は別のプロセスにある。取り消しは再起動なしで効く
        server = self.make()
        self.assertIsNone(server.verify("x"))                      # 空のうちに読ませておく
        dev, token = self.store.add("a")                           # bbctl 側で足す
        self.assertEqual(server.verify(token), dev)
        self.assertTrue(self.store.revoke(dev))                    # bbctl 側で取り消す
        self.assertIsNone(server.verify(token))
        self.assertFalse(server.exists(dev))
        self.assertEqual(server.list(), [])

    def test_last_seen_written_once_per_minute(self):
        dev, token = self.store.add("a")
        t0 = int(self.clock.wall * 1000)
        self.assertEqual(self.store.verify(token), dev)
        self.assertEqual(self.file_rows()[0]["lastSeenAt"], t0)    # 最初の1回は書く
        self.clock.advance(30)
        for _ in range(5):
            self.store.verify(token)
        self.assertEqual(self.file_rows()[0]["lastSeenAt"], t0)    # 1分たつまでは書き直さない
        self.clock.advance(31)
        self.store.verify(token)
        self.assertEqual(self.file_rows()[0]["lastSeenAt"], int(self.clock.wall * 1000))
        self.assertEqual(self.store.list()[0]["lastSeenAt"], int(self.clock.wall * 1000))

    def test_last_seen_does_not_write_the_file_between_minutes(self):
        dev, token = self.store.add("a")
        self.store.verify(token)
        with mock.patch.object(devices, "write_atomic", side_effect=AssertionError("書き直した")):
            self.clock.advance(59)
            self.assertEqual(self.store.verify(token), dev)

    def test_a_late_last_seen_does_not_bring_back_a_revoked_device(self):
        # 取り消しと lastSeenAt の書き込みが重なっても、取り消した端末を書き戻さない
        server = self.make()
        dev, token = self.store.add("a")
        self.assertEqual(server.verify(token), dev)                # server は端末を知っている
        self.clock.advance(120)
        self.assertTrue(self.store.revoke(dev))
        server.touch(dev)                                          # 取り消しを知らないまま書きに来た
        self.assertEqual(self.file_rows(), [])
        self.assertEqual(server.list(), [])
        # 確かめた直後に取り消された（exists が True を答えたあと、書く前に消えた）場合も、書き戻さない
        dev2, _ = self.store.add("b")
        with mock.patch.object(server, "exists", return_value=True):
            self.assertTrue(self.store.revoke(dev2))
            self.clock.advance(120)
            server.touch(dev2)
        self.assertEqual(self.file_rows(), [])

    def test_last_seen_write_failure_does_not_break_auth(self):
        dev, token = self.store.add("a")
        with mock.patch.object(devices, "write_atomic", side_effect=OSError("disk full")), \
                self.assertLogs("bblist.devices", "WARNING") as cm:
            self.assertEqual(self.store.verify(token), dev)
            self.clock.advance(120)
            self.assertEqual(self.store.verify(token), dev)
        text = "\n".join(cm.output)
        self.assertIn("OSError", text)                                 # 型の名前だけ。例外の文・鍵・名前は書かない
        for secret in ("disk full", token):
            self.assertNotIn(secret, text.replace("WARNING:bblist.devices:", ""))

    def test_touch_for_an_unknown_device_does_nothing(self):
        self.store.touch("0" * 16)
        self.assertFalse((self.support / "devices.json").exists())

    # ---- 壊れた置き場

    def test_missing_file_means_no_devices(self):
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.verify("x"))
        self.assertFalse((self.support / "devices.json").exists())    # 読むだけでは作らない

    def test_unreadable_file_denies_everyone_and_is_never_overwritten(self):
        dev, token = self.store.add("a")
        for junk in (b"{not json", b"{}", b'"x"', b"[1,2]", b"[{\"id\": 1}]"):
            (self.support / "devices.json").write_bytes(junk)
            fresh = self.make()
            with self.assertLogs("bblist.devices", "WARNING") as cm:     # 読めないことを記録する（型の名前だけ）
                self.assertIsNone(fresh.verify(token), junk)       # 読めなければ誰も通さない
                self.assertEqual(fresh.list(), [], junk)
            self.assertNotIn(token, "\n".join(cm.output))
            with self.assertRaises(ValueError, msg=junk):          # 足す・取り消すのに、壊れたものを上書きしない
                fresh.add("b")
            with self.assertRaises(ValueError, msg=junk):
                fresh.revoke(dev)
            with self.assertRaises(ValueError, msg=junk):          # strict は壊れていると言う（空とは別）
                fresh.list(strict=True)
            self.assertEqual((self.support / "devices.json").read_bytes(), junk)

    def test_a_symlinked_file_is_not_followed(self):
        dev, token = self.store.add("a")
        elsewhere = Path(self._tmp.name) / "elsewhere.json"
        (self.support / "devices.json").rename(elsewhere)
        (self.support / "devices.json").symlink_to(elsewhere)
        fresh = self.make()
        with self.assertLogs("bblist.devices", "WARNING"):
            self.assertIsNone(fresh.verify(token))
        with self.assertRaises(OSError):
            fresh.add("b")

    def test_deeply_nested_json_is_an_unreadable_file_not_a_crash(self):
        dev, token = self.store.add("a")
        (self.support / "devices.json").write_bytes(b"[" * 5000)
        fresh = self.make()
        with self.assertLogs("bblist.devices", "WARNING"):
            self.assertIsNone(fresh.verify(token))
        with self.assertRaises(ValueError):
            fresh.add("b")

    def test_one_bad_entry_makes_the_whole_file_unreadable(self):
        dev, token = self.store.add("a")
        rows = self.file_rows()
        rows.append({"id": "zz", "name": "x", "hash": "short", "createdAt": 1, "lastSeenAt": None})
        (self.support / "devices.json").write_text(json.dumps(rows), encoding="utf-8")
        fresh = self.make()
        with self.assertLogs("bblist.devices", "WARNING"):
            self.assertIsNone(fresh.verify(token))                 # 半分だけ使うことはしない（手で壊したものを黙って直さない）
        with self.assertRaises(ValueError):
            fresh.add("b")

    # ---- 同時

    def test_concurrent_adds_all_survive(self):
        stores = [self.make() for _ in range(6)]
        got, errors = [], []

        def work(store):
            try:
                for i in range(5):
                    got.append(store.add(f"t{i}"))
            except Exception as e:                                 # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 30)
        self.assertEqual({r["id"] for r in self.make().list()}, {d for d, _ in got})


class CleanNameTest(unittest.TestCase):
    def test_keeps_a_normal_name(self):
        self.assertEqual(clean_name("Tomo's iPad Pro"), "Tomo's iPad Pro")
        self.assertEqual(clean_name("タカシのiPad"), "タカシのiPad")

    def test_drops_control_characters_and_trims(self):
        self.assertEqual(clean_name("  iPad\x1b[31m red\x07\n\r\t "), "iPad[31m red")
        self.assertEqual(clean_name("a‮b c​d"), "abcd")      # 向きを変える文字・行の区切りなども

    def test_limits_the_length(self):
        self.assertEqual(len(clean_name("x" * 1000)), 64)

    def test_empty_or_not_text_gets_a_default(self):
        for bad in ("", "   ", "\x1b\x07", None, 5, b"x", ["a"]):
            self.assertEqual(clean_name(bad), "Unnamed device", repr(bad))

    def test_add_cleans_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            store = DeviceStore(Path(d))
            dev, _ = store.add("iPad\x1b[2J\nx")
            self.assertEqual(store.list()[0]["name"], "iPad[2Jx")


if __name__ == "__main__":
    unittest.main()
