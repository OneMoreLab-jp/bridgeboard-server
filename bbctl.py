#!/usr/bin/python3
"""BridgeBoard の一覧サーバーの管理コマンド：bbctl pair / devices / revoke。

Python 3.9 の標準ライブラリだけで動かす（QR の符号器だけ、同梱の vendor/qrcodegen.py）。

  bbctl pair [--no-open]   5分で切れる一度きりの符号を作り、QR の PNG を置き場の pair/ に書いて Mac の画面に開く
                           （/usr/bin/open。--no-open なら開かない）。iPad のアプリで読むと、動いているサーバーが符号を引き換えて
                           その iPad だけの鍵を渡す。組めるか5分たつまで1秒ごとに見て、終わったら PNG を消し、
                           `paired: <端末の名前>`（終了コード 0）・`expired`（切れた。1）・`replaced: …`（別の bbctl pair が
                           後から始まって、この符号が使えなくなった。1）のどれかを出す。
                           符号を作る前に2つ確かめ、だめなら直し方を標準エラーに出して 1 で終わる（何も作らない）：
                           ①この Mac の MagicDNS の名前が取れる（Tailscale で MagicDNS が有効）こと。アプリは host が空の QR を
                           断るため。②QR に入れる IP とポートの /v1/health にサーバーが答えること（1回。入れていない・止まって
                           いる・別のポートを、5分待つ前に知らせる）。--host を渡したときは①の断りはせず、サーバーが受ける名前
                           （自分の MagicDNS の名前）と違えば警告だけ出す。
  bbctl devices            組んだ端末の一覧（id・名前・組んだ日時・最後に使った日時）。鍵・ハッシュは出さない。
  bbctl revoke <id>        その端末を取り消す。動いているサーバーは再起動なしですぐ断る。

どの道も --support-dir（既定は ~/Library/Application Support/BridgeBoard）を取る。pair は --api-port / --file-port
（QR に入れる口。既定は置き場の config.json の apiPort / filePort、無ければ 8772 / 8773）、--ip / --host（QR に入れるつなぎ先。
既定は tailscale ip -4 と MagicDNS の名前。tailscale の場所は config.json の tailscale か、探した場所）も取る。
Tailscale の IP と名前の取り方は bblist.discover に1つ（サーバーと同じ。サーバー本体 listserver は import しない）。
サーバーとは置き場のファイル（pairing.json・devices.json）を通してやりとりする。別のプロセスなので、時計は pairing.py の
システム全体の時計を使う。

符号・鍵は、標準出力・標準エラー・記録・例外の文に出さない。符号は QR の PNG の中にだけある。
例外は、人が端末から直接走らせたとき（標準出力が isatty）の QR の描画だけ（QR の絵であって、符号の文字は出さない）。
PNG は、組めたとき・切れたとき・止められたとき（Ctrl-C・SIGTERM・端末の窓を閉じる SIGHUP・SIGQUIT）・失敗したときに必ず消す
（消せるのは SIGKILL 以外。SIGKILL で残ったものは、次の bbctl pair と、サーバーの起動で消す）。
nohup などで無視されていた合図は、無視されたままにする（窓を閉じても続けたい人の意図。続けても5分で切れれば片付けて終わる）。
標準出力が端末でない（Claude Code などが走らせた）ときは、画像の場所のあとに「AI は画像を開かない・読まない」と書く。
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from bblist import qr
from bblist.config import load_config
from bblist.devices import DEVICE_ID, DeviceStore
from bblist.discover import TAILSCALE_NET, magicdns_status, tailscale_ip
from bblist.pairing import Pairing
from bblist.secrets_store import load_or_create_secret, server_id

OPEN_CMD = "/usr/bin/open"
POLL_SECONDS = 1.0                                        # 組めたかを見る間隔
NEW_DEVICE_WAIT = 10                                      # 符号が引き換えられた後、端末が足されるのを待つ回数（POLL_SECONDS ごと）
HEALTH_TIMEOUT = 3.0                                      # 符号を作る前の /v1/health の待ち（秒。つなぐのと答えるのに、それぞれ）
MIN_API_VERSION = 3                                       # POST /v1/pair があるサーバーの apiVersion（これ未満は組めない）
MAGICDNS_URL = "https://login.tailscale.com/admin/dns"
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*")


def default_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "BridgeBoard"


# ---------------------------------------------------------------- 引数

def _ip(value: str) -> str:
    """QR に入れる IP。サーバーが待ち受けられる範囲（Tailscale の IPv4 か loopback）だけ。"""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an IPv4 address") from None
    if not (ip.is_loopback or ip in TAILSCALE_NET):
        raise argparse.ArgumentTypeError("must be a Tailscale IPv4 address (100.64.0.0/10) or loopback")
    return str(ip)


def _host(value: str) -> str:
    if len(value) > 253 or not _HOST.fullmatch(value):
        raise argparse.ArgumentTypeError("not a host name")
    return value.lower()


def _port(value: str) -> int:
    if not (value.isascii() and value.isdigit() and 1 <= int(value) <= 65535):
        raise argparse.ArgumentTypeError("not a port number (1-65535)")
    return int(value)


def _device_id(value: str) -> str:
    if not DEVICE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a device id is 16 lowercase hex digits (see `bbctl devices`)")
    return value


def build_parser() -> argparse.ArgumentParser:
    def common(p, suppress):
        # サブコマンドの後ろにも前にも置ける。サブコマンド側の既定が、前に置いた値を上書きしないよう SUPPRESS にする
        p.add_argument("--support-dir", type=Path, default=argparse.SUPPRESS if suppress else default_support_dir(),
                       help="where the server keeps its files (default: ~/Library/Application Support/BridgeBoard)")

    parser = argparse.ArgumentParser(prog="bbctl", description="Manage the BridgeBoard list server's paired devices")
    common(parser, False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{pair,devices,revoke}")
    pair = sub.add_parser("pair", help="show a QR code that pairs one iPad (valid for 5 minutes)")
    common(pair, True)
    pair.add_argument("--no-open", action="store_true", help="do not open the QR image on this Mac's screen")
    pair.add_argument("--api-port", type=_port, default=None,
                      help="the server's API port to put in the QR (default: apiPort in config.json, else 8772)")
    pair.add_argument("--file-port", type=_port, default=None,
                      help="the server's file port to put in the QR (default: filePort in config.json, else 8773)")
    pair.add_argument("--ip", type=_ip, default=None, help="Tailscale IPv4 to put in the QR (default: tailscale ip -4)")
    pair.add_argument("--host", type=_host, default=None,
                      help="MagicDNS name to put in the QR (default: this Mac's, from tailscale). The server answers 421 to any name "
                           "other than its own MagicDNS name (or the one it was started with --magicdns-name), so this must be "
                           "that name; a different one is warned about")
    devices = sub.add_parser("devices", help="list paired devices")
    common(devices, True)
    revoke = sub.add_parser("revoke", help="revoke a paired device")
    common(revoke, True)
    revoke.add_argument("id", type=_device_id, help="device id (see `bbctl devices`)")
    return parser


# ---------------------------------------------------------------- 道具

def pair_url(host: str, ip: str, api_port: int, file_port: int, code: str, sid: str) -> str:
    """QR の中身。本当の鍵は入れない（一度きりの符号だけ）。"""
    query = urlencode([("v", 1), ("host", host), ("ip", ip), ("api", api_port), ("file", file_port), ("code", code), ("sid", sid)])
    return f"bridgeboard://pair?{query}"


def _stamp(ms) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if isinstance(ms, int) else "-"
    except (OverflowError, OSError, ValueError):          # 手で書き換えた桁違いの値
        return "-"


def _warn(message: str) -> None:
    """標準エラーに1行。書けなくても（窓が閉じている）黙って諦める。片付けの途中で落ちないため。"""
    try:
        print(message, file=sys.stderr)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _remove(path: Path) -> None:
    """ファイルを消す。無ければ何もしない。消せなくても落とさない（片付けの残りを続けるため。型の名前と場所だけ知らせる）。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _warn(f"warning: could not remove {path} ({type(e).__name__}). Remove it yourself.")


