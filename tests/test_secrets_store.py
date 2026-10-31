"""bblist/secrets_store.py の試験。python3 -m unittest tests.test_secrets_store -v

置き場の secret（32 バイト・権限 600）と、そこから導く起点ごとのファイルの鍵。鍵・secret の中身は試験の出力にも出さない。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bblist.secrets_store import file_key, load_or_create_secret, server_id


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class LoadOrCreateSecretTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.support = Path(self._tmp.name) / "support"

    def test_creates_32_random_bytes_with_mode_600(self):
        got = load_or_create_secret(self.support)                 # 置き場のフォルダが無くても作る
        self.assertIsInstance(got, bytes)
        self.assertEqual(len(got), 32)
        self.assertEqual((self.support / "secret").read_bytes(), got)
        self.assertEqual(mode(self.support / "secret"), 0o600)
        self.assertEqual(mode(self.support), 0o700)               # 作ったフォルダは 700

    def test_second_call_returns_the_same_secret(self):
        first = load_or_create_secret(self.support)
        self.assertEqual(load_or_create_secret(self.support), first)

    def test_different_support_dirs_get_different_secrets(self):
        other = Path(self._tmp.name) / "other"
        self.assertNotEqual(load_or_create_secret(self.support), load_or_create_secret(other))

    def test_existing_folder_keeps_its_mode_but_the_secret_is_still_600(self):
        self.support.mkdir(mode=0o755)
        os.chmod(self.support, 0o755)
        load_or_create_secret(self.support)
        self.assertEqual(mode(self.support / "secret"), 0o600)

    def test_loose_mode_of_an_existing_secret_is_tightened(self):
        got = load_or_create_secret(self.support)
        os.chmod(self.support / "secret", 0o644)
        self.assertEqual(load_or_create_secret(self.support), got)   # 中身は変えない
        self.assertEqual(mode(self.support / "secret"), 0o600)

    def test_truncated_secret_is_replaced_not_used(self):
        # 書きかけ・壊れた短い secret のまま鍵を導かない（短い secret は総当たりしやすい）
        self.support.mkdir()
        (self.support / "secret").write_bytes(b"abc")
        got = load_or_create_secret(self.support)
        self.assertEqual(len(got), 32)
        self.assertNotEqual(got, b"abc")
        self.assertEqual((self.support / "secret").read_bytes(), got)
        self.assertEqual(mode(self.support / "secret"), 0o600)
        self.assertEqual(load_or_create_secret(self.support), got)   # 以後は安定

    def test_symlinked_secret_is_refused(self):
        # secret をリンクにして、読ませる先・書かせる先を変えさせない
        self.support.mkdir()
        target = Path(self._tmp.name) / "elsewhere"
        target.write_bytes(os.urandom(32))
        (self.support / "secret").symlink_to(target)
        with self.assertRaises(OSError):
            load_or_create_secret(self.support)
        self.assertEqual(len(target.read_bytes()), 32)            # 行き先は書き換えない

    def test_leaves_no_temporary_file_behind(self):
        load_or_create_secret(self.support)
        self.assertEqual(sorted(p.name for p in self.support.iterdir()), ["secret"])


REPO = Path(__file__).resolve().parents[1]
# 別のプロセスから呼ぶ。決めた時刻（壁時計）まで待ってから呼び、全員が同時に「無い」を見る形にする
CALL = """
import sys, time
from pathlib import Path
from bblist.secrets_store import load_or_create_secret
support, at = Path(sys.argv[1]), float(sys.argv[2])
while time.time() < at:
    pass
