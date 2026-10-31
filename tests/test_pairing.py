"""bblist/pairing.py の試験。python3 -m unittest tests.test_pairing -v

5分で切れる、一度きりの組む符号。符号そのものは置き場に残さず（ハッシュだけ）、期限は単調な時計・起動時刻で見る。
bbctl（別のプロセス）が作り、動いているサーバーが引き換えるので、時計は別のプロセスの間で比べられるものでなければならない。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bblist import pairing
from bblist.fsutil import exclusive, write_atomic
from bblist.pairing import PairLimiter, Pairing, boot_time, monotonic, parse_boottime, remove_stale_qr

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class PairingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.support = Path(self._tmp.name) / "support"
        self.clock = Clock()
        self.boot = [1_792_343_805]
        self.pairing = self.make()

    def make(self) -> Pairing:
        return Pairing(self.support, clock=self.clock, boot=lambda: self.boot[0])

    @property
    def path(self) -> Path:
        return self.support / "pairing.json"

    # ---- 作る

    def test_create_makes_a_128_bit_code_and_stores_only_its_hash(self):
        code = self.pairing.create()
        self.assertEqual(len(code), 22)                            # secrets.token_urlsafe(16)＝128 ビット
        self.assertRegex(code, r"\A[A-Za-z0-9_-]{22}\Z")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"hash": hashlib.sha256(code.encode()).hexdigest(), "deadline": 1300.0, "boot": self.boot[0]})
        self.assertNotIn(code, self.path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.support).st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in self.support.iterdir()), ["pairing.json"])

    def test_codes_differ_each_time(self):
        self.assertEqual(len({self.pairing.create() for _ in range(20)}), 20)

    def test_a_new_code_replaces_the_pending_one(self):
        first = self.pairing.create()
        second = self.pairing.create()
        self.assertFalse(self.pairing.redeem(first))
        self.assertTrue(self.pairing.redeem(second))

    # ---- 引き換える

    def test_pair_success_then_code_unusable(self):
        code = self.pairing.create()
        self.assertTrue(self.pairing.redeem(code))
        self.assertFalse(self.path.exists())                       # 引き換えたら消える
        self.assertFalse(self.pairing.redeem(code))                # 何度送っても断る
        self.assertFalse(self.make().redeem(code))

    def test_a_wrong_code_neither_passes_nor_uses_up_the_pending_one(self):
        code = self.pairing.create()
        for bad in ("", "x", code[:-1], code + "x", code.upper(), code.lower() if code.lower() != code else "y" * 22,
                    "コード", " " + code, None, 123, b"x", ["a"], "x" * 100_000):
            self.assertFalse(self.pairing.redeem(bad), repr(bad)[:40])
        self.assertTrue(self.path.exists())
        self.assertTrue(self.pairing.redeem(code))

    def test_pair_expired(self):
        code = self.pairing.create()
        self.clock.now += 301
        self.assertFalse(self.pairing.redeem(code))
        code = self.pairing.create()
        self.clock.now += 299
        self.assertTrue(self.pairing.redeem(code))                 # 5分の手前なら通る
        code = self.pairing.create()
        self.clock.now += 300
        self.assertFalse(self.pairing.redeem(code))                # ちょうど5分で切れる

    def test_expired_code_stays_expired(self):
        code = self.pairing.create()
        self.clock.now += 301
        self.assertFalse(self.pairing.redeem(code))
        self.clock.now = 1000.0                                    # 時計が戻っても、期限切れの符号は生き返らない
        self.assertFalse(self.pairing.redeem(code))

    def test_pair_expiry_uses_monotonic_clock(self):
        # 壁の時計（time.time）を戻しても期限は延びない。進めても縮まない
        code = self.pairing.create()
        self.clock.now += 301
        with mock.patch("time.time", return_value=time.time() - 3600):
            self.assertFalse(self.pairing.redeem(code))
        code = self.pairing.create()
        self.clock.now += 10
        with mock.patch("time.time", return_value=time.time() + 3600):
            self.assertTrue(self.pairing.redeem(code))

    def test_pair_invalid_after_reboot(self):
        code = self.pairing.create()
        self.boot[0] += 90                                         # 再起動した（起動時刻が違う）
        self.assertFalse(self.pairing.redeem(code))
        self.assertTrue(self.pairing.redeem(self.pairing.create()))   # 新しく作れば通る

    def test_a_small_shift_of_the_boot_time_is_the_same_boot(self):
        # 壁の時計を直すと起動時刻もずれうる。数秒のずれで符号を捨てない（再起動なら、もっと開く）
        code = self.pairing.create()
        self.boot[0] += pairing.BOOT_TOLERANCE
        self.assertTrue(self.pairing.redeem(code))
        code = self.pairing.create()
        self.boot[0] -= pairing.BOOT_TOLERANCE + 1
        self.assertFalse(self.pairing.redeem(code))

    def test_check_does_not_ask_for_the_boot_time(self):
        # bbctl は毎秒 check する。そのたびに sysctl を起動しない
        code = self.pairing.create()
        calls = []
        watcher = Pairing(self.support, clock=self.clock, boot=lambda: calls.append(1) or self.boot[0])
        for _ in range(5):
            self.assertEqual(watcher.check(code), "pending")
        self.assertEqual(calls, [])

    def test_a_deadline_from_another_clock_domain_is_refused(self):
        # 再起動で単調な時計が戻った後の、古い期限（今から 300 秒より先）は信じない
        code = self.pairing.create()
        self.clock.now = 3.0
        self.assertFalse(self.pairing.redeem(code))

    def test_unknown_boot_time_fails_closed(self):
        code = self.pairing.create()
        def broken():
            raise OSError("no sysctl")
        self.assertFalse(Pairing(self.support, clock=self.clock, boot=broken).redeem(code))
        with self.assertRaises(OSError):
            Pairing(self.support, clock=self.clock, boot=broken).create()

    def test_garbage_pairing_file_is_just_a_failure(self):
        code = "A" * 22
        for junk in (b"", b"{", b"[]", b'"x"', b"{}", b'{"hash": 1, "deadline": 2, "boot": 3}',
                     json.dumps({"hash": hashlib.sha256(code.encode()).hexdigest(), "deadline": "soon", "boot": self.boot[0]}).encode(),
                     json.dumps({"hash": hashlib.sha256(code.encode()).hexdigest(), "deadline": 1300.0}).encode(),
                     json.dumps({"hash": hashlib.sha256(code.encode()).hexdigest(), "deadline": float("nan"), "boot": self.boot[0]}).encode()):
            self.support.mkdir(exist_ok=True)
            self.path.write_bytes(junk)
            self.assertFalse(self.pairing.redeem(code), junk)

    def test_redeem_without_a_pending_code_is_false(self):
        self.assertFalse(self.pairing.redeem("A" * 22))
        self.assertFalse(self.support.exists())                    # 読むだけで置き場を作らない

    def test_concurrent_redeems_let_exactly_one_through(self):
        for _ in range(5):
            code = self.pairing.create()
            results = []
            barrier = threading.Barrier(8)

            def work():
                barrier.wait()
                results.append(self.pairing.redeem(code))

            threads = [threading.Thread(target=work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(results.count(True), 1, results)

    def test_deeply_nested_json_is_just_a_bad_file(self):
        self.support.mkdir()
        self.path.write_bytes(b"[" * 5000)                         # RecursionError にならず、壊れたファイルとして扱う
        self.assertFalse(self.pairing.redeem("A" * 22))
        self.assertEqual(self.pairing.check("A" * 22), "expired")

    # ---- 別のプロセスの bbctl との競り合い（置き場の排他）

    def run_blocked(self, fn):
        """置き場の排他を握ったまま fn を別の糸で呼び、まだ終わっていない（待たされている）ことを確かめてから、握りを離す。"""
        done = threading.Event()
        result = []
        t = threading.Thread(target=lambda: (result.append(fn()), done.set()))
        return t, done, result

    def test_create_redeem_and_discard_wait_for_the_folder_lock(self):
        code = self.pairing.create()
        for name, fn in (("create", lambda: Pairing(self.support, clock=self.clock, boot=lambda: self.boot[0]).create()),
                         ("redeem", lambda: Pairing(self.support, clock=self.clock, boot=lambda: self.boot[0]).redeem(code)),
                         ("discard", lambda: Pairing(self.support, clock=self.clock, boot=lambda: self.boot[0]).discard(code))):
            t, done, _ = self.run_blocked(fn)
            with exclusive(self.support):                          # 別の bbctl が置き場を使っている間は
                t.start()
                self.assertFalse(done.wait(0.4), f"{name} が排他を待たなかった")
            self.assertTrue(done.wait(5), f"{name} が排他を離したあとも終わらない")
            t.join()
            code = self.pairing.create() if name != "discard" else code

    def test_a_wrong_code_does_not_take_the_lock(self):
        # 当てずっぽうの符号で、置き場の排他（bbctl・端末の登録が使う）を握らせない
        self.pairing.create()
        with exclusive(self.support):
            t, done, result = self.run_blocked(lambda: self.pairing.redeem("W" * 22))
            t.start()
            self.assertTrue(done.wait(2), "違う符号が排他を待った")
        t.join()
        self.assertEqual(result, [False])

    def test_redeem_does_not_delete_a_newer_code_written_while_it_waited(self):
        # 引き換えが排他を待つ間に、別の bbctl pair が新しい符号を書いた。読み直して、新しい方を消さない
        older = self.pairing.create()
        t, done, result = self.run_blocked(lambda: self.pairing.redeem(older))
        with exclusive(self.support):
            t.start()
            self.assertFalse(done.wait(0.3))
            data = {"hash": hashlib.sha256(b"N" * 22).hexdigest(), "deadline": self.clock() + 300, "boot": self.boot[0]}
            write_atomic(self.path, json.dumps(data).encode())      # 排他を握る別の bbctl が書き換える
        self.assertTrue(done.wait(5))
        t.join()
        self.assertEqual(result, [False])
        self.assertTrue(self.path.exists(), "新しい符号が消された")
        self.assertTrue(self.pairing.redeem("N" * 22))

    def test_discard_does_not_delete_a_newer_code_written_while_it_waited(self):
        older = self.pairing.create()
        t, done, _ = self.run_blocked(lambda: self.pairing.discard(older))
        with exclusive(self.support):
            t.start()
            self.assertFalse(done.wait(0.3))
            data = {"hash": hashlib.sha256(b"N" * 22).hexdigest(), "deadline": self.clock() + 300, "boot": self.boot[0]}
            write_atomic(self.path, json.dumps(data).encode())
        self.assertTrue(done.wait(5))
        t.join()
        self.assertTrue(self.path.exists())

    def test_redeem_and_discard_without_the_folder_do_not_make_it(self):
        self.assertFalse(self.pairing.redeem("A" * 22))
        self.pairing.discard("A" * 22)
        self.assertFalse(self.support.exists())

    # ---- bbctl が様子を見る

    def test_check_tells_pending_used_expired_and_replaced(self):
        code = self.pairing.create()
        self.assertEqual(self.pairing.check(code), "pending")
        self.clock.now += 100
        self.assertEqual(self.pairing.check(code), "pending")
        newer = self.pairing.create()
        self.assertEqual(self.pairing.check(code), "replaced")     # 別の符号に置き換わった
        self.assertEqual(self.pairing.check(newer), "pending")
        self.assertTrue(self.pairing.redeem(newer))
        self.assertEqual(self.pairing.check(newer), "used")        # 無くなった＝引き換えられた
        code = self.pairing.create()
        self.clock.now += 300
        self.assertEqual(self.pairing.check(code), "expired")

    def test_discard_removes_only_its_own_code(self):
        mine = self.pairing.create()
        theirs = self.pairing.create()                             # 後から別の bbctl が作った
        self.pairing.discard(mine)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.pairing.check(theirs), "pending")
        self.pairing.discard(theirs)
        self.assertFalse(self.path.exists())
        self.pairing.discard(theirs)                               # 無くても落ちない


class ClockTest(unittest.TestCase):
    def test_clock_is_the_same_across_processes(self):
        # 別のプロセスの時計と比べられること。time.monotonic() はそうとは限らない（このMacの /usr/bin/python3 はプロセスの
        # 起動からの秒を返す【実測 2026-10-31】）ので、別のプロセスの間では使わない
        out = subprocess.run([sys.executable, "-I", "-c",
                              "import sys; sys.path.insert(0, sys.argv[1]); from bblist.pairing import monotonic; print(monotonic())",
                              str(ROOT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        other, mine = float(out.stdout), monotonic()
        self.assertLess(abs(mine - other), 10.0, (mine, other))

    def test_clock_keeps_going_and_never_goes_back(self):
        a = monotonic()
        time.sleep(0.05)
        b = monotonic()
        self.assertGreater(b, a)

    def test_clock_refuses_instead_of_falling_back_to_a_per_process_clock(self):
        # CLOCK_MONOTONIC が使えないとき、time.monotonic()（このMacではプロセスごとの値）に黙って替えない
        with mock.patch.object(time, "clock_gettime", side_effect=OSError("no clock")), \
                mock.patch.object(time, "monotonic", side_effect=AssertionError("プロセスごとの時計に替えた")):
            with self.assertRaises(OSError):
                monotonic()
        with mock.patch.object(pairing.time, "CLOCK_MONOTONIC", create=True, new=None), \
                mock.patch.object(time, "monotonic", side_effect=AssertionError("プロセスごとの時計に替えた")):
            with self.assertRaises(OSError):
                monotonic()

    def test_pairing_with_no_usable_clock_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Pairing(Path(d), boot=lambda: 1)
            with mock.patch.object(time, "clock_gettime", side_effect=OSError("no clock")):
                with self.assertRaises(OSError):
                    p.create()                                      # 作れない（bbctl は失敗を言って終わる）
            code = p.create()
            with mock.patch.object(time, "clock_gettime", side_effect=OSError("no clock")):
                self.assertFalse(p.redeem(code))                    # 引き換えは通さない（落ちない）
                self.assertEqual(p.check(code), "expired")
            self.assertTrue(p.redeem(code))                         # 時計が戻れば、符号はまだ使える（一時の不調で使い切らない）

    def test_boot_time_matches_sysctl(self):
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout
        self.assertEqual(boot_time(), parse_boottime(out))
        self.assertGreater(boot_time(), 1_500_000_000)

    def test_parse_boottime(self):
        self.assertEqual(parse_boottime("{ sec = 1792343805, usec = 432273 } Mon Oct 19 02:16:45 2026\n"), 1792343805)
        for bad in ("", "nothing", "{ usec = 5 }", "{ sec = x }"):
            with self.assertRaises(OSError, msg=bad):
                parse_boottime(bad)

    def test_boot_time_failure_is_oserror(self):
        with mock.patch.object(pairing.subprocess, "run", side_effect=subprocess.TimeoutExpired("sysctl", 5)):
            with self.assertRaises(OSError):
                boot_time()
        with mock.patch.object(pairing.subprocess, "run",
                               return_value=subprocess.CompletedProcess(["sysctl"], 1, stdout="", stderr="")):
            with self.assertRaises(OSError):
                boot_time()


class PairLimiterTest(unittest.TestCase):
    """組む口の回数の上限：送り元ごとに1分に5回、全体で1分に30回。"""

    def test_defaults_are_5_per_source_and_30_in_total_per_minute(self):
        limiter = PairLimiter()
        self.assertEqual((limiter.per_ip, limiter.total, limiter.window), (5, 30, 60.0))

    def test_one_source_gets_five_per_minute(self):
        clock = Clock(100.0)
        limiter = PairLimiter(clock=clock)
        self.assertEqual([limiter.allow("100.64.0.1") for _ in range(5)], [True] * 5)
        self.assertFalse(limiter.allow("100.64.0.1"))
        self.assertFalse(limiter.allow("100.64.0.1"))
        self.assertTrue(limiter.allow("100.64.0.2"))               # 別の送り元は別の数え方
        clock.now += 59
        self.assertFalse(limiter.allow("100.64.0.1"))
        clock.now += 2                                             # 最初の5回が1分の外に出た
        self.assertEqual([limiter.allow("100.64.0.1") for _ in range(5)], [True] * 5)
        self.assertFalse(limiter.allow("100.64.0.1"))

    def test_the_total_is_thirty_per_minute_across_sources(self):
        clock = Clock(0.0)
        limiter = PairLimiter(clock=clock)
        for n in range(6):                                         # 6 つの送り元が 5 回ずつ＝30 回。どれも送り元の上限の中
            self.assertEqual([limiter.allow(f"100.64.0.{n}") for _ in range(5)], [True] * 5)
        self.assertFalse(limiter.allow("100.64.0.99"))             # 新しい送り元も、全体の上限で断る
        self.assertFalse(limiter.allow("100.64.0.0"))
        clock.now += 61
        self.assertTrue(limiter.allow("100.64.0.99"))

    def test_a_refused_attempt_is_not_counted_anywhere(self):
        clock = Clock(0.0)
        limiter = PairLimiter(per_ip=2, total=3, clock=clock)
        self.assertTrue(limiter.allow("a") and limiter.allow("a"))
        for _ in range(50):                                        # 送り元の上限で断られ続けても、全体の枠を使わない
            clock.now += 0.1
            self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))                        # 全体の 3 回目は、a の断られた分に取られていない
        self.assertFalse(limiter.allow("c"))                       # 全体の上限（3）に達した
        clock.now = 61.0
        self.assertTrue(limiter.allow("a"))                        # 断られ続けても待ちは延びない

    def test_memory_does_not_grow_with_many_sources(self):
        clock = Clock(0.0)
        limiter = PairLimiter(per_ip=5, total=1_000_000, clock=clock)
        for n in range(5000):
            limiter.allow(f"src{n}")
            clock.now += 0.1                                       # 500 秒＝窓の 8 倍以上
        self.assertLess(len(limiter._by_ip), 1000)

    def test_is_safe_across_threads(self):
        limiter = PairLimiter(per_ip=1000, total=100, clock=Clock(0.0))
        results = []
        threads = [threading.Thread(target=lambda: results.extend(limiter.allow("x") for _ in range(50))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 100)


class RemoveStaleQrTest(unittest.TestCase):
    """サーバーの起動時の掃除：5分より古い pair/qr-*.png（SIGKILL などで残ったもの）だけを消す。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.support = Path(self._tmp.name)
        self.folder = self.support / "pair"
        self.folder.mkdir()

    def png(self, name: str, age: float) -> Path:
        p = self.folder / name
        p.write_bytes(b"x")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_removes_only_old_qr_pngs(self):
        old, fresh = self.png("qr-1.png", 600), self.png("qr-2.png", 30)
        other, odd = self.png("notes.txt", 9999), self.png("qr-3.txt", 9999)
        self.assertEqual(remove_stale_qr(self.support), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists() and other.exists() and odd.exists())   # 生きている組みの PNG・ほかのファイルは残す

    def test_age_limit_is_the_code_lifetime_plus_a_margin(self):
        self.assertGreaterEqual(pairing.STALE_QR_AGE, pairing.CODE_TTL)
        self.png("qr-1.png", pairing.STALE_QR_AGE - 5)
        self.assertEqual(remove_stale_qr(self.support), 0)
        self.png("qr-2.png", pairing.STALE_QR_AGE + 5)
        self.assertEqual(remove_stale_qr(self.support), 1)

    def test_a_link_is_removed_not_followed(self):
        target = self.support / "target.txt"
        target.write_text("keep", encoding="utf-8")
        link = self.folder / "qr-9.png"
        link.symlink_to(target)
        t = time.time() - 9999
        os.utime(link, (t, t), follow_symlinks=False)
        remove_stale_qr(self.support)
        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_no_folder_or_a_file_in_its_place_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(remove_stale_qr(Path(d)), 0)
            (Path(d) / "pair").write_text("x", encoding="utf-8")
            self.assertEqual(remove_stale_qr(Path(d)), 0)
        self.assertEqual(remove_stale_qr(self.support / "nowhere"), 0)


if __name__ == "__main__":
    unittest.main()