def _open_dir(support: Path) -> Path:
    """PNG を置くフォルダ（pair/。権限 700）。前に残った PNG（置き換わった符号のもの）はここで消す。ほかのファイルは触らない。"""
    folder = support / "pair"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in folder.glob("qr-*.png"):
        _remove(old)
    return folder


class _Terminated(BaseException):
    """終了の合図（SIGTERM・SIGHUP・SIGQUIT）で止められた。finally で PNG を消せるよう、例外にして上へ抜ける。"""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


_STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT") if hasattr(signal, name))
NOTICE_FOR_AI = ("Do not open or read this image from an AI assistant; it contains a one-time pairing code. "
                 "A human should scan it on screen.")


_STOPPING: list = []                                      # 最初に受けた止める合図の番号（空でなければ、止まり始めている）
_CLI = False                                              # コマンドとして走っている（末尾の __main__）。終わるまで合図を無視にしておく


def _on_stop(signum, frame):
    """止める合図を例外にして主の流れを抜けさせる。**最初の1回だけ**：続けて届いた別の合図（HUP・TERM・QUIT がほぼ同時、など）は
    何もしない。そうしないと、すでに届いている合図の受け手が、except の中や finally の頭（無視にする前）で次々に例外を上げ、
    PNG と符号の片付けが途中で切れる。終了コードは最初に受けた合図の 128+番号。"""
    if _STOPPING:
        return
    _STOPPING.append(signum)
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise _Terminated(signum)