print(load_or_create_secret(support).hex())
"""


class ConcurrentFirstRunTest(unittest.TestCase):
    """最初の起動が同時に走っても、全員が同じ secret を持つ（一時ファイルを置き換えるだけだと最後の者が勝ち、
    先に返した者の値が捨てられる）。無いときは os.link で「無ければ作る」。"""

    ROUNDS = 8
    WORKERS = 8

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def assert_agree(self, got: list, support: Path, msg: str) -> None:
        self.assertEqual(len(set(got)), 1, msg)
        self.assertEqual(len(got), self.WORKERS, msg)
        self.assertEqual((support / "secret").read_bytes().hex(), got[0], msg)           # 残ったものが、全員が返したもの
        self.assertEqual(sorted(p.name for p in support.iterdir()), ["secret"], msg)     # 一時ファイルが残らない

    def threads(self, support: Path) -> list:
        barrier, got = threading.Barrier(self.WORKERS), []

        def run():
            barrier.wait()
            got.append(load_or_create_secret(support).hex())

        workers = [threading.Thread(target=run) for _ in range(self.WORKERS)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        return got

    def processes(self, support: Path) -> list:
        at = time.time() + 1.0                                    # python の起動（数十ms）より後に、全員で同時に始める
        procs = [subprocess.Popen([sys.executable, "-c", CALL, str(support), str(at)], cwd=REPO,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(self.WORKERS)]
        got = []
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
            got.append(out.strip())
        return got

    def test_threads_starting_together_agree(self):
        for i in range(self.ROUNDS):
            support = self.base / f"t{i}"
            self.assert_agree(self.threads(support), support, f"round {i}")

    def test_processes_starting_together_agree(self):
        for i in range(3):
            support = self.base / f"p{i}"
            self.assert_agree(self.processes(support), support, f"round {i}")

    def test_repairing_a_truncated_secret_also_agrees(self):
        # 壊れた短い secret を直すときも、全員が同じ値に着く（直す側は置き場を排他で使う）
        for i in range(self.ROUNDS):
            support = self.base / f"r{i}"
            support.mkdir()
            (support / "secret").write_bytes(b"short")
            self.assert_agree(self.threads(support), support, f"round {i}")
        support = self.base / "rp"
        support.mkdir()
        (support / "secret").write_bytes(b"short")
        self.assert_agree(self.processes(support), support, "processes")


class FileKeyTest(unittest.TestCase):
    """ファイル口の鍵は端末ごと・起点ごと。端末を取り消せば、その端末の鍵は使えなくなる。"""

    SECRET = bytes(range(32))
    DEV, ROOT = "fedcba9876543210", "0123456789abcdef"

    def test_shape_is_64_hex(self):
        key = file_key(self.SECRET, self.DEV, self.ROOT)
        self.assertEqual(len(key), 64)
        int(key, 16)

    def test_is_hmac_sha256_of_file_device_and_root_id(self):
        import hashlib
        import hmac
        want = hmac.new(self.SECRET, b"file:" + self.DEV.encode() + b":" + self.ROOT.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(file_key(self.SECRET, self.DEV, self.ROOT), want)

    def test_depends_on_device_root_and_secret(self):
        a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
        self.assertNotEqual(file_key(self.SECRET, self.DEV, a), file_key(self.SECRET, self.DEV, b))     # 起点ごと
        self.assertNotEqual(file_key(self.SECRET, a, self.ROOT), file_key(self.SECRET, b, self.ROOT))   # 端末ごと
        self.assertNotEqual(file_key(self.SECRET, self.DEV, a), file_key(bytes(32), self.DEV, a))
        self.assertEqual(file_key(self.SECRET, self.DEV, a), file_key(self.SECRET, self.DEV, a))

    def test_swapping_device_and_root_gives_another_key(self):
        a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
        self.assertNotEqual(file_key(self.SECRET, a, b), file_key(self.SECRET, b, a))

    def test_device_id_must_be_16_lowercase_hex(self):
        # 区切りの ":" を含む・長さが違う端末 id で、(端末, 起点) の切れ目をずらせないようにする
        for bad in ("a:b", "", "A" * 16, "g" * 16, "a" * 15, "a" * 17, "../" + "a" * 13, 5, None):
            with self.assertRaises(ValueError, msg=bad):
                file_key(self.SECRET, bad, self.ROOT)


class ServerIdTest(unittest.TestCase):
    SECRET = bytes(range(32))

    def test_is_16_hex_made_from_the_secret(self):
        import hashlib
        import hmac
        sid = server_id(self.SECRET)
        self.assertRegex(sid, r"\A[0-9a-f]{16}\Z")
        self.assertEqual(sid, hmac.new(self.SECRET, b"server-id", hashlib.sha256).hexdigest()[:16])
        self.assertEqual(sid, server_id(self.SECRET))
        self.assertNotEqual(sid, server_id(bytes(32)))

    def test_does_not_reveal_the_secret_or_the_file_keys(self):
        sid = server_id(self.SECRET)
        self.assertNotIn(sid, self.SECRET.hex())
        self.assertFalse(file_key(self.SECRET, "0" * 16, "0" * 16).startswith(sid))


if __name__ == "__main__":
    unittest.main()
