#!/usr/bin/python3
"""一覧サーバーの起動口（launchd が動かす）。

Python 3.9 の標準ライブラリだけで動かす。

引数なしで本番：Tailscale の IPv4（tailscale ip -4）の本体（既定 8772）とファイル口（既定 8773）で待ち受ける。
設定は置き場の config.json（bblist/config.py。ポート・Claude のフォルダ・ランチャーの起点・git で無視されたファイル・
プレビューの外への読み込み・tailscale の場所）。引数で渡したものは設定より先。試験では --host 127.0.0.1 と tmp のフォルダを渡す。
待ち受けは Tailscale の IPv4（100.64.0.0/10）か loopback だけ（0.0.0.0 にはならない。web.make_servers が確かめる）。

Tailscale が落ちている（IP が取れない）間は終了せず、プロセスの中で待って確かめ直す（IP_CHECK_SECONDS ごと）。
IP が変わったら（取れなくなって戻った時も）待ち受け直す。MagicDNS の名前（discover.magicdns_name）も同じ時に取り直し、
Host の見出しで受ける名前に入れる。--host を渡したときは固定（確かめ直さない・Tailscale に聞かない）。
Host の見出しは、待ち受ける IP・loopback・MagicDNS の名前（--magicdns-name で固定もできる）だけ受ける（違えば 421。
DNS リバインディング対策）。
置き場の secret（32 バイト・権限 600）は、無ければ起動のときに作る（rootId の塩・ファイルの鍵の親。secrets_store）。
認証は端末ごとの鍵（置き場の devices.json にハッシュだけ。devices.py）。端末が1台も無くても起動する（組むのは bbctl pair。
bbctl は別のプロセスで、devices.json・pairing.json を通して動いているサーバーとやりとりする）。共通のトークンのファイルは使わない。
ポートの番号：config.json の apiPort と filePort が同じなら、設定を読む段階（config.py）で両方を無視して既定（8772・8773）に
戻し、記録（項目の名前だけ）して起動する。終了コードが 2 になるのは、フラグ（--api-port・--file-port）を含めた最後の値が
同じになったときだけ（フラグ同士、またはフラグと設定の組み合わせ。起動のときに記録して終わる）。
終了コード：0 止めた（SIGTERM・Ctrl-C）・1 --host で渡した IP で待ち受けられない／secret が使えない・
2 本体の口とファイル口が同じ番号（上の、フラグを含めて同じになったとき）。
0 以外で終わると、launchd が ThrottleInterval（10秒）ごとに立て直す。Tailscale が落ちているだけでは終わらない。
記録は --log-dir の listserver.log（1,000,000 バイトで回し、控え2本）。鍵・符号・本文・ファイルの中身は書かない。
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bblist import VERSION
from bblist.config import load_config
from bblist.devices import DeviceStore
from bblist.discover import magicdns_name, tailscale_ip
from bblist.gitignore import GitIgnore
from bblist.hub import Hub
from bblist.pairing import Pairing, remove_stale_qr
from bblist.secrets_store import load_or_create_secret
from bblist.sessions import Catalog
from bblist.transcript import TranscriptStore
from bblist.web import Context, build_allowed_hosts, make_servers

log = logging.getLogger("listserver")

HOME = Path.home()
IP_CHECK_SECONDS = 15.0                                   # Tailscale の IP と MagicDNS の名前を確かめ直す間隔（秒）
DOWN_AFTER_MISSES = 2                                     # IP が続けてこれだけ取れなかったら、待ち受けを閉じる（1回の取りこぼしで切らない）


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="listserver.py", description="BridgeBoard list server (read-only)")
    p.add_argument("--host", help="IP to listen on (default: tailscale ip -4, checked again every "
                                  f"{IP_CHECK_SECONDS:g}s; 127.0.0.1 only for tests)")
    p.add_argument("--api-port", type=int, default=None, help="default: apiPort in config.json, else 8772")
    p.add_argument("--file-port", type=int, default=None, help="default: filePort in config.json, else 8773")
    p.add_argument("--magicdns-name", default=None,
                   help="this Mac's MagicDNS name (e.g. mac.tailnet.ts.net); accepted in the Host header besides the "
                        "listening IP and loopback. Default: asked from tailscale. Without it, requests addressed by "
                        "name get 421 bad_host")
    p.add_argument("--claude-dir", type=Path, default=None, help="default: claudeDir in config.json, else ~/.claude")
    p.add_argument("--support-dir", type=Path, default=HOME / "Library" / "Application Support" / "BridgeBoard")
    p.add_argument("--log-dir", type=Path, default=HOME / "Library" / "Logs" / "BridgeBoard")
    a = p.parse_args(argv)
    a.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(a.log_dir / "listserver.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        return _run(a)
    finally:                                              # 何度呼んでも（試験）記録の先が増えないように外す
        root.removeHandler(handler)
        root.setLevel(level)
        handler.close()


class _Listening:
    """今の待ち受け：2つの口（本体・ファイル口）と、それぞれを回す糸。開く・名前を替える・閉じる。"""

    def __init__(self):
        self.ip: str | None = None                        # 待ち受けている IP（閉じていれば None）
        self.ports: tuple = ()
        self._servers: tuple = ()
        self._threads: list = []

    def open(self, ip: str, servers) -> None:
        self.ip, self._servers = ip, tuple(servers)
        self.ports = tuple(s.server_address[1] for s in servers)
        self._threads = [threading.Thread(target=s.serve_forever, name=f"bblist-{name}", daemon=True)
                         for name, s in zip(("api", "files"), servers)]
        for t in self._threads:
            t.start()

    def set_hosts(self, hosts) -> None:
        for s in self._servers:
            s.set_allowed_hosts(hosts)

    def close(self) -> None:
        servers, threads = self._servers, self._threads
        self.ip, self.ports, self._servers, self._threads = None, (), (), []
        for s in servers:
            s.shutdown()
        for t in threads:
            t.join()
        for s in servers:
            s.server_close()                              # 要求の糸（daemon）は待たない。終わると一緒に消える


class _Supervisor:
    """待ち受けを見張る。step() を IP_CHECK_SECONDS ごとに呼ぶ：IP を取り、変わっていれば待ち受け直し、MagicDNS の名前も取り直す。
    step() が整数を返したら、それが終了コード（--host で渡した IP で待ち受けられなかった）。"""

    def __init__(self, a, cfg, ctx: Context, api_port: int, file_port: int):
        self.a, self.cfg, self.ctx = a, cfg, ctx
        self.ports = (api_port, file_port)
        self.listening = _Listening()
        self.misses = 0
        self.name: str | None = a.magicdns_name           # 最後に取れた名前（取れない回があっても外さない）
        self.last_problem: str | None = None              # 同じ問題を確かめ直しのたびに記録しない

    def step(self) -> int | None:
        fixed = self.a.host
        ip = fixed or tailscale_ip(self.cfg.tailscale)
        if ip is None:
            self.misses += 1
            if self.listening.ip is not None and self.misses >= DOWN_AFTER_MISSES:
                self.listening.close()
                log.warning("Tailscale has no IPv4 address: stopped listening until it comes back")
            elif self.listening.ip is None:
                self._problem("Waiting for Tailscale (no IPv4 address yet); checking again every "
                              f"{IP_CHECK_SECONDS:g}s")
            return None
        self.misses = 0
        if not fixed and not self.a.magicdns_name:
            self.name = magicdns_name(self.cfg.tailscale) or self.name
        hosts = build_allowed_hosts(ip, self.name)
        if ip == self.listening.ip:
            self.listening.set_hosts(hosts)               # 名前だけ変わったこともある（待ち受けは作り直さない）
            return None
        if self.listening.ip is not None:
            log.info("Tailscale IP changed from %s to %s: listening again", self.listening.ip, ip)
            self.listening.close()
        try:
            servers = make_servers(ip, *self.ports, dataclasses.replace(self.ctx, allowed_hosts=hosts))
        except (ValueError, OSError) as e:
            if fixed:
                log.error("Cannot listen on %s: %s", ip, e)
                return 1
            self._problem(f"Cannot listen on {ip}: {e}; trying again every {IP_CHECK_SECONDS:g}s")
            return None
        self.last_problem = None
        self.listening.open(ip, servers)
        log.info("Listening on %s: api port %d, file port %d (%s)", ip, *self.listening.ports, VERSION)
        return None

    def _problem(self, message: str) -> None:
        if message != self.last_problem:
            self.last_problem = message
            log.warning("%s", message)


def _run(a) -> int:
    cfg = load_config(a.support_dir, HOME)
    for problem in cfg.problems:
        log.warning("%s", problem)                        # 項目の名前だけ（値は書かない）
    claude_dir = a.claude_dir if a.claude_dir is not None else cfg.claude_dir
    api_port = a.api_port if a.api_port is not None else cfg.api_port
    file_port = a.file_port if a.file_port is not None else cfg.file_port
    if api_port == file_port and api_port != 0:           # 0 は OS に選ばせる（2つあっても別の口）。フラグと設定の組み合わせで同じになりうる
        log.error("The api port and the file port must differ (both are %d). Change apiPort/filePort in config.json "
                  "or --api-port/--file-port", api_port)
        return 2
    try:
        secret = load_or_create_secret(a.support_dir)
    except (OSError, ValueError) as e:                    # 書くのは例外の型の名前と置き場のパス。secret の中身・例外の文は書かない
        log.error("Cannot read or create the secret (%s). Check the permissions of %s", type(e).__name__, a.support_dir)
        return 1
    remove_stale_qr(a.support_dir)                        # bbctl が消せずに残した、5分より古い QR の PNG
    # 見せない場所：置き場・記録・Claude の設定フォルダ。~/.claude がリンクでも、CLAUDE_CONFIG_DIR で別の場所でも、実体で守る
    catalog = Catalog(claude_dir, cfg.launcher_roots, HOME,
                      [a.support_dir, a.log_dir, Path(os.path.realpath(claude_dir))], secret=secret, launcher_auto=True)
    store = TranscriptStore(claude_dir / "projects")
    usage = a.support_dir / "usage.json"
    hub = Hub(catalog, store, usage)
    ctx = Context(devices=DeviceStore(a.support_dir), pairing=Pairing(a.support_dir), secret=secret, catalog=catalog,
                  store=store, hub=hub, usage_path=usage, started_at=int(time.time() * 1000),
                  gitignore=GitIgnore(show_ignored=cfg.show_git_ignored), preview_external=cfg.preview_external)
    supervisor = _Supervisor(a, cfg, ctx, api_port, file_port)
    stop = threading.Event()
    before = signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())   # launchd は SIGTERM で止める
    code = 0
    try:
        hub.start()
        while True:
            code = supervisor.step()
            if code is not None or stop.wait(1.0 if a.host else IP_CHECK_SECONDS):
                break
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, before)
        log.info("Stopped")
        supervisor.listening.close()
        hub.stop()
    return code or 0


if __name__ == "__main__":
    raise SystemExit(main())