def _catch_stop_signals() -> dict:
    """止める合図を例外にする受け手を入れる。元の受け手の表を返す（戻すのに使う）。無視されていた合図は、無視のままにする。
    主の糸でないとき（試験）は何もしない。"""
    saved = {}
    _STOPPING.clear()
    for sig in _STOP_SIGNALS:
        try:
            old = signal.getsignal(sig)
            if old == signal.SIG_IGN:
                continue
            signal.signal(sig, _on_stop)
            saved[sig] = old
        except ValueError:
            break
    return saved


def _hold_stop_signals(saved: dict) -> None:
    """片付けの間は、次の合図で中断されないよう、入れた合図を無視にする（すでに届いて受け手の実行を待っている合図も、
    _STOPPING に印を置いて何もさせない）。"""
    if not _STOPPING:
        _STOPPING.append(0)
    for sig in saved:
        try:
            signal.signal(sig, signal.SIG_IGN)
        except ValueError:
            return


def _restore_stop_signals(saved: dict) -> None:
    _STOPPING.clear()
    for sig, old in saved.items():
        try:
            signal.signal(sig, old if old is not None else signal.SIG_DFL)
        except ValueError:
            return


def _say(message: str) -> None:
    """標準出力に1行。端末の窓が閉じていて書けないときは、静かに諦める（終わるときの flush も失敗しないよう、出力先を捨てる先に替える）。"""
    try:
        print(message)
        sys.stdout.flush()
    except (OSError, ValueError):
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass


def _unusable_devices(e: Exception) -> str:
    return f"error: devices.json cannot be used ({type(e).__name__}). Fix or remove it. Nothing was changed."


# ---------------------------------------------------------------- pair

def check_health(ip: str, port: int) -> str | None:
    """QR に入れる IP とポートの GET /v1/health を1回。BridgeBoard の（組める版の）サーバーが答えれば None、
    そうでなければ理由（英語の短い文。鍵・符号は入らない）。鍵なしで答える口で、答えは ok と apiVersion だけ。"""
    conn = http.client.HTTPConnection(ip, port, timeout=HEALTH_TIMEOUT)
    try:
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        body = resp.read(4096)
    except (OSError, http.client.HTTPException):
        return "no answer"
    finally:
        conn.close()
    if resp.status != 200:
        return f"HTTP {resp.status}"
    try:
        version = json.loads(body.decode("utf-8")).get("apiVersion")
    except (ValueError, AttributeError, RecursionError):        # JSON でない・辞書でない
        version = None
    if not isinstance(version, int) or isinstance(version, bool):
        return "not a BridgeBoard server"
    if version < MIN_API_VERSION:
        return f"an older BridgeBoard server (apiVersion {version}) that cannot pair; run install.sh again"
    return None


def _magicdns_problem(enabled, name) -> str | None:
    """--host なしで使う名前が取れないときの、直し方（英語）。取れていれば None。"""
    if enabled is False:
        return ("error: Tailscale MagicDNS is turned off for this tailnet, so the iPad has no name to reach this Mac by. "
                f"Turn it on at {MAGICDNS_URL}, then run `bbctl pair` again. No pairing code was made.")
    if name is None:
        return ("error: could not read this Mac's MagicDNS name from Tailscale. Is Tailscale running and signed in, and is MagicDNS "
                f"turned on at {MAGICDNS_URL}? Fix that, then run `bbctl pair` again. No pairing code was made.")
    return None


