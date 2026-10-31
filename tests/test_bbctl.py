"""bbctl.py の試験。python3 -m unittest tests.test_bbctl -v

bbctl pair（QR の PNG を置き場に書き、組めるか5分たつまで見る）・devices・revoke。
本物の Tailscale と本物の `open` には触らない（--ip / --host を渡し、--no-open か、受けた引数を書き残すだけの偽の open を使う）。
符号・鍵は試験の中で決めた値にして、端末でない出力・記録のどこにも出ないことを確かめる。
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import logging
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bbctl
from bblist import qr
from bblist.devices import DeviceStore
from bblist.pairing import Pairing
from bblist.secrets_store import load_or_create_secret, server_id
from tests.test_qr import decode_png

ROOT = Path(__file__).resolve().parents[1]
CODE = "KnownPairingCode_0123x"[:22]                         # secrets.token_urlsafe(16) と同じ22字
IP, HOSTNAME = "100.101.102.103", "mac.example.ts.net"
FAKE_TAILSCALE = ('#!/bin/sh\ncase "$*" in\n'
                  '  "status --json") echo \'{"BackendState":"Running","CurrentTailnet":{"MagicDNSEnabled":true},'
                  f'"Self":{{"DNSName":"{HOSTNAME}."}}}}\' ;;\n'
                  '  "ip -4") echo 127.0.0.1 ;;\n  *) exit 1 ;;\nesac\n')
REAL_TOKEN_URLSAFE = secrets.token_urlsafe


def default_stop_signals():
    """子プロセスの preexec_fn：止める合図（INT・TERM・HUP・QUIT）の扱いを既定に戻してから exec する。

    試験を背景で走らせる（`cmd &`・nohup）と、シェルが INT・QUIT（nohup なら HUP も）を無視にしたまま子へ引き継ぐ。
    bbctl は「無視されていた合図は無視のまま」（nohup した人の意図を尊重）なので、引き継いだ無視のままでは、
    合図を送る試験が届かず落ちる。試験が確かめたいのは「既定の状態で起動した bbctl が合図で片付ける」ことなので、
    親の状態に依らないよう既定に戻す。"""
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)


def fixed_code(nbytes=None):
    """組む符号（16 バイト）だけ決めた値にする。端末の鍵（32 バイト）は本物の乱数のまま。"""
    return CODE if nbytes == 16 else REAL_TOKEN_URLSAFE(nbytes)


class Clock:
    def __init__(self, now: float = 5000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeStdout(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class HealthStub:
    """本物のサーバーの代わりに /v1/health へ答える小さなサーバー（loopback）。body は答えの中身、status は状態コード。"""

    def __init__(self, status: int = 200, body: bytes = b'{"ok": true, "apiVersion": 3}', delay: float = 0.0):
        stub = self
        self.status, self.body, self.delay, self.paths = status, body, delay, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                stub.paths.append(self.path)
                if stub.delay:
                    time.sleep(stub.delay)
                try:
                    self.send_response(stub.status)
                    self.send_header("Content-Length", str(len(stub.body)))
                    self.end_headers()
                    self.wfile.write(stub.body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class BbctlCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.support = self.tmp / "support"
        self.clock = Clock()
        self.sleeps = 0
        # 本物の tailscale は呼ばない。MagicDNS の答えは self.dns（有効か, この Mac の名前）、health の答えは self.health_answer
        # （None なら答えた、文ならその理由で答えなかった）。呼ばれた引数は dns_calls・health_calls に残る
        self.dns, self.dns_calls = (True, HOSTNAME), []
        self.health_answer, self.health_calls = None, []

        def fake_dns(*args):
            self.dns_calls.append(args)
            return self.dns

        patcher = mock.patch.object(bbctl, "magicdns_status", side_effect=fake_dns)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._stub = None

    def health(self, ip, port):
        self.health_calls.append((ip, port))
        return self.health_answer

    def health_stub(self, **kw) -> HealthStub:
        """loopback で /v1/health に答える本物の小さなサーバー（本物のプロセスで bbctl pair を走らせる試験が使う）。"""
        stub = HealthStub(**kw)
        self.addCleanup(stub.close)
        return stub

    def live_args(self, support=None, *extra: str) -> list:
        """本物のプロセスで走らせる引数。loopback の小さなサーバーに health を当てる（QR の中身は見ない試験用）。"""
        if self._stub is None:
            self._stub = self.health_stub()
        return ["pair", "--support-dir", str(support or self.support), "--ip", "127.0.0.1", "--api-port", str(self._stub.port),
                "--host", HOSTNAME, "--no-open", *extra]

    def live_env(self) -> dict:
        """本物のプロセス用の環境。PATH の先頭に、MagicDNS の名前を答える偽の tailscale を置く（本物の tailscale は呼ばない）。"""
        folder = self.tmp / "live-bin"
        if not folder.exists():
            folder.mkdir()
            (folder / "tailscale").write_text(FAKE_TAILSCALE, encoding="utf-8")
            (folder / "tailscale").chmod(0o755)
        return {**os.environ, "PATH": f"{folder}:{os.environ['PATH']}"}

    def common(self, *extra: str) -> list:
        return ["--support-dir", str(self.support), *extra]

    def pair_args(self, *extra: str) -> list:
        return ["pair", *self.common("--ip", IP, "--host", HOSTNAME, "--no-open", *extra)]

    def run_main(self, argv, *, tty=False, on_sleep=None):
        """bbctl.main を、単調な時計と sleep を差し替えて走らせる。sleep のたびに時計を進め、on_sleep(回数) を呼ぶ。
        (終了コード, 標準出力, 標準エラー, 記録の行) を返す。"""
        out, err, rec = FakeStdout(tty), io.StringIO(), Records()

        def sleep(seconds):
            self.sleeps += 1
            self.clock.now += seconds
            if on_sleep:
                on_sleep(self.sleeps)

        root = logging.getLogger()
        root.addHandler(rec)
        level = root.level
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch("secrets.token_urlsafe", side_effect=fixed_code):
                try:
                    code = bbctl.main(argv, clock=self.clock, sleep=sleep, health=self.health)
                except SystemExit as e:                                   # argparse の使い方の誤り（終了コード 2）
                    code = e.code
        finally:
            root.removeHandler(rec)
            root.setLevel(level)
        return code, out.getvalue(), err.getvalue(), rec.lines

    def server_pairs(self, name="Test iPad"):
        """動いているサーバーが POST /v1/pair を受けたときにすること（符号を引き換え、端末を足す）。(deviceId, token) を返す。"""
        self.assertTrue(Pairing(self.support, clock=self.clock).redeem(CODE))
        return DeviceStore(self.support).add(name)

    def assert_png_holds(self, png: bytes, text: str) -> None:
        """PNG の画素が、text を QR にした行列（余白つき）と全部合う。"""
        m = qr.matrix(text)
        width, height, pixels = decode_png(png)
        size = (len(m) + 2 * qr.BORDER) * qr.SCALE
        self.assertEqual((width, height), (size, size))
        for y, row in enumerate(pixels):
            for x, dark in enumerate(row):
                mx, my = x // qr.SCALE - qr.BORDER, y // qr.SCALE - qr.BORDER
                if dark != (0 <= mx < len(m) and 0 <= my < len(m) and m[my][mx]):
                    self.fail(f"画素 {(x, y)} が行列と合わない")

    def pair_url(self, *, host=HOSTNAME, ip=IP, api=8772, file=8773) -> str:
        sid = server_id(load_or_create_secret(self.support))
        return f"bridgeboard://pair?v=1&host={host}&ip={ip}&api={api}&file={file}&code={CODE}&sid={sid}"

    def pngs(self) -> list:
        d = self.support / "pair"
        return sorted(d.glob("qr-*.png")) if d.is_dir() else []


class PairTest(BbctlCase):
    def test_pair_success_prints_the_device_name(self):
        got = {}

        def on_sleep(n):
            if n == 3:
                got["dev"] = self.server_pairs("Tomo's iPad")

        code, out, err, _ = self.run_main(self.pair_args(), on_sleep=on_sleep)
        self.assertEqual(code, 0, err)
        self.assertIn("paired: Tomo's iPad\n", out)
        self.assertEqual(out.strip().splitlines()[-1], "paired: Tomo's iPad")
        self.assertEqual(self.pngs(), [])                                   # 組めたら PNG を消す
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertNotIn(got["dev"][1], out + err)                           # 端末の鍵を出さない

    def test_pair_expired(self):
        code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().splitlines()[-1], "expired")
        self.assertGreaterEqual(self.clock.now, 5000.0 + 300)                # 5分たつまで見ていた
        self.assertLess(self.clock.now, 5000.0 + 310)
        self.assertEqual(self.pngs(), [])                                   # 切れたら PNG を消す
        self.assertFalse((self.support / "pairing.json").exists())          # 切れた符号も置き場に残さない
        self.assertEqual(DeviceStore(self.support).list(), [])

    def test_polls_once_a_second(self):
        seen = []
        self.run_main(self.pair_args(), on_sleep=lambda n: seen.append(self.clock.now))
        gaps = {round(b - a, 6) for a, b in zip(seen, seen[1:])}
        self.assertEqual(gaps, {1.0})

    def test_a_newer_pairing_ends_this_one_without_removing_the_newer_code(self):
        newer = {}

        def on_sleep(n):
            if n == 2:                                                       # 別の bbctl pair が後から始まった
                with mock.patch("secrets.token_urlsafe", return_value="N" * 22):
                    newer["code"] = Pairing(self.support, clock=self.clock).create()

        code, out, err, _ = self.run_main(self.pair_args(), on_sleep=on_sleep)
        self.assertEqual(code, 1)
        last = out.strip().splitlines()[-1]
        self.assertRegex(last, r"\Areplaced: another .* was started")       # 切れたのではなく、別の pair が始まった
        self.assertNotIn("expired", out)
        self.assertNotIn(newer["code"], out + err)                          # 新しい方の符号は出さない
        self.assertTrue((self.support / "pairing.json").exists())           # 新しい方の符号は消さない
        self.assertEqual(Pairing(self.support, clock=self.clock).check(newer["code"]), "pending")
        self.assertEqual(self.pngs(), [])

    def test_qr_png_is_written_in_a_700_folder_and_holds_the_pairing_url(self):
        seen = {}

        def on_sleep(n):
            if n == 1:
                files = self.pngs()
                seen["files"] = files
                seen["dir_mode"] = stat.S_IMODE(os.stat(self.support / "pair").st_mode)
                seen["png_mode"] = stat.S_IMODE(os.stat(files[0]).st_mode)
                seen["png"] = files[0].read_bytes()
                seen["pairing_mode"] = stat.S_IMODE(os.stat(self.support / "pairing.json").st_mode)
                seen["secret_mode"] = stat.S_IMODE(os.stat(self.support / "secret").st_mode)
                seen["support_mode"] = stat.S_IMODE(os.stat(self.support).st_mode)

        code, out, err, _ = self.run_main(self.pair_args("--api-port", "18772", "--file-port", "18773"), on_sleep=on_sleep)
        self.assertEqual(len(seen["files"]), 1)
        self.assertRegex(seen["files"][0].name, r"\Aqr-\d+\.png\Z")
        self.assertEqual(seen["dir_mode"], 0o700)
        self.assertEqual(seen["png_mode"], 0o600)
        self.assertEqual((seen["pairing_mode"], seen["secret_mode"], seen["support_mode"]), (0o600, 0o600, 0o700))
        self.assert_png_holds(seen["png"], self.pair_url(api=18772, file=18773))   # 中身が違えば画素が合わない
        self.assertRegex(server_id(load_or_create_secret(self.support)), r"\A[0-9a-f]{16}\Z")

    def test_secret_files_mode_600(self):
        # 秘密のファイル（secret・pairing.json・devices.json・QR の PNG）は 600、置き場と pair/ のフォルダは 700
        seen = {}

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)

        def on_sleep(n):
            if n == 1:
                seen["pending"] = {p.name: mode(p) for p in (self.support / "secret", self.support / "pairing.json", *self.pngs())
                                   if not p.name.startswith("qr-")}
                seen["png"] = mode(self.pngs()[0])
                seen["dirs"] = (mode(self.support), mode(self.support / "pair"))
            if n == 2:
                self.server_pairs()

        code, out, err, _ = self.run_main(self.pair_args(), on_sleep=on_sleep)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["pending"], {"secret": 0o600, "pairing.json": 0o600})
        self.assertEqual((seen["png"], seen["dirs"]), (0o600, (0o700, 0o700)))
        self.assertEqual(mode(self.support / "devices.json"), 0o600)
        self.assertEqual(sorted(p.name for p in self.support.iterdir()), ["devices.json", "pair", "secret"])   # 一時ファイルを残さない

    def test_pair_refuses_a_broken_devices_file_before_making_a_code(self):
        self.support.mkdir()
        (self.support / "devices.json").write_text("{broken", encoding="utf-8")
        code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(code, 1)
        self.assertIn("devices.json", err)
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertEqual(self.pngs(), [])
        self.assertEqual((self.support / "devices.json").read_text(), "{broken")

    def test_default_ports(self):
        seen = {}
        self.run_main(self.pair_args(), on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assert_png_holds(seen["png"], self.pair_url())

    def test_finds_the_ip_and_the_name_when_not_given(self):
        seen = {}
        argv = ["pair", *self.common("--no-open")]
        self.dns = (True, "found.example.ts.net")
        with mock.patch.object(bbctl, "tailscale_ip", return_value="100.64.0.9"):
            self.run_main(argv, on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assert_png_holds(seen["png"], self.pair_url(host="found.example.ts.net", ip="100.64.0.9"))
        self.assertEqual(self.health_calls, [("100.64.0.9", 8772)])         # QR に入れる IP とポートで、health を1回

    def assert_refused_before_a_code(self, code, out, err, *words):
        self.assertEqual(code, 1, (out, err))
        for word in words:
            self.assertIn(word, err)
        self.assertFalse((self.support / "pairing.json").exists())           # 符号を作る前に断る
        self.assertEqual(self.pngs(), [])
        self.assertEqual(self.health_calls, [])                              # health も呼ばない
        self.assertNotIn("QR image", out)

    def test_without_a_magicdns_name_pair_says_how_to_fix_it_before_making_a_code(self):
        # アプリは host が空の QR を断る。QR を作ってから断られるより、符号を作る前に直し方を言う
        for dns in ((True, None), (None, None), (False, None)):
            with self.subTest(dns=dns):
                self.dns = dns
                code, out, err, _ = self.run_main(["pair", *self.common("--no-open", "--ip", IP)])
                self.assert_refused_before_a_code(code, out, err, "MagicDNS", "https://login.tailscale.com/admin/dns",
                                                  "bbctl pair")
                self.assertTrue(err.startswith("error: "), err)

    def test_with_magicdns_turned_off_pair_says_so_before_making_a_code(self):
        self.dns = (False, HOSTNAME)                                         # 名前は見えても、MagicDNS が無効
        code, out, err, _ = self.run_main(["pair", *self.common("--no-open", "--ip", IP)])
        self.assert_refused_before_a_code(code, out, err, "turned off", "https://login.tailscale.com/admin/dns")

    def test_host_flag_that_matches_the_name_tailscale_gives_is_silent(self):
        for given in (HOSTNAME, HOSTNAME.upper()):
            code, out, err, _ = self.run_main(["pair", *self.common("--no-open", "--ip", IP, "--host", given)])
            self.assertEqual((code, err), (1, ""), given)                    # 切れて終わる。警告は無い

    def test_host_flag_that_differs_from_the_name_tailscale_gives_is_warned_but_used(self):
        seen = {}
        argv = ["pair", *self.common("--no-open", "--ip", IP, "--host", "other.example.ts.net")]
        code, out, err, _ = self.run_main(argv, on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assertIn("warning: --host other.example.ts.net differs from this Mac's MagicDNS name", err)
        self.assertIn("421", err)
        self.assertIn(HOSTNAME, err)
        self.assert_png_holds(seen["png"], self.pair_url(host="other.example.ts.net"))   # 利用者が決めた値は使う

    def test_host_flag_is_warned_too_when_tailscale_gives_no_name(self):
        self.dns = (None, None)
        argv = ["pair", *self.common("--no-open", "--ip", IP, "--host", HOSTNAME)]
        seen = {}
        code, out, err, _ = self.run_main(argv, on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assertIn("warning: could not read this Mac's MagicDNS name", err)
        self.assert_png_holds(seen["png"], self.pair_url())

    def test_host_help_says_the_server_accepts_only_its_own_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            bbctl.main(["pair", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("421", text)
        self.assertIn("--magicdns-name", text)
        self.assertIn("MagicDNS", text)

    # ---- QR に入れる IP とポートで、サーバーが答えるか（符号を作る前に）

    def test_a_server_that_does_not_answer_stops_pair_before_making_a_code(self):
        self.health_answer = "no answer"
        code, out, err, _ = self.run_main(self.pair_args("--api-port", "18772"))
        self.assertEqual(code, 1)
        self.assertEqual(self.health_calls, [(IP, 18772)])                   # QR に入れる IP とポートで1回
        self.assertIn(f"http://{IP}:18772/v1/health", err)
        self.assertIn("no answer", err)
        self.assertIn("listserver.log", err)
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertEqual(self.pngs(), [])
        self.assertNotIn("QR image", out)
        self.assertEqual(DeviceStore(self.support).list(), [])

    def test_the_health_check_happens_once_with_the_default_and_the_configured_ports(self):
        self.run_main(self.pair_args())
        self.write_config({"apiPort": 19772, "filePort": 19773})
        self.run_main(self.pair_args())
        self.assertEqual(self.health_calls, [(IP, 8772), (IP, 19772)])

    def test_the_real_health_check_refuses_when_nothing_listens(self):
        # 本物の check_health で（loopback の空きポート）。符号も画像もできず、1 で終わる
        stub = HealthStub()
        port = stub.port
        stub.close()                                                         # 閉じた＝誰も待っていない
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bbctl.main(["pair", *self.common("--no-open", "--ip", "127.0.0.1", "--host", HOSTNAME, "--api-port", str(port))],
                              clock=self.clock, sleep=lambda s: None)
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn("did not answer", err.getvalue())
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertEqual(self.pngs(), [])

    def write_config(self, data, mode=0o600) -> Path:
        self.support.mkdir(mode=0o700, exist_ok=True)
        p = self.support / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(p, mode)
        return p

    def test_ports_come_from_config_json_when_not_given(self):
        self.write_config({"apiPort": 19772, "filePort": 19773})
        seen = {}
        self.run_main(self.pair_args(), on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assert_png_holds(seen["png"], self.pair_url(api=19772, file=19773))

    def test_port_flags_win_over_config_json(self):
        self.write_config({"apiPort": 19772, "filePort": 19773})
        seen = {}
        self.run_main(self.pair_args("--api-port", "18772"), on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assert_png_holds(seen["png"], self.pair_url(api=18772, file=19773))

    def test_a_config_json_that_others_can_write_is_not_trusted_and_is_reported(self):
        self.write_config({"apiPort": 19772, "filePort": 19773}, mode=0o666)
        seen = {}
        code, out, err, _ = self.run_main(self.pair_args(),
                                          on_sleep=lambda n: seen.setdefault("png", self.pngs()[0].read_bytes()))
        self.assert_png_holds(seen["png"], self.pair_url())              # 既定の口
        self.assertIn("warning: config.json: not read (writable by group or others", err)

    def test_the_configured_tailscale_is_the_one_asked(self):
        self.write_config({"tailscale": "/opt/x/tailscale"})
        argv = ["pair", *self.common("--no-open")]
        with mock.patch.object(bbctl, "tailscale_ip", return_value="100.64.0.9") as ip:
            self.run_main(argv)
        self.assertEqual((ip.call_args.args, self.dns_calls), (("/opt/x/tailscale",), [("/opt/x/tailscale",)]))

    def test_bbctl_does_not_import_the_server(self):
        # bbctl は別のプロセス。サーバー（listserver）を import しない（取り方は bblist.discover に1つ）
        code = "import bbctl, sys; sys.exit(1 if 'listserver' in sys.modules else 0)"
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_ip_means_no_pairing(self):
        with mock.patch.object(bbctl, "tailscale_ip", return_value=None):
            code, out, err, _ = self.run_main(["pair", *self.common("--no-open")])
        self.assertEqual(code, 1)
        self.assertIn("Tailscale IP", err)
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertEqual(self.pngs(), [])

    def test_bad_ip_is_refused(self):
        for bad in ("not-an-ip", "8.8.8.8", "300.1.1.1", "100.64.0.1/8", ""):
            code, out, err, _ = self.run_main(["pair", *self.common("--no-open", "--ip", bad)])
            self.assertEqual(code, 2, bad)
            self.assertFalse((self.support / "pairing.json").exists())

    def test_bad_host_and_ports_are_refused(self):
        for opts in (["--host", "a b"], ["--host", "x&y=z"], ["--host", ""], ["--host", "a" * 300],
                     ["--api-port", "0"], ["--api-port", "70000"], ["--file-port", "x"], ["--file-port", "-1"]):
            code, out, err, _ = self.run_main(["pair", *self.common("--no-open", "--ip", IP, *opts)])
            self.assertEqual(code, 2, opts)
        self.assertFalse((self.support / "pairing.json").exists())

    def test_stale_pngs_are_removed_at_start(self):
        (self.support / "pair").mkdir(parents=True, mode=0o700)
        old = self.support / "pair" / "qr-1.png"
        old.write_bytes(b"x")
        keep = self.support / "pair" / "notes.txt"
        keep.write_text("mine", encoding="utf-8")
        seen = {}
        self.run_main(self.pair_args(), on_sleep=lambda n: seen.setdefault("files", [p.name for p in self.pngs()]))
        self.assertNotIn("qr-1.png", seen["files"])
        self.assertEqual(len(seen["files"]), 1)
        self.assertTrue(keep.exists())

    # ---- 開く

    def stub_open(self) -> Path:
        calls = self.tmp / "open.calls"
        stub = self.tmp / "open"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)
        return calls

    def test_pair_opens_the_png_unless_no_open(self):
        calls = self.stub_open()
        seen = {}
        argv = ["pair", *self.common("--ip", IP, "--host", HOSTNAME)]
        with mock.patch.object(bbctl, "OPEN_CMD", str(self.tmp / "open")):
            self.run_main(argv, on_sleep=lambda n: seen.setdefault("png", str(self.pngs()[0])))
        self.assertEqual(calls.read_text(encoding="utf-8").strip(), seen["png"])   # 開いたのは置き場の PNG
        calls.unlink()
        with mock.patch.object(bbctl, "OPEN_CMD", str(self.tmp / "open")):
            self.run_main(self.pair_args())                                       # --no-open
        self.assertFalse(calls.exists())

    def test_default_open_command_is_the_system_open(self):
        self.assertEqual(bbctl.OPEN_CMD, "/usr/bin/open")

    def test_open_failure_is_reported_without_the_code_and_pairing_goes_on(self):
        argv = ["pair", *self.common("--ip", IP, "--host", HOSTNAME)]
        with mock.patch.object(bbctl, "OPEN_CMD", str(self.tmp / "no-such-open")):
            code, out, err, _ = self.run_main(argv)
        self.assertEqual(code, 1)                                                  # 開けなくても待つ（切れて expired）
        self.assertIn("Could not open", err)
        self.assertNotIn(CODE, out + err)

    # ---- 出さないもの

    def test_no_secret_in_non_tty_output(self):
        # 端末でない出力（Claude Code が走らせたとき）と記録に、符号・鍵が出ない
        got = {}
        code, out, err, records = self.run_main(
            self.pair_args(), on_sleep=lambda n: got.setdefault("dev", self.server_pairs()) if n == 2 else None)
        self.assertEqual(code, 0, err)
        dev, token = got["dev"]
        text = "\n".join([out, err, *records])
        for secret in (CODE, token, "bridgeboard://", load_or_create_secret(self.support).hex()):
            self.assertNotIn(secret, text)
        self.assertNotRegex(out, "[▀▄█]")                                        # QR を描かない
        self.assertIn(str(self.support / "pair"), out)                           # 画像の置き場所は教える

    def test_no_secret_in_non_tty_output_when_run_as_a_real_process(self):
        # 本物のプロセス（標準出力・標準エラーがパイプ）で。符号は包みの中で決めた値にする
        wrapper = ("import secrets, sys\n"
                   "real = secrets.token_urlsafe\n"
                   f"secrets.token_urlsafe = lambda n=None: {CODE!r} if n == 16 else real(n)\n"
                   "import bbctl\n"
                   "sys.exit(bbctl.main(sys.argv[1:]))\n")
        proc = subprocess.Popen([sys.executable, "-c", wrapper, *self.live_args()], cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.live_env())
        try:
            deadline = time.monotonic() + 15
            while not (self.support / "pairing.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.support / "pairing.json").exists(), "pairing.json ができない")
            while not self.pngs() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pngs(), "PNG ができない")
            _, token = self.server_pairs_real_clock()
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip().splitlines()[-1], "paired: Test iPad")
        for secret in (CODE, token, "bridgeboard://"):
            self.assertNotIn(secret, out + err)
        self.assertEqual(err, "")

    def server_pairs_real_clock(self):
        self.assertTrue(Pairing(self.support).redeem(CODE))
        return DeviceStore(self.support).add("Test iPad")

    def test_tty_run_also_draws_the_qr_but_still_not_the_code(self):
        # 人が端末から走らせたとき（isatty）だけ、端末にも QR を描く。符号・URL の文字は出さない
        code, out, err, records = self.run_main(
            self.pair_args(), tty=True, on_sleep=lambda n: self.server_pairs() if n == 2 else None)
        self.assertEqual(code, 0, err)
        self.assertRegex(out, "[▀▄█]")
        for secret in (CODE, "bridgeboard://"):
            self.assertNotIn(secret, out + err + "\n".join(records))
        self.assertEqual(out.strip().splitlines()[-1], "paired: Test iPad")
        # 描いた QR は、PNG と同じ行列
        lines = [l for l in out.splitlines() if re.fullmatch(r"(\x1b\[30;47m)[ ▀▄█]+\x1b\[0m", l)]
        self.assertEqual(lines, qr.terminal_art(qr.matrix(self.pair_url())).split("\n"))

    def run_and_signal(self, signum, *, close_stdout=False):
        """本物のプロセスで bbctl pair を走らせ、PNG ができたところで signum を送る。(終了コード, 標準出力, 標準エラー)。
        close_stdout は、端末の窓を閉じたときのように、標準出力の受け手が先に消えた場合。"""
        proc = subprocess.Popen([sys.executable, str(ROOT / "bbctl.py"), *self.live_args()], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=default_stop_signals, env=self.live_env())
        try:
            deadline = time.monotonic() + 15
            while not (self.pngs() and (self.support / "pairing.json").exists()) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pngs(), "PNG ができない")
            time.sleep(0.3)                                                    # 待つ輪に入るのを待つ
            if close_stdout:
                proc.stdout.close()
            proc.send_signal(signum)
            out, err = proc.communicate(timeout=15)
        except ValueError:                                                     # 閉じた標準出力を communicate が読もうとした
            out, err = "", proc.stderr.read()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        return proc.returncode, out, err

    def test_signals_remove_the_png_and_the_pending_code(self):
        # 終わりの合図のどれでも（端末の窓を閉じる SIGHUP も）、PNG と、まだ有効な符号を残さない
        for name in ("SIGTERM", "SIGHUP", "SIGQUIT", "SIGINT"):
            signum = getattr(signal, name)
            with self.subTest(signal=name):
                code, out, err = self.run_and_signal(signum)
                self.assertEqual(self.pngs(), [], name)
                self.assertFalse((self.support / "pairing.json").exists(), name)
                self.assertEqual(code, 128 + signum, (name, err))               # 合図で終わった印（128 + 番号）
                self.assertIn("cancelled", out)
                self.assertEqual(err, "", name)

    def test_several_different_signals_at_once_still_clean_up(self):
        # 窓を閉じる（HUP）・終了（TERM）・QUIT が、ほとんど同時に届く。どの順で受けても PNG と符号を残さず、
        # 受けた合図のどれかの 128+番号で、トレースバックなしで終わる。本物のプロセスで何度も繰り返す
        sequences = [("SIGHUP", "SIGTERM", "SIGQUIT"), ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")]
        rounds, batch = 20, 10                                                 # 並び方ごとに20回＝全部で40回（10本ずつ並べて走らせる）
        bad = []
        for names in sequences:
            sent = [getattr(signal, n) for n in names]
            allowed = {128 + n for n in sent}
            for start in range(0, rounds, batch):
                runs = []
                for i in range(batch):
                    support = self.tmp / f"multi-{'-'.join(names)}-{start + i}"
                    argv = self.live_args(support)
                    proc = subprocess.Popen([sys.executable, str(ROOT / "bbctl.py"), *argv], cwd=ROOT, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                            preexec_fn=default_stop_signals, env=self.live_env())
                    runs.append((proc, support))
                try:
                    for proc, support in runs:
                        deadline = time.monotonic() + 20
                        while not list((support / "pair").glob("qr-*.png")) and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(list((support / "pair").glob("qr-*.png")), "PNG ができない")
                        for signum in sent:                                    # 間を空けずに続けて送る
                            os.kill(proc.pid, signum)
                    for proc, support in runs:
                        out, err = proc.communicate(timeout=30)
                        left = list((support / "pair").glob("qr-*.png")) + list(support.glob("pairing.json"))
                        if left or proc.returncode not in allowed or err:
                            bad.append((names, proc.returncode, [p.name for p in left], err[-200:]))
                finally:
                    for proc, _ in runs:
                        if proc.poll() is None:
                            proc.kill()
                            proc.communicate()
        self.assertEqual(bad, [], f"{len(bad)} 回、片付けが途中で終わった／変な終わり方をした")

    def test_sighup_with_the_terminal_already_gone_still_cleans_up(self):
        # 窓を閉じると、標準出力の先が消えた状態で SIGHUP が来る。印字に失敗しても片付けて、静かに終わる
        code, out, err = self.run_and_signal(signal.SIGHUP, close_stdout=True)
        self.assertEqual(self.pngs(), [])
        self.assertFalse((self.support / "pairing.json").exists())
        self.assertEqual(code, 128 + signal.SIGHUP, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Exception ignored", err)

    def test_a_signal_that_was_ignored_is_left_ignored(self):
        # nohup で走らせた人の意図（窓を閉じても続ける）は尊重する。続けても、5分で切れれば片付けて終わる（時計の試験で別に確かめる）
        proc = subprocess.Popen(["/bin/sh", "-c", f'trap "" HUP; exec "{sys.executable}" "{ROOT / "bbctl.py"}" "$@"', "sh",
                                 *self.live_args()], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, preexec_fn=default_stop_signals, env=self.live_env())
        try:
            deadline = time.monotonic() + 15
            while not self.pngs() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pngs())
            time.sleep(0.3)
            proc.send_signal(signal.SIGHUP)
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "無視していた SIGHUP で終わった")
            proc.send_signal(signal.SIGTERM)                                   # 後片付けは SIGTERM で
            proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(self.pngs(), [])
        self.assertFalse((self.support / "pairing.json").exists())

    def test_every_exit_path_removes_the_png_and_the_code(self):
        # 組めた・切れた・開けなかった・印字に失敗した・想定外の例外、のどれでも
        def png_gone():
            self.assertEqual(self.pngs(), [])
            self.assertFalse((self.support / "pairing.json").exists())

        self.run_main(self.pair_args(), on_sleep=lambda n: self.server_pairs() if n == 2 else None)
        png_gone()                                                             # 組めた
        self.run_main(self.pair_args())
        png_gone()                                                             # 切れた
        with mock.patch.object(bbctl, "OPEN_CMD", str(self.tmp / "no-such-open")):
            self.run_main(["pair", *self.common("--ip", IP, "--host", HOSTNAME)])
        png_gone()                                                             # 開けなかった
        with mock.patch.object(bbctl, "_wait", side_effect=OSError("stdout closed")):
            code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(code, 1)
        png_gone()                                                             # 印字・待つ途中の OSError
        with mock.patch.object(bbctl, "_wait", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_main(self.pair_args())
        png_gone()                                                             # 想定外の例外
        with mock.patch.object(bbctl.qr, "write_png", side_effect=OSError("disk full")):
            code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(code, 1)
        png_gone()                                                             # PNG を書けなかった

    def test_the_code_is_not_left_when_a_signal_comes_right_after_it_is_made(self):
        # 符号を作る前に合図の受け手を入れておく（作った直後の SIGTERM でも pairing.json を残さない）
        order = []
        real_create = Pairing.create

        def spy(self_):
            order.append(signal.getsignal(signal.SIGTERM))
            return real_create(self_)

        with mock.patch.object(Pairing, "create", spy):
            self.run_main(self.pair_args())
        self.assertTrue(order and callable(order[0]) and order[0] not in (signal.SIG_DFL, signal.SIG_IGN))

    def test_a_second_signal_during_cleanup_does_not_cut_the_cleanup_short(self):
        # 片付けの最中にもう一度合図が来ても（窓を閉じたあとの SIGTERM など）、PNG と符号を消し終える
        real_remove = bbctl._remove
        sent = []

        def remove_after_another_signal(path):
            if not sent:
                sent.append(1)
                os.kill(os.getpid(), signal.SIGTERM)               # 無視されているはず（落ちない・途中でやめない）
            real_remove(path)

        with mock.patch.object(bbctl, "_remove", remove_after_another_signal):
            code, out, err, _ = self.run_main(self.pair_args())     # 時計を進めて切れて終わる
        self.assertEqual(sent, [1])
        self.assertEqual(code, 1)
        self.assertEqual(self.pngs(), [])
        self.assertFalse((self.support / "pairing.json").exists())

    def test_the_stop_handler_acts_only_on_the_first_signal(self):
        # 続けて届いた別の合図は何もしない（片付けの途中で例外を上げて中断させない）。終了コードは最初に受けた合図のもの
        bbctl._STOPPING.clear()
        self.addCleanup(bbctl._STOPPING.clear)
        with self.assertRaises(bbctl._Terminated) as first:
            bbctl._on_stop(signal.SIGHUP, None)
        self.assertEqual(first.exception.signum, signal.SIGHUP)
        for later in (signal.SIGTERM, signal.SIGQUIT, signal.SIGINT, signal.SIGHUP):
            self.assertIsNone(bbctl._on_stop(later, None))                       # 例外を上げない
        self.assertEqual(bbctl._STOPPING, [signal.SIGHUP])
        bbctl._restore_stop_signals({})
        self.assertEqual(bbctl._STOPPING, [])                                    # 片付けが済めば、次の呼び出しのために空に戻る
        with self.assertRaises(KeyboardInterrupt):
            bbctl._on_stop(signal.SIGINT, None)

    def test_the_png_removal_failing_does_not_stop_the_code_from_being_discarded(self):
        real_unlink = Path.unlink

        def unlink(self_, *a, **kw):
            if self_.name.startswith("qr-"):
                raise PermissionError(13, f"denied {CODE}")                      # 例外の文に符号が入っていても、出さない
            return real_unlink(self_, *a, **kw)

        with mock.patch.object(Path, "unlink", unlink):
            code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(code, 1)                                                # 切れて終わった（片付けの失敗で落ちない）
        self.assertFalse((self.support / "pairing.json").exists())               # PNG が消せなくても、符号は消す
        self.assertIn("PermissionError", err)
        self.assertNotIn(CODE, out + err)
        for p in self.pngs():                                                    # 試験の後始末（消せなかった PNG）
            p.unlink()

    def test_a_signal_exception_at_the_start_of_cleanup_only_repeats_the_cleanup(self):
        # 片付けに入った直後に（受け手がまだ何も止めていない状態で）合図の例外が来ても、片付けを最初からやり直して済ませる
        real_remove = bbctl._remove
        raised = []

        def remove(path):
            if not raised:
                raised.append(1)
                raise bbctl._Terminated(signal.SIGTERM)
            real_remove(path)

        with mock.patch.object(bbctl, "_remove", remove):
            code, out, err, _ = self.run_main(self.pair_args())
        self.assertEqual(raised, [1])
        self.assertEqual(code, 1)
        self.assertEqual(self.pngs(), [])
        self.assertFalse((self.support / "pairing.json").exists())

    def test_holding_the_signals_also_silences_a_handler_that_is_already_waiting_to_run(self):
        bbctl._STOPPING.clear()
        self.addCleanup(bbctl._STOPPING.clear)
        bbctl._hold_stop_signals({})                                             # 片付けに入る
        self.assertIsNone(bbctl._on_stop(signal.SIGTERM, None))                  # 届いていた合図の受け手は、何も上げない
        self.assertIsNone(bbctl._on_stop(signal.SIGINT, None))

    def test_as_a_command_the_signals_stay_ignored_until_the_process_ends(self):
        # コマンドとして走るときは、片付けたあとに遅れて届く合図で、終わり方（128+番号）が変わらないようにする
        before = {sig: signal.getsignal(sig) for sig in bbctl._STOP_SIGNALS}
        self.addCleanup(lambda: [signal.signal(sig, old) for sig, old in before.items()])
        with mock.patch.object(bbctl, "_CLI", True):
            self.run_main(self.pair_args())
        self.assertEqual({sig: signal.getsignal(sig) for sig in bbctl._STOP_SIGNALS},
                         {sig: signal.SIG_IGN for sig in bbctl._STOP_SIGNALS})
        bbctl._STOPPING.clear()

    def test_signal_handlers_are_put_back_after_pairing(self):
        before = {sig: signal.getsignal(sig) for sig in bbctl._STOP_SIGNALS}
        self.run_main(self.pair_args())
        self.assertEqual({sig: signal.getsignal(sig) for sig in bbctl._STOP_SIGNALS}, before)

    def test_non_tty_output_tells_ai_assistants_not_to_open_the_image(self):
        code, out, err, _ = self.run_main(self.pair_args())
        lines = out.splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("QR image: "))
        self.assertEqual(lines[i + 1], "Do not open or read this image from an AI assistant; it contains a one-time pairing code. "
                                       "A human should scan it on screen.")
        self.assertNotIn(CODE, out)

    def test_tty_output_does_not_carry_the_ai_notice(self):
        # 人が端末から走らせたときは、人に向けた文だけ（QR を描く）
        code, out, err, _ = self.run_main(self.pair_args(), tty=True)
        self.assertNotIn("AI assistant", out)


class CheckHealthTest(BbctlCase):
    """bbctl.check_health：QR に入れる IP とポートの /v1/health を1回呼ぶ。答えた BridgeBoard server なら None、でなければ理由。"""

    def test_a_bridgeboard_server_that_answers_is_none(self):
        stub = self.health_stub()
        self.assertIsNone(bbctl.check_health("127.0.0.1", stub.port))
        self.assertEqual(stub.paths, ["/v1/health"])                         # 1回だけ、鍵なしの口を

    def test_every_other_answer_says_why(self):
        for status, body, word in ((500, b'{"ok": true, "apiVersion": 3}', "HTTP 500"),
                                   (200, b"<html>hello</html>", "not a BridgeBoard server"),
                                   (200, b'{"ok": true}', "not a BridgeBoard server"),
                                   (200, b'{"apiVersion": "3"}', "not a BridgeBoard server"),
                                   (200, b'{"apiVersion": true}', "not a BridgeBoard server"),
                                   (200, b"[]", "not a BridgeBoard server"),
                                   (200, b'{"ok": true, "apiVersion": 2}', "older")):
            with self.subTest(status=status, body=body):
                stub = self.health_stub(status=status, body=body)
                reason = bbctl.check_health("127.0.0.1", stub.port)
                self.assertIsInstance(reason, str)
                self.assertIn(word, reason)

    def test_nothing_listening_is_no_answer(self):
        stub = HealthStub()
        port = stub.port
        stub.close()
        self.assertIn("no answer", bbctl.check_health("127.0.0.1", port))

    def test_a_server_that_never_answers_is_given_up_on(self):
        stub = self.health_stub(delay=3.0)
        t0 = time.monotonic()
        with mock.patch.object(bbctl, "HEALTH_TIMEOUT", 0.3):
            self.assertIn("no answer", bbctl.check_health("127.0.0.1", stub.port))
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertLess(bbctl.HEALTH_TIMEOUT, 10)                            # 本番の待ちも、符号を作る前に長く待たせない


class DevicesTest(BbctlCase):
    def test_devices_lists_without_the_hash_or_the_token(self):
        store = DeviceStore(self.support)
        a, ta = store.add("Tomo's iPad")
        b, tb = store.add("Old iPhone")
        store.verify(ta)                                                       # lastSeenAt が入る
        code, out, err, _ = self.run_main(["devices", *self.common()])
        self.assertEqual(code, 0, err)
        for want in (a, b, "Tomo's iPad", "Old iPhone"):
            self.assertIn(want, out)
        for secret in (ta, tb, *[r["hash"] for r in json.loads((self.support / "devices.json").read_text())]):
            self.assertNotIn(secret, out + err)
        self.assertRegex(out, r"(?m)^ID\s+NAME\s+CREATED\s+LAST SEEN")
        old = next(l for l in out.splitlines() if b in l)
        self.assertTrue(old.rstrip().endswith("-"), old)                       # 一度も使っていない端末は「-」

    def test_devices_with_none(self):
        code, out, err, _ = self.run_main(["devices", *self.common()])
        self.assertEqual((code, out.strip()), (0, "No paired devices."))
        self.assertFalse((self.support / "devices.json").exists())

    def test_revoke(self):
        store = DeviceStore(self.support)
        a, ta = store.add("a")
        b, tb = store.add("b")
        code, out, err, _ = self.run_main(["revoke", a, *self.common()])
        self.assertEqual((code, out.strip()), (0, f"revoked: {a}"))
        self.assertIsNone(DeviceStore(self.support).verify(ta))
        self.assertEqual(DeviceStore(self.support).verify(tb), b)
        code, out, err, _ = self.run_main(["revoke", a, *self.common()])
        self.assertEqual(code, 1)
        self.assertIn(f"no such device: {a}", err)

    def test_revoke_rejects_a_malformed_id(self):
        store = DeviceStore(self.support)
        a, _ = store.add("a")
        for bad in ("", "xyz", a[:8], a.upper(), "../" + a, a + "0"):
            code, out, err, _ = self.run_main(["revoke", bad, *self.common()])
            self.assertEqual(code, 2, bad)
        self.assertEqual([r["id"] for r in store.list()], [a])

    def test_revoke_with_a_broken_file_does_not_touch_it(self):
        self.support.mkdir()
        (self.support / "devices.json").write_text("{broken", encoding="utf-8")
        code, out, err, _ = self.run_main(["revoke", "0" * 16, *self.common()])
        self.assertEqual(code, 1)
        self.assertIn("devices.json", err)
        self.assertEqual((self.support / "devices.json").read_text(), "{broken")

    def test_devices_with_a_broken_file_says_so_instead_of_saying_none(self):
        self.support.mkdir()
        (self.support / "devices.json").write_text("{broken", encoding="utf-8")
        code, out, err, _ = self.run_main(["devices", *self.common()])
        self.assertEqual(code, 1)
        self.assertIn("devices.json cannot be used", err)
        self.assertNotIn("No paired devices", out)

    def test_support_dir_option_can_come_before_the_command(self):
        DeviceStore(self.support).add("Laptop-A")
        code, out, err, _ = self.run_main(["--support-dir", str(self.support), "devices"])
        self.assertEqual(code, 0, err)
        self.assertIn("Laptop-A", out)

    def test_default_support_dir_is_the_normal_place(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/someone"}):
            self.assertEqual(bbctl.default_support_dir(), Path("/Users/someone/Library/Application Support/BridgeBoard"))

    def test_no_command_is_a_usage_error(self):
        code, out, err, _ = self.run_main([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