def _host_warning(host: str, name) -> str | None:
    """--host を渡したとき：サーバーが受ける名前（自分の MagicDNS の名前）と違う・確かめられない、の警告。同じなら None。"""
    if name is None:
        return (f"warning: could not read this Mac's MagicDNS name from Tailscale, so --host {host} cannot be checked. The server "
                "answers 421 to any name other than its own MagicDNS name.")
    if host != name:
        return (f"warning: --host {host} differs from this Mac's MagicDNS name ({name}). The server answers 421 to any other name "
                f"unless it was started with --magicdns-name {host}.")
    return None


def cmd_pair(a, clock, sleep, health) -> int:
    support = a.support_dir
    config = load_config(support)
    for problem in config.problems:                       # 項目の名前だけ（値は出さない）
        _warn(f"warning: {problem}")
    api_port = a.api_port if a.api_port is not None else config.api_port
    file_port = a.file_port if a.file_port is not None else config.file_port
    ip = a.ip or tailscale_ip(config.tailscale)
    if ip is None:
        print("error: could not determine the Tailscale IP. Is Tailscale running? Pass --ip to set it.", file=sys.stderr)
        return 1
    enabled, found = magicdns_status(config.tailscale)    # tailscale を1回。MagicDNS が無いと、アプリは QR を断る（符号を作る前に言う）
    if a.host is None:
        problem = _magicdns_problem(enabled, found)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        host = found
    else:
        host = a.host
        warning = _host_warning(host, found)
        if warning:
            _warn(warning)
    reason = health(ip, api_port)                         # QR に入れる IP とポートでサーバーが答えるか（5分待つ前に知らせる）
    if reason is not None:
        print(f"error: the BridgeBoard server did not answer at http://{ip}:{api_port}/v1/health ({reason}). Is it running? "
              "See ~/Library/Logs/BridgeBoard/listserver.log, or run install.sh --check; pass --api-port if the port is not "
              "the one in config.json. No pairing code was made.", file=sys.stderr)
        return 1
    try:
        sid = server_id(load_or_create_secret(support))
        pairing = Pairing(support, clock=clock)
        folder = _open_dir(support)
    except (OSError, ValueError) as e:
        print(f"error: could not prepare pairing ({type(e).__name__}). Check the permissions of {support}", file=sys.stderr)
        return 1
    try:
        known = {d["id"] for d in DeviceStore(support).list(strict=True)}   # 符号を作る前の端末（後で足された1台を見分ける）
    except (OSError, ValueError) as e:                    # 壊れた devices.json では、符号を引き換えても端末を足せない
        print(_unusable_devices(e), file=sys.stderr)
        return 1
    tty = sys.stdout.isatty()
    png = folder / f"qr-{int(time.time() * 1000)}.png"
    code = None
    saved = _catch_stop_signals()                         # 符号を作る前に入れる（作った直後の合図でも pairing.json を残さない）
    try:
        try:
            code = pairing.create()
        except (OSError, ValueError) as e:
            print(f"error: could not prepare pairing ({type(e).__name__}). Check the permissions of {support}", file=sys.stderr)
            return 1
        text = pair_url(host, ip, api_port, file_port, code, sid)
        qr.write_png(png, text)
        print(f"QR image: {png}")
        if not tty:                                       # Claude Code などが走らせたとき：画像を開かせない・読ませない
            print(NOTICE_FOR_AI)
        print("Scan it with the BridgeBoard app within 5 minutes. It is removed when pairing ends.")
        if not a.no_open:
            _open_png(png)
        if tty:
            print(qr.terminal_art(qr.matrix(text)))       # 人が端末から走らせたときだけ。符号・URL の文字は出さない
        sys.stdout.flush()
        return _wait(pairing, DeviceStore(support), code, known, clock, sleep)
    except KeyboardInterrupt:
        _say("cancelled")
        return 128 + signal.SIGINT
    except _Terminated as t:
        _say("cancelled")
        return 128 + t.signum
    except OSError as e:
        print(f"error: could not show the QR ({type(e).__name__})", file=sys.stderr)
        return 1
    finally:
        _cleanup(saved, png, pairing, code)


def _cleanup(saved: dict, png: Path, pairing: Pairing, code) -> None:
    """PNG と、まだ有効な符号を消す。どの終わり方でも通る。最初の合図で止まり始めたあとは受け手が何もしない（_on_stop）うえ、
    念のため、ここへ入った直後に合図の例外が来ても、全部やり直す（どの手順も、2度やって構わない）。"""
    for _ in range(3):
        try:
            _hold_stop_signals(saved)
            _remove(png)
            if code is not None:
                pairing.discard(code)                     # 組めたあとは既に無い。切れた・止められたときの符号を残さない
            break
        except (KeyboardInterrupt, _Terminated):
            continue
    if not _CLI:                                          # コマンドなら、プロセスが終わるまで無視のまま（遅れて届く合図で、
        _restore_stop_signals(saved)                      # 片付けたあとの終わり方を 128+番号 から変えない）


def _open_png(png: Path) -> None:
    """Mac の画面に PNG を開く。開けなくても組むのは続ける（--no-open と同じに、画像の場所は出してある）。"""
    try:
        subprocess.run([OPEN_CMD, str(png)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"warning: Could not open the QR image ({type(e).__name__}). Open it yourself: {png}", file=sys.stderr)


def _wait(pairing: Pairing, devices: DeviceStore, code: str, known: set, clock, sleep) -> int:
    """組めるか切れるまで、POLL_SECONDS ごとに見る。"""
    while True:
        state = pairing.check(code)
        if state == "used":
            return _report_paired(devices, known, sleep)
        if state == "replaced":                           # 後から別の bbctl pair が始まった：切れたのではない
            print("replaced: another `bbctl pair` was started, so this QR code no longer works")
            return 1
        if state != "pending":                            # expired（切れた・壊れた）
            print("expired")
            return 1
        sleep(POLL_SECONDS)


def _report_paired(devices: DeviceStore, known: set, sleep) -> int:
    """符号が引き換えられた。足された端末の名前を出す（サーバーは符号を消してから端末を足すので、少し待つ）。"""
    for attempt in range(NEW_DEVICE_WAIT):
        new = [d for d in devices.list() if d["id"] not in known]
        if new:
            print(f"paired: {max(new, key=lambda d: d['createdAt'])['name']}")
            return 0
        if attempt + 1 < NEW_DEVICE_WAIT:
            sleep(POLL_SECONDS)
    print("failed: the code was used but no device was registered. Check the server's log, then run `bbctl pair` again.",
          file=sys.stderr)
    return 1


# ---------------------------------------------------------------- devices / revoke

def cmd_devices(a) -> int:
    try:
        rows = DeviceStore(a.support_dir).list(strict=True)
    except (OSError, ValueError) as e:
        print(_unusable_devices(e).replace(" Nothing was changed.", ""), file=sys.stderr)
        return 1
    if not rows:
        print("No paired devices.")
        return 0
    width = max(len(r["name"]) for r in rows)
    print(f"{'ID':<16}  {'NAME':<{width}}  {'CREATED':<16}  LAST SEEN")
    for r in rows:
        print(f"{r['id']:<16}  {r['name']:<{width}}  {_stamp(r['createdAt']):<16}  {_stamp(r['lastSeenAt'])}")
    return 0


def cmd_revoke(a) -> int:
    try:
        revoked = DeviceStore(a.support_dir).revoke(a.id)
    except (OSError, ValueError) as e:
        print(_unusable_devices(e), file=sys.stderr)
        return 1
    if not revoked:
        print(f"no such device: {a.id}", file=sys.stderr)
        return 1
    print(f"revoked: {a.id}")
    return 0


def main(argv=None, *, clock=None, sleep=time.sleep, health=None) -> int:
    """clock は単調な時計（既定は pairing.monotonic）、sleep は待ち方、health は(IP, ポート)で サーバーの答えを確かめるもの
    （既定は check_health）。どれも試験が差し替える。"""
    a = build_parser().parse_args(argv)
    if a.command == "pair":
        return cmd_pair(a, clock, sleep, health or check_health)
    if a.command == "devices":
        return cmd_devices(a)
    return cmd_revoke(a)


if __name__ == "__main__":
    _CLI = True
    raise SystemExit(main())
