"""bblist/web.py・listserver.py の試験。python3 -m unittest discover -s tests -t . -p 'test_web.py' -v

loopback・port 0 で2つの口を立て、http.client で叩く（本物の Tailscale IP と ~/.claude には触らない）。
Catalog・TranscriptStore・Hub は本物を tmp で使う（生死と procStart は test_sessions.py と同じく辞書で差し替える）。
一覧を読み直すのは見回り（Hub）だけなので、sessions/*.json を変えた試験は hub.step() を呼ぶ。
見回りの糸を動かすのは SSE の試験だけ。ping は 0.2秒（後始末で SSE の糸がすぐ抜けるように）。
NFD の名前と chmod 000 は、ディスクに本当に作って試す。chmod 000 は、後始末（tmp の削除）のため、必ず権限を戻す。
"""
from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import statistics
import subprocess
import tempfile
import threading
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import listserver
from bblist.devices import DeviceStore
from bblist.gitignore import GitIgnore, run_check_ignore
from bblist.hub import Hub
from bblist.pairing import PairLimiter, Pairing
from bblist.secrets_store import file_key
from bblist.transcript import TranscriptStore
from bblist import VERSION, web
from bblist.web import Context, build_allowed_hosts, make_servers
from tests.make_fixture import PNG_1x1
from tests.test_gitignore import init_repo, isolated_git_env
from tests.test_sessions import SECRET, CatalogCase, folder_id

HOST = "127.0.0.1"
TOKEN = "t0ken_" + "Q" * 37                   # 試験の端末の鍵。本物（secrets.token_urlsafe(32)）と同じく URL で使える文字の 43 字
CODE = "PairCode_" + "Z" * 13                 # 組む符号（secrets.token_urlsafe(16) と同じ22字）
REAL_TOKEN_URLSAFE = secrets.token_urlsafe
JSON = "application/json; charset=utf-8"
B, B_HOME = "session_1", "session_2"          # 作業フォルダがプロジェクトのセッションと、ホームのセッション
SID = "sid-1"
TS = "2026-10-27T01:00:00.000Z"
TS_LAST = "2026-10-27T01:00:09.000Z"
SECRET_TEXT = "ひみつの本文"                   # 会話の本文（記録に出てはいけない）
FILE_TEXT = "ファイルの中身"                   # ファイルの中身（記録に出てはいけない）
STARTED = 1792941510789

log = logging.getLogger(__name__)


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


# ---------------------------------------------------------------- 会話ログの行

def line(row: dict) -> bytes:
    """Claude Code と同じ詰めた形（区切りに空白なし）の1行。"""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def human(uuid: str, text: str, images=()) -> dict:
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt,
                                            "data": base64.b64encode(data).decode("ascii")}} for mt, data in images]
    content.append({"type": "text", "text": text})
    return {"type": "user", "uuid": uuid, "timestamp": TS, "origin": {"kind": "human"},
            "message": {"role": "user", "content": content}}


def tool(uuid: str, tid: str, name: str, **inp) -> dict:
    return {"type": "assistant", "uuid": uuid, "timestamp": TS, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(uuid: str, tid: str) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": TS, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


# ---------------------------------------------------------------- HTTP の道具

class Reply:
    def __init__(self, status: int, headers, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)


def call(method: str, port: int, path: str, token: str | None = None, **headers) -> Reply:
    """1回の要求。path は ASCII（日本語はパーセントで符号化して渡す）。"""
    conn = http.client.HTTPConnection(HOST, port, timeout=5)
    try:
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, headers=headers)
        with conn.getresponse() as resp:
            return Reply(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


def get(port: int, path: str, token: str | None = None, **headers) -> Reply:
    return call("GET", port, path, token, **headers)


def post(port: int, path: str, body: bytes, **headers) -> Reply:
    """本文つきの POST（Content-Length は http.client が付ける）。"""
    conn = http.client.HTTPConnection(HOST, port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers)
        with conn.getresponse() as resp:
            return Reply(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


class Clock:
    """組む符号の単調な時計。試験が進める。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class SSE:
    """/v1/events を1本つなぐ。status が 200 なら知らせを読める。"""

    def __init__(self, port: int, query: str = "", token: str | None = TOKEN, **headers):
        self.conn = http.client.HTTPConnection(HOST, port, timeout=5)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.conn.request("GET", "/v1/events" + query, headers=headers)
        self.resp = self.conn.getresponse()
        self.status = self.resp.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def block(self) -> list:
        """空行までの1かたまり（行の並び）。"""
        lines = []
        while True:
            raw = self.resp.readline()
            if not raw:
                raise EOFError("SSE が閉じた")
            text = raw.decode("utf-8").rstrip("\n")
            if text:
                lines.append(text)
            elif lines:
                return lines

    def wait_event(self, name: str, timeout: float = 5.0) -> dict:
        """name の知らせが来るまで読み、data の JSON を返す（間の ping・ほかの知らせは読み捨てる）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.block()
            if lines[0] == f"event: {name}":
                assert len(lines) == 2 and lines[1].startswith("data: "), lines
                return json.loads(lines[1][len("data: "):])
        raise AssertionError(f"{name} が {timeout} 秒で来ない")

    def wait_ping(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.block() == [": ping"]:
                return
        raise AssertionError(f"ping が {timeout} 秒で来ない")

    def ends(self, timeout: float = 2.0) -> bool:
        """timeout 秒のうちに流れが終わる（サーバーが閉じる）か。間の ping は読み捨てる。"""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                self.block()
        except EOFError:
            return True
        return False

    def alive_for(self, seconds: float) -> bool:
        """seconds 秒のあいだ流れが続く（閉じられない）か。"""
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                self.block()
        except EOFError:
            return False
        return True

    def close(self) -> None:
        self.resp.close()                     # 応答が握っているソケットも、これで本当に閉じる
        self.conn.close()


def stop_servers(servers, threads) -> None:
    for s in servers:
        s.shutdown()
    for t in threads:
        t.join()
    for s in servers:
        s.server_close()                      # 要求の糸（daemon）は待たない。待つのは join_new_threads


def join_new_threads(before: set, timeout: float = 3.0) -> list:
    """before に無かった糸が終わるのを待ち、残ったものの名前を返す（漏れ）。
    3.9 の ThreadingMixIn は daemon の糸を server_close で待たないので、試験の後始末で待って確かめる。"""
    deadline = time.monotonic() + timeout
    left = []
    for t in set(threading.enumerate()) - before:
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            left.append(t.name)
    return left


def wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def open_idle(port: int) -> socket.socket:
    """つないだだけで何も送らない接続。後始末は呼んだ側（addCleanup）。"""
    return socket.create_connection((HOST, port), timeout=5)


def closed_within(sock: socket.socket, seconds: float) -> bool:
    """seconds 秒のうちにサーバーが閉じたか（EOF か reset）。何かを送ってきた・まだ開いているなら False。"""
    sock.settimeout(seconds)
    try:
        return sock.recv(1) == b""
    except socket.timeout:
        return False
    except ConnectionError:
        return True


def exchange_raw(port: int, request: bytes, wait: float = 3.0) -> bytes:
    """生の要求を送り、応答の全部（見出しも）を返す。応答が無いまま切られたら b""。"""
    data = b""
    with socket.create_connection((HOST, port), timeout=wait) as s:
        try:
            s.sendall(request)
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except ConnectionError:                               # 送り終える前に切られた（見出しが大きすぎるなど）
            pass
    return data


def head_raw(port: int, path: str, token: str | None = None, **headers):
    """HEAD を生のソケットで送り、(状態コード, 見出し, 見出しのあとに続いたバイト) を返す。
    http.client は HEAD の応答の本体を読まないので、call("HEAD") では本体が付いていても見えない。"""
    sent = {"Host": f"{HOST}:{port}", **headers}
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    request = f"HEAD {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in sent.items()) + "\r\n"
    raw = exchange_raw(port, request.encode("ascii"))
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    fields = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return int(lines[0].split(" ")[1]), fields, body


def exchange(port: int, request: bytes, wait: float = 3.0):
    """生の要求を送り、(状態コード, 本文) を返す。応答が無いまま切られたら (None, b"")。"""
    data = exchange_raw(port, request, wait)
    m = re.match(rb"HTTP/1\.[01] (\d{3}) ", data)
    return (int(m.group(1)) if m else None), data.partition(b"\r\n\r\n")[2]


class Records(logging.Handler):
    """記録の行（メッセージだけ）を集める。要求の糸から呼ばれる（list.append は糸をまたいでよい）。"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def capture_log(name: str = "bblist"):
    logger = logging.getLogger(name)
    rec, level = Records(), logger.level
    logger.addHandler(rec)
    logger.setLevel(logging.INFO)
    try:
        yield rec
    finally:
        logger.removeHandler(rec)
        logger.setLevel(level)


def cors(r: Reply) -> list:
    return [k for k in r.headers if k.lower().startswith("access-control-")]


# ---------------------------------------------------------------- 土台

class WebCase(CatalogCase):
    """tmp にプロジェクトのフォルダ・会話ログ・セッション2本を作り、2つの口を loopback・port 0 で立てる。

    session_1 の作業フォルダは ~/Projects/30_cat/proj（見せてよい）。session_2 はホーム（見せない）。
    """

    HUB: dict = {}                            # Hub の間隔（既定は本物の間隔）
    START_HUB = False                         # 見回りの糸を動かすか
    PING = 0.2
    CTX: dict = {}                            # Context に足す引数（接続の上限・見出しの時間・許す Host）

    def setUp(self):
        super().setUp()
        before = set(threading.enumerate())
        self.addCleanup(lambda: self.assertEqual(join_new_threads(before), [], "後始末の後も残った糸"))   # 最後に走る
        self.root = self.lab / "30_cat" / "proj"
        self.outside = self.tmp / "outside"
        files = {"a.md": f"# 見出し\n{FILE_TEXT}\n", "sub/b.png": PNG_1x1, ".env": "K=1", ".git/config": "[core]",
                 "credentials.json": "{}", "k.pem": "-----", "x.sqlite": "db"}
        for rel, data in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        self.big = os.urandom(200 * 1024)                       # 64 KiB ずつ送る分を超える大きさ
        (self.root / "big.bin").write_bytes(self.big)
        self.outside.mkdir()
        (self.outside / "x").write_text("外", encoding="utf-8")
        (self.root / "link_out").symlink_to(self.outside)      # 外へ出るシンボリックリンク
        self.logs = self.claude / "projects" / "folder"
        self.logs.mkdir(parents=True)
        self.append_log(*self.log_rows())
        self.add(1, cwd=str(self.root))
        self.add(2, cwd=str(self.home))
        self.R, self.R_HOME = folder_id(self.root), folder_id(self.home)
        self.usage = self.own[0] / "usage.json"
        self.usage.parent.mkdir(parents=True)
        self.catalog = self.make_catalog()
        self.store = TranscriptStore(self.claude / "projects")
        self.hub = Hub(self.catalog, self.store, self.usage, **self.HUB)
        self.addCleanup(self.hub.stop)
        # 端末の鍵・組む符号は置き場（self.own[0]）に置く。試験の端末の鍵は TOKEN（作るときだけ乱数を固定する）
        self.clock, self.boot = Clock(), [1_792_343_805]
        self.devices = DeviceStore(self.own[0])
        with mock.patch("secrets.token_urlsafe", return_value=TOKEN):
            self.DEV, _ = self.devices.add("Test iPad")
        self.pairing = Pairing(self.own[0], clock=self.clock, boot=lambda: self.boot[0])
        self.ctx = Context(devices=self.devices, pairing=self.pairing, pair_limits=PairLimiter(clock=self.clock),
                           secret=SECRET, catalog=self.catalog, store=self.store, hub=self.hub,
                           usage_path=self.usage, started_at=STARTED, ping=self.PING, **self.CTX)
        servers = self.servers = make_servers(HOST, 0, 0, self.ctx)
        threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}) for s in servers]
        for t in threads:
            t.start()
        self.addCleanup(stop_servers, servers, threads)
        self.api, self.files = (s.server_address[1] for s in servers)
        if self.START_HUB:
            self.hub.start()

    def log_rows(self) -> list:
        return [
            human("u1", SECRET_TEXT),
            human("u-img", "見て", [("image/png", PNG_1x1)]),
            human("u-odd", "種類が画像でない", [("text/html", PNG_1x1)]),
            {"type": "assistant", "uuid": "a1", "timestamp": TS,
             "message": {"role": "assistant", "content": [{"type": "text", "text": "こたえ"}]}},
            tool("a2", "t1", "Write", file_path=str(self.root / "a.md")), result("r1", "t1"),
            tool("a3", "t2", "Write", file_path=str(self.root / "new.md")), result("r2", "t2"),
            tool("a4", "t3", "Write", file_path=str(self.outside / "x")), result("r3", "t3"),
            tool("a5", "t4", "Bash", command="git status"),                 # 答えが来ていない呼び出し
            {"type": "queue-operation", "operation": "enqueue", "content": "待っている指示", "timestamp": TS_LAST},
        ]

    def append_log(self, *rows: dict) -> None:
        with open(self.logs / f"{SID}.jsonl", "ab") as f:
            f.write(b"".join(line(r) for r in rows))

    def cookie(self, root_id: str | None = None, device: str | None = None) -> str:
        """その端末・その起点のファイルの鍵のクッキー（bb_file=<端末の id>.<鍵>）。鍵は secret から導く（値を書き写さない）。"""
        dev = device or self.DEV
        return f"bb_file={dev}.{file_key(SECRET, dev, root_id or self.R)}"

    def add_device(self, name: str = "Second iPad"):
        """もう1台の端末（本物の乱数の鍵）。(id, 鍵)。"""
        return self.devices.add(name)

    def other_root(self) -> str:
        """もう1つ見せてよい起点（30_cat/other。b.md が入っている）を作り、その rootId を返す。"""
        other = self.lab / "30_cat" / "other"
        other.mkdir()
        (other / "b.md").write_text("別の起点", encoding="utf-8")
        self.catalog.refresh()
        rid = folder_id(other)
        self.assertIn(rid, self.catalog.visible_roots())
        return rid

    def tree(self, rel: str = "") -> Reply:
        return get(self.api, f"/v1/roots/{self.R}/tree" + (f"?path={quote(rel)}" if rel else ""), TOKEN)

    def resolve(self, path: str, bridge: str = B) -> Reply:
        return get(self.api, f"/v1/sessions/{bridge}/resolve?path={quote(path)}", TOKEN)

    @contextlib.contextmanager
    def assertNoLogs_compat(self, name: str):
        """その名前の記録に WARNING 以上が出ないこと（3.9 には assertNoLogs が無い）。"""
        rec = Records()
        logger = logging.getLogger(name)
        before = logger.level
        logger.addHandler(rec)
        logger.setLevel(logging.WARNING)
        try:
            yield
        finally:
            logger.removeHandler(rec)
            logger.setLevel(before)
        self.assertEqual(rec.lines, [], "記録に警告が出た")

    def assert_error(self, r: Reply, status: int, code: str, msg=None, reason: str | None = None) -> None:
        self.assertEqual(r.status, status, msg)
        self.assertEqual(r.headers["Content-Type"], JSON, msg)
        self.assertEqual(r.json(), {"error": code, **({"reason": reason} if reason else {})}, msg)

    def diagnostics(self) -> dict:
        r = get(self.api, "/v1/diagnostics", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        return r.json()


# ---------------------------------------------------------------- 守り

class GuardTest(WebCase):
    def test_health_needs_no_token(self):
        for tok in (None, "x"):
            r = get(self.api, "/v1/health", tok)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], JSON)
            self.assertEqual(r.json()["ok"], True)

    def test_every_route_401_without_or_with_wrong_token(self):
        R = self.R
        for path in ["/v1/sessions", f"/v1/sessions/{B}/transcript", f"/v1/sessions/{B}/images/x/0",
                     f"/v1/sessions/{B}/written", f"/v1/sessions/{B}/resolve?path=a.md", f"/v1/roots/{R}/tree",
                     f"/v1/filekey?rootId={R}", "/v1/usage", "/v1/events", "/v1/diagnostics", "/v1/nope"]:   # 無い道も 401（道があるかを見せない）
            self.assert_error(get(self.api, path), 401, "unauthorized", path)
            self.assert_error(get(self.api, path, token="x"), 401, "unauthorized", path)
        for path in [f"/{R}/a.md", f"/{R}/nope.md", "/nope"]:
            self.assert_error(get(self.files, path), 401, "unauthorized", path)
            self.assert_error(get(self.files, path, token="x"), 401, "unauthorized", path)
        # 合わない形の合言葉も通さない（クッキーを受けるファイル口でも）
        for headers in ({"Authorization": "Bearer "}, {"Authorization": f"Basic {TOKEN}"},
                        {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": TOKEN},
                        {"Cookie": "bb_token="}, {"Cookie": f"bb_token={TOKEN[:-1]}"}, {"Cookie": f"token={TOKEN}"},
                        {"Cookie": "bb_file="}, {"Cookie": f"bb_file={self.DEV}.{file_key(SECRET, self.DEV, R)[:-1]}"},
                        {"Cookie": f"bb_file={file_key(SECRET, self.DEV, R)}"}, {"Cookie": f"bb_file={self.DEV}."},
                        {"Authorization": "Bearer トークン".encode("utf-8")},           # ASCII でない合言葉で落ちない
                        {"Cookie": "bb_token=トークン".encode("utf-8")}, {"Cookie": "bb_file=鍵".encode("utf-8")}):
            self.assert_error(get(self.api, "/v1/sessions", **headers), 401, "unauthorized", headers)
            self.assert_error(get(self.files, f"/{R}/a.md", **headers), 401, "unauthorized", headers)
        self.assert_error(get(self.api, f"/v1/sessions?token={TOKEN}"), 401, "unauthorized")   # 問い合わせでは受けない
        self.assert_error(get(self.files, f"/{R}/a.md?token={TOKEN}"), 401, "unauthorized")

    def test_cookie_accepted_on_file_port(self):
        # クッキーは、プレビューの WKWebView が読む css・画像に見出しを付けられないためのもの。ファイル口だけで受ける。
        # 受けるのは bb_file（その URL の起点の鍵）だけ
        for cookie in (self.cookie(), f"theme=dark; {self.cookie()}; x=1"):
            r = get(self.files, f"/{self.R}/a.md", Cookie=cookie)
            self.assertEqual((r.status, r.body.decode("utf-8")), (200, f"# 見出し\n{FILE_TEXT}\n"), cookie)
        self.assertEqual(get(self.files, f"/{self.R}/a.md", TOKEN).status, 200)              # 見出し（Bearer）も受ける

    def test_cookie_alone_is_401_on_every_api_route(self):
        # クッキーは口（ポート）でなく名前で届く先が決まる。ファイル口で開いた html の JS がクッキーを読んで、
        # または付けたまま本体を叩いても、会話の道を開けず、アプリの知らせの枠も奪えない
        cookie = f"bb_token={TOKEN}; {self.cookie()}"                                   # 旧い名前も、起点の鍵も
        R = self.R
        for path in ["/v1/sessions", f"/v1/sessions/{B}/transcript", f"/v1/sessions/{B}/images/u-img/0",
                     f"/v1/sessions/{B}/written", f"/v1/sessions/{B}/resolve?path=a.md", f"/v1/roots/{R}/tree",
                     f"/v1/filekey?rootId={R}", "/v1/usage", "/v1/nope"]:
            self.assert_error(get(self.api, path, Cookie=cookie), 401, "unauthorized", path)
        self.assertEqual(get(self.api, "/v1/health", Cookie=cookie).status, 200)          # health は誰にでも開いたまま
        conns = [SSE(self.api) for _ in range(4)]                                      # アプリの知らせで枠が埋まっている
        self.addCleanup(lambda: [c.close() for c in conns])
        self.assertEqual([c.status for c in conns], [200] * 4)
        for _ in range(2):
            with SSE(self.api, token=None, Cookie=cookie) as page:
                self.assertEqual(page.status, 401)
                self.assertEqual(json.loads(page.resp.read()), {"error": "unauthorized"})
        for c in conns:                                                                # 1本も追い出されない
            self.assertTrue(c.alive_for(3 * self.PING))

    def test_non_get_is_405(self):
        for port, path in ((self.api, "/v1/sessions"), (self.api, "/v1/health"), (self.api, "/v1/nope"),
                           (self.files, f"/{self.R}/a.md")):
            for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "PROPFIND", "HEAD"):
                for tok in (None, TOKEN):
                    r = call(method, port, path, tok)
                    msg = (port, path, method, tok)
                    self.assertEqual(r.status, 405, msg)
                    self.assertEqual(r.headers["Allow"], "GET", msg)       # HEAD も 405（GET だけ。例外は POST /v1/pair）
                    if method != "HEAD":
                        self.assertEqual(r.json(), {"error": "method_not_allowed"}, msg)
                        continue
                    status, fields, body = head_raw(port, path, tok)          # 生のソケットで：HEAD の応答には本体を付けない
                    self.assertEqual((status, fields["allow"], body), (405, "GET", b""), msg)
                    self.assertEqual(fields["content-length"], str(len(json.dumps({"error": "method_not_allowed"}))), msg)

    def test_no_cors_headers(self):
        replies = [get(self.api, "/v1/health"), get(self.api, "/v1/sessions", TOKEN), self.tree(),
                   get(self.files, f"/{self.R}/a.md", TOKEN), get(self.api, "/v1/sessions"),
                   get(self.api, "/v1/nope", TOKEN), get(self.files, f"/{self.R}/nope.md", TOKEN),
                   call("OPTIONS", self.api, "/v1/sessions", Origin=f"http://{HOST}:{self.files}",
                        **{"Access-Control-Request-Method": "GET"})]
        for r in replies:
            self.assertEqual(cors(r), [], r.status)
        with SSE(self.api) as ev:
            self.assertEqual(ev.status, 200)
            self.assertEqual([k for k in ev.resp.headers if k.lower().startswith("access-control-")], [])

    def test_every_api_reply_says_no_store(self):
        # 本体の応答（JSON・貼った画像・エラー）を、iPad の URLSession や途中のキャッシュに残させない。見出しは1つだけ
        replies = {
            "health": (get(self.api, "/v1/health"), 200),
            "sessions": (get(self.api, "/v1/sessions", TOKEN), 200),
            "transcript": (get(self.api, f"/v1/sessions/{B}/transcript", TOKEN), 200),
            "image": (get(self.api, f"/v1/sessions/{B}/images/u-img/0", TOKEN), 200),
            "written": (get(self.api, f"/v1/sessions/{B}/written", TOKEN), 200),
            "resolve": (self.resolve("a.md"), 200),
            "tree": (self.tree(), 200),
            "filekey": (get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN), 200),
            "filekey 404": (get(self.api, "/v1/filekey?rootId=0000000000000000", TOKEN), 404),
            "usage": (get(self.api, "/v1/usage", TOKEN), 200),
            "401": (get(self.api, "/v1/sessions"), 401),
            "404": (get(self.api, "/v1/nope", TOKEN), 404),
            "404 session": (get(self.api, "/v1/sessions/session_9/transcript", TOKEN), 404),
            "405": (call("POST", self.api, "/v1/sessions", TOKEN), 405),
            "file": (get(self.files, f"/{self.R}/a.md", TOKEN), 200),
            "file 404": (get(self.files, f"/{self.R}/nope.md", TOKEN), 404),
        }
        for name, (r, status) in replies.items():
            self.assertEqual(r.status, status, name)
            self.assertEqual(r.headers.get_all("Cache-Control"), ["no-store"], name)
        with mock.patch.object(self.store, "fetch", side_effect=RuntimeError("x")), self.assertLogs("bblist.web", "WARNING"):
            r = get(self.api, f"/v1/sessions/{B}/transcript", TOKEN)
        self.assertEqual((r.status, r.headers.get_all("Cache-Control")), (500, ["no-store"]))
        with SSE(self.api) as ev:
            self.assertEqual((ev.status, ev.resp.headers.get_all("Cache-Control")), (200, ["no-store"]))

    def test_refuses_non_tailscale_host(self):
        # "0" は 0.0.0.0 と同じ意味になる。100.128.0.1 は 100.64.0.0/10 のすぐ外
        for host in ("192.168.1.10", "0", "0.0.0.0", "8.8.8.8", "100.128.0.1"):
            with self.assertRaises(ValueError, msg=host):
                make_servers(host, 0, 0, self.ctx)

    def test_log_has_no_body_or_token(self):
        (self.root / "naisho-dir").mkdir()
        (self.root / "naisho-dir" / "naisho.md").write_text(FILE_TEXT, encoding="utf-8")
        wrong = "wrong-token-xyz"
        with capture_log() as rec:
            asked = [
                (self.api, "/v1/health", None, 200),
                (self.api, f"/v1/sessions/{B}/transcript", TOKEN, 200),
                (self.api, f"/v1/sessions/{B}/written", TOKEN, 200),
                (self.api, f"/v1/sessions/{B}/resolve?path=naisho-dir/naisho.md", TOKEN, 200),
                (self.api, f"/v1/roots/{self.R}/tree?path=naisho-dir", TOKEN, 200),
                (self.api, f"/v1/sessions/{B}/images/u-img/0", TOKEN, 200),
                (self.files, f"/{self.R}/naisho-dir/naisho.md", TOKEN, 200),
                (self.api, "/v1/sessions", wrong, 401),
                (self.files, f"/{self.R}/naisho-dir/naisho.md", wrong, 401),
            ]
            for port, path, tok, status in asked:
                self.assertEqual(get(port, path, tok).status, status, path)
            self.assertEqual(get(self.files, f"/{self.R}/naisho-dir/naisho.md", Cookie=self.cookie()).status, 200)
            self.assertEqual(get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN).status, 200)
            self.assertTrue(wait_until(lambda: len(rec.lines) >= len(asked) + 2))   # 記録は応答を送った後に書く
        text = "\n".join(rec.lines)
        for secret in (TOKEN, wrong, SECRET_TEXT, FILE_TEXT, "naisho", B, self.R, "u-img", "見て",
                       self.cookie().split("=")[1], SECRET.hex()):
            self.assertNotIn(secret, text)
        for ln in rec.lines:                                     # 1要求1行：口・方法・道の形・状態コード・ms
            self.assertRegex(ln, r"\A(api|files) GET \S+ \d{3} \d+ms\Z")
        for want in ("api GET /v1/health 200", "api GET /v1/sessions/*/transcript 200",
                     "api GET /v1/sessions/*/images/*/* 200", "api GET /v1/roots/*/tree 200",
                     "api GET /v1/sessions/*/resolve 200", "files GET /*/* 200", "api GET /v1/sessions 401",
                     "api GET /v1/filekey 200"):
            self.assertIn(want, text)

    def test_unexpected_error_is_500_with_type_name_only(self):
        with mock.patch.object(self.store, "fetch", side_effect=RuntimeError(f"secret-content {SECRET_TEXT}")):
            with self.assertLogs("bblist.web", level="WARNING") as cm:
                r = get(self.api, f"/v1/sessions/{B}/transcript", TOKEN)
        self.assert_error(r, 500, "unreadable")
        text = "\n".join(cm.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret-content", text)
        self.assertNotIn(SECRET_TEXT, text)


# ---------------------------------------------------------------- 認証の前の守り

class HealthTest(WebCase):
    def test_health_is_minimal(self):
        # 認証なしで答える道は、動いているかと API の版だけ。アプリの版・起動時刻は /v1/diagnostics（認証あり）へ
        for tok in (None, "x", TOKEN):
            r = get(self.api, "/v1/health", tok)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.json(), {"ok": True, "apiVersion": 3})   # 認証・クッキー・組む口が変わった
            self.assertNotIn(b"startedAt", r.body)
            self.assertNotIn(b"version\"", r.body)


class DiagnosticsTest(WebCase):
    """GET /v1/diagnostics：認証あり。Claude Code の形が合わないものを黙って落とさず数えて返す。

    数えるのは見回り・会話ログの読みのとき。要求のたびにファイルを数えない。パス・rootId・本文は出さない。
    """

    def test_diagnostics_requires_auth(self):
        for tok in (None, "x", TOKEN + "x"):
            self.assert_error(get(self.api, "/v1/diagnostics", tok), 401, "unauthorized")
        self.assert_error(get(self.api, "/v1/diagnostics", Cookie=self.cookie()), 401, "unauthorized")
        self.assert_error(get(self.api, f"/v1/diagnostics?token={TOKEN}"), 401, "unauthorized")
        self.assertEqual(get(self.api, "/v1/diagnostics", TOKEN).status, 200)
        self.assert_error(call("POST", self.api, "/v1/diagnostics", TOKEN), 405, "method_not_allowed")
        # ファイル口には無い（<rootId>/<相対パス> の形として 401/404 になるだけ）
        self.assertEqual(get(self.files, "/v1/diagnostics", TOKEN).status, 404)

    def test_diagnostics_shape(self):
        r = get(self.api, "/v1/diagnostics", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"], r.headers["Cache-Control"]), (200, JSON, "no-store"))
        self.assertEqual(r.json(), {
            "version": VERSION, "apiVersion": 3, "startedAt": STARTED, "claudeCodeVersions": ["2.1.306"],
            "sessions": {"files": 2, "malformed": 0, "dead": 0, "procMismatch": 0, "noBridge": 0},
            "transcripts": {"unreadableLines": 0, "unknownTypes": 0, "unknownTypeNames": [], "unknownOriginKinds": 0},
            "gitignore": {"available": True, "showIgnored": False},
            "tcc": {"blockedRoots": 0},
            "previewExternal": True})

    def test_diagnostics_counts(self):
        self.add(3, live=False, version="2.1.200")                                     # 死んだ
        self.add(4, ps="Mon Oct 26 10:00:00 2026")                                     # procStart が ps と合わない
        self.add(5, drop=("bridgeSessionId",))                                         # bridgeSessionId なし
        self.write("broken.json", "not json")
        self.hub.step()                                                                # 一覧を読み直すのは見回りだけ
        # 会話ログ：JSON に直せない行・知らない種類の type の行（読み飛ばす行）・知らない origin.kind の人の指示。
        # 知っている描かない種類（api-request）は数えない
        with open(self.logs / f"{SID}.jsonl", "ab") as f:
            f.write(b'{"type":"user","message":"broken\n' + line({"type": "api-request", "id": "r1", "timestamp": TS})
                    + line({"type": "future-kind", "id": "r2", "timestamp": TS})
                    + line({**human("u-new", "x"), "origin": {"kind": "person"}}))
        with self.assertLogs("bblist.transcript", "WARNING"):
            self.assertEqual(get(self.api, f"/v1/sessions/{B}/transcript", TOKEN).status, 200)
        d = self.diagnostics()
        self.assertEqual(d["sessions"], {"files": 6, "malformed": 1, "dead": 1, "procMismatch": 1, "noBridge": 1})
        self.assertEqual(d["claudeCodeVersions"], ["2.1.200", "2.1.306"])
        self.assertEqual(d["transcripts"], {"unreadableLines": 1, "unknownTypes": 1, "unknownTypeNames": ["future-kind"],
                                            "unknownOriginKinds": 1})

    def test_diagnostics_does_not_scan_or_refresh_per_request(self):
        with mock.patch.object(self.catalog, "refresh") as refresh, \
                mock.patch("bblist.sessions.Path.glob", side_effect=AssertionError("scan")), \
                mock.patch.object(self.store, "log_path", side_effect=AssertionError("scan")):
            for _ in range(3):
                self.assertEqual(get(self.api, "/v1/diagnostics", TOKEN).status, 200)
        refresh.assert_not_called()

    def test_diagnostics_carries_no_paths_ids_or_text(self):
        body = get(self.api, "/v1/diagnostics", TOKEN).body.decode("utf-8")
        for needle in (str(self.tmp), str(self.home), self.R, self.R_HOME, B, SID, SECRET_TEXT, TOKEN, self.DEV, "Test iPad"):
            self.assertNotIn(needle, body, needle)

    def test_diagnostics_is_in_the_request_log_as_a_shape_only(self):
        rec = Records()
        logging.getLogger("bblist.web").addHandler(rec)
        self.addCleanup(logging.getLogger("bblist.web").removeHandler, rec)
        before = logging.getLogger("bblist.web").level
        logging.getLogger("bblist.web").setLevel(logging.INFO)
        self.addCleanup(logging.getLogger("bblist.web").setLevel, before)
        get(self.api, "/v1/diagnostics?x=1", TOKEN)
        self.assertTrue(any("/v1/diagnostics 200" in ln for ln in rec.lines), rec.lines)
        self.assertFalse(any("x=1" in ln for ln in rec.lines))

    def test_diagnostics_reports_gitignore_state(self):
        self.ctx.gitignore = GitIgnore(show_ignored=True)
        self.assertEqual(self.diagnostics()["gitignore"], {"available": True, "showIgnored": True})
        self.ctx.gitignore = GitIgnore(show_ignored=False, git=lambda directory, names, root=None: None)
        self.assertEqual(self.diagnostics()["gitignore"], {"available": True, "showIgnored": False})   # まだ聞いていない


class ServerHeaderTest(WebCase):
    def test_server_header_does_not_advertise_the_version(self):
        # 認証の前に答える応答（health・401・421・404・405・見出しが大きすぎる 431）の Server 見出しに、版を書かない
        replies = [get(self.api, "/v1/health"), get(self.api, "/v1/sessions"), get(self.api, "/nope"),
                   get(self.api, "/v1/health", Host="rebind.example"), call("POST", self.api, "/v1/health"),
                   get(self.files, f"/{self.R}/a.md"), get(self.files, "/nope", TOKEN)]
        for r in replies:
            server = r.headers.get_all("Server")
            self.assertEqual([v.strip() for v in server], ["bblist"], r.status)
            self.assertNotIn(VERSION, " ".join(server))
        raw = exchange_raw(self.api, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n")
        if raw:                                                  # 431 の応答（HTML のエラーページ）も。切られたら応答が無い
            head = raw.partition(b"\r\n\r\n")[0].decode("latin-1")
            self.assertIn("Server: bblist", head)
            self.assertNotRegex(head, r"Server: [^\r\n]*\d")


class TotalLimitTest(WebCase):
    CTX = {"max_conns": 4, "max_conns_per_ip": 99, "header_timeout": 30.0}

    def test_total_connection_limit(self):
        held = [open_idle(self.api) for _ in range(4)]          # 見出しを送らない接続で枠を埋める
        for s in held:
            self.addCleanup(s.close)
        t0 = time.monotonic()
        with capture_log() as rec:                               # 断る記録は10秒に1行。つなぐ前から取り始める（先に断られると行を落とす）
            fifth = open_idle(self.api)
            self.addCleanup(fifth.close)
            self.assertTrue(closed_within(fifth, 1.0), "上限を超えた接続がすぐ閉じられない")
            sixth = open_idle(self.api)
            self.addCleanup(sixth.close)
            self.assertTrue(closed_within(sixth, 1.0))
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(rec.lines, ["Refused 1 connection(s) over the limit (total)"])   # 洪水の間は10秒に1行。送り元は書かない
        for s in held:                                           # 先に入った4本は切られない
            self.assertFalse(closed_within(s, 0.05))
        other = open_idle(self.files)                            # ファイル口の数は別（片方への洪水でもう片方を止めない）
        self.addCleanup(other.close)
        self.assertFalse(closed_within(other, 0.2))
        held.pop().close()                                       # 1本閉じれば枠が戻り、次の要求が通る
        self.assertTrue(wait_until(lambda: self.servers[0].active_connections() == 3), "閉じた接続の枠が戻らない")
        self.assertEqual(get(self.api, "/v1/health").status, 200)


class PerIpLimitTest(WebCase):
    CTX = {"max_conns": 64, "max_conns_per_ip": 2, "header_timeout": 30.0}

    def test_per_ip_connection_limit(self):
        a, b = open_idle(self.api), open_idle(self.api)          # 送り元はどれも 127.0.0.1
        third = open_idle(self.api)
        for s in (a, b, third):
            self.addCleanup(s.close)
        self.assertTrue(closed_within(third, 1.0), "同じ送り元の3本目がすぐ閉じられない")
        self.assertFalse(closed_within(a, 0.05))
        self.assertFalse(closed_within(b, 0.05))
        a.close()                                                # 枠が戻れば、同じ送り元の次の接続が要求を通せる
        self.assertTrue(wait_until(lambda: self.servers[0].active_connections() == 1))
        status, _ = exchange(self.api, b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertEqual(status, 200)

    def test_per_ip_counts_are_separate(self):
        server = self.servers[0]
        self.assertIsNone(server.reserve("100.64.0.1"))
        self.assertIsNone(server.reserve("100.64.0.1"))
        self.assertEqual(server.reserve("100.64.0.1"), "per_ip")
        self.assertIsNone(server.reserve("100.64.0.2"))          # 別の送り元には効かない
        for ip in ("100.64.0.1", "100.64.0.1", "100.64.0.2"):
            server.release(ip)
        self.assertEqual((server.active_connections(), server._per_ip), (0, {}))


class HeaderTimeoutTest(WebCase):
    CTX = {"header_timeout": 0.5}

    def test_header_timeout(self):
        s = open_idle(self.api)
        self.addCleanup(s.close)
        s.sendall(b"G")                                          # 1バイトだけ送って黙る
        t0 = time.monotonic()
        self.assertTrue(closed_within(s, 1.0), "見出しが終わらない接続が閉じられない")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(get(self.api, "/v1/health").status, 200)   # そのあとも答える

    def test_one_request_per_connection(self):
        # 見出しの締め切りは接続の最初の要求だけに掛かる（_HeaderReader）。HTTP/1.1 の keep-alive にするなら掛け直しが要る
        self.assertEqual(web._Handler.protocol_version, "HTTP/1.0")
        s = open_idle(self.api)
        self.addCleanup(s.close)
        s.sendall(b"GET /v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
        s.settimeout(2)
        data = b""
        while chunk := s.recv(4096):                             # 応答のあと、サーバーが閉じる
            data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200 "))

    def test_nothing_sent_also_times_out(self):
        s = open_idle(self.api)
        self.addCleanup(s.close)
        self.assertTrue(closed_within(s, 1.0))

    def test_deadline_covers_the_whole_header_not_each_read(self):
        # 0.15秒ごとに1バイト：1回ずつの待ちは 0.5秒に届かないが、見出し全体では締め切りを過ぎる
        s = open_idle(self.api)
        stop = threading.Event()

        def drip():
            for ch in b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: " + b"a" * 200:
                if stop.is_set():
                    return
                try:
                    s.sendall(bytes([ch]))
                except OSError:
                    return
                time.sleep(0.15)

        t = threading.Thread(target=drip)
        t.start()
        self.addCleanup(s.close)
        self.addCleanup(t.join)
        self.addCleanup(stop.set)                                # 後始末は逆順：止めてから待ち、そのあと閉じる
        t0 = time.monotonic()
        self.assertTrue(closed_within(s, 1.5), "1バイトずつ送り続ける接続が閉じられない")
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_headers_in_time_are_served(self):
        self.ctx.header_timeout = 1.5                            # 1回ごとに接続を作る糸が読む。0.3秒の送り方に余裕を持たせる
        s = open_idle(self.api)
        self.addCleanup(s.close)
        for part in (b"GET /v1/health HTTP/1.1\r\n", b"Host: 127.0.0.1\r\n", b"\r\n"):
            s.sendall(part)
            time.sleep(0.1)
        s.settimeout(2)
        self.assertTrue(s.recv(64).startswith(b"HTTP/1.0 200 "))

    def test_longer_timeout_applies_once_headers_are_read(self):
        seen = []
        real = web._ApiHandler._health

        def spy(handler, query):
            seen.append(handler.connection.gettimeout())
            return real(handler, query)

        with mock.patch.object(web._ApiHandler, "_health", spy):
            self.assertEqual(get(self.api, "/v1/health").status, 200)
        self.assertEqual(seen, [web._Handler.timeout])           # 0.5 のままではない

    def test_sse_outlives_the_header_timeout(self):
        with SSE(self.api) as ev:
            self.assertEqual(ev.status, 200)
            self.assertTrue(ev.alive_for(1.5))                   # 見出しの時間（0.5秒）の3倍、ping が続く


class HeaderSizeTest(WebCase):
    REQ = b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n%s\r\n"

    def test_oversized_header_rejected(self):
        too_long = b"X-Big: " + b"a" * 9000 + b"\r\n"                              # 1行が 8 KiB を超える
        too_many = b"".join(b"X-H%d: 1\r\n" % i for i in range(33))                # 見出しが 33 本
        for name, extra in (("long line", too_long), ("many headers", too_many)):
            status, _ = exchange(self.api, self.REQ % extra)
            self.assertIn(status, (400, 431, None), name)        # 400 台で断るか、答えずに切る。通してはいけない
        self.assertEqual(exchange(self.api, self.REQ % b"")[0], 200)

    def test_header_count_boundary_is_32_including_host(self):
        # 標準は見出しの終わりの空行も数えるので、_MAXHEADERS をそのまま 32 にすると 31 本までしか通らない
        def request(total: int) -> bytes:                        # Host を含めて total 本の見出し
            return self.REQ % b"".join(b"X-H%d: 1\r\n" % i for i in range(total - 1))
        self.assertEqual(exchange(self.api, request(32))[0], 200)
        self.assertEqual(exchange(self.api, request(33))[0], 431)

    def test_headers_just_inside_the_limits_pass(self):
        long_ok = b"X-Big: " + b"a" * 8000 + b"\r\n"
        many_ok = b"".join(b"X-H%d: 1\r\n" % i for i in range(20))
        self.assertEqual(exchange(self.api, self.REQ % long_ok)[0], 200)
        self.assertEqual(exchange(self.api, self.REQ % many_ok)[0], 200)


class HostTest(WebCase):
    CTX = {"allowed_hosts": frozenset({"127.0.0.1", "Mac.Example.ts.net", "100.101.102.103"})}   # 大文字は直して比べる

    def test_host_header_checked(self):
        for port in (self.api, self.files):
            for path in ("/v1/health", "/v1/sessions", f"/{self.R}/a.md", "/nope"):
                for tok in (None, TOKEN):                        # 合言葉が合っていても、Host が違えば断る
                    r = get(port, path, tok, Host="rebind.example")
                    self.assert_error(r, 421, "bad_host", (port, path, tok))
                    self.assertEqual(r.headers.get_all("Cache-Control"), ["no-store"])
        self.assert_error(call("POST", self.api, "/v1/sessions", TOKEN, Host="rebind.example"), 421, "bad_host")
        for port in (self.api, self.files):                      # HEAD への 421 も本体なし（見出しだけ）
            status, fields, body = head_raw(port, "/v1/health", Host="rebind.example")
            self.assertEqual((status, body), (421, b""), port)
            self.assertEqual(fields["content-length"], str(len(json.dumps({"error": "bad_host"}))), port)
        for host in ("127.0.0.1", f"127.0.0.1:{self.api}", "mac.example.ts.net", "MAC.EXAMPLE.TS.NET:8772",
                     "mac.example.ts.net.", "100.101.102.103:8772"):
            self.assertEqual(get(self.api, "/v1/health", Host=host).status, 200, host)
            self.assertEqual(get(self.api, "/v1/sessions", TOKEN, Host=host).status, 200, host)
            self.assertEqual(get(self.files, f"/{self.R}/a.md", TOKEN, Host=host).status, 200, host)

    def test_lookalike_and_malformed_hosts_are_refused(self):
        for host in ("", "evil.example", "127.0.0.1.evil.example", "mac.example.ts.net.evil.example",
                     "evil.mac.example.ts.net", "127.0.0.1:abc", "127.0.0.1:", "127.0.0.1:80:90",
                     "127.0.0.1@evil.example", "evil.example@127.0.0.1", "localhost", "[::1]", "::1", "[127.0.0.1", "[127.0.0.1]", "[::1"):
            self.assert_error(get(self.api, "/v1/health", Host=host), 421, "bad_host", repr(host))

    def test_missing_duplicate_and_non_ascii_host_are_refused(self):
        raw = {
            "no Host (HTTP/1.0)": b"GET /v1/health HTTP/1.0\r\n\r\n",
            "no Host (HTTP/1.1)": b"GET /v1/health HTTP/1.1\r\n\r\n",
            "two Hosts": b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: 127.0.0.1\r\n\r\n",
            "non-ASCII": "GET /v1/health HTTP/1.1\r\nHost: ホスト\r\n\r\n".encode("utf-8"),
        }
        for name, request in raw.items():
            status, body = exchange(self.api, request)
            self.assertEqual((status, json.loads(body)), (421, {"error": "bad_host"}), name)
        self.assertEqual(exchange(self.api, b"GET /v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")[0], 200)

    def test_bad_host_is_logged_without_the_host(self):
        with capture_log() as rec:
            self.assertEqual(get(self.api, "/v1/health", Host="rebind.example").status, 421)
            self.assertTrue(wait_until(lambda: len(rec.lines) >= 1))
        self.assertRegex(rec.lines[0], r"\Aapi GET /v1/health 421 \d+ms\Z")
        self.assertNotIn("rebind", "\n".join(rec.lines))


class DefaultHostTest(WebCase):
    def test_loopback_names_pass_by_default(self):
        for host in ("127.0.0.1", "localhost", "LOCALHOST:8772"):
            self.assertEqual(get(self.api, "/v1/health", Host=host).status, 200, host)
        self.assert_error(get(self.api, "/v1/health", Host="rebind.example"), 421, "bad_host")


class AllowedHostsTest(unittest.TestCase):
    def test_host_name(self):
        for value, want in (("127.0.0.1", "127.0.0.1"), ("127.0.0.1:8772", "127.0.0.1"), ("Mac.TS.net:80", "mac.ts.net"),
                            ("mac.ts.net.", "mac.ts.net"), ("[::1]", "::1"), ("[FD7A::1]:8772", "fd7a::1"),
                            (None, None), ("", None), (":80", None), ("a:", None), ("a:b", None), ("a:1:2", None),
                            ("a:\u0661", None), ("[::1", None), ("[::1]x", None), ("[::1]:", None),
                            ("[127.0.0.1]", None)):
            self.assertEqual(web._host_name(value), want, value)

    def test_build_allowed_hosts(self):
        loopback = {"127.0.0.1", "localhost"}
        self.assertEqual(build_allowed_hosts("100.101.102.103"), frozenset(loopback | {"100.101.102.103"}))
        self.assertEqual(build_allowed_hosts("100.101.102.103", "Mac.Example.ts.net."),     # 大文字・末尾の点は直す
                         frozenset(loopback | {"100.101.102.103", "mac.example.ts.net"}))
        self.assertEqual(build_allowed_hosts("127.0.0.1", ""), frozenset(loopback))          # 空の名前は入れない


class SlotsComeBackTest(WebCase):
    """どの抜け方（正常・エラー・相手が消えた・SSE が終わる）でも、接続の枠が戻る。"""
    CTX = {"header_timeout": 0.5}

    def open_now(self) -> int:
        return sum(s.active_connections() for s in self.servers)

    def back(self, label: str) -> None:
        self.assertTrue(wait_until(lambda: self.open_now() == 0, 4.0), f"{label}：枠が戻らない（{self.open_now()}）")
        self.assertEqual([s._per_ip for s in self.servers], [{}, {}], label)

    def test_slots_come_back_on_every_exit_path(self):
        get(self.api, "/v1/health")
        self.back("normal")
        get(self.api, "/v1/sessions")                                                    # 401
        get(self.api, "/v1/nope", TOKEN)                                                 # 404
        call("POST", self.api, "/v1/sessions", TOKEN)                                    # 405
        get(self.api, "/v1/health", Host="rebind.example")                               # 421
        get(self.files, f"/{self.R}/a.md", TOKEN)
        self.back("errors answered")
        with mock.patch.object(self.store, "fetch", side_effect=RuntimeError("x")), self.assertLogs("bblist.web", "WARNING"):
            self.assertEqual(get(self.api, f"/v1/sessions/{B}/transcript", TOKEN).status, 500)
        self.back("500")
        exchange(self.api, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n")
        self.back("header too large")
        open_idle(self.api).close()                                                      # つないですぐ消えた
        s = open_idle(self.api)
        s.sendall(b"GET /v1/he")                                                         # 見出しの途中で消えた
        s.close()
        self.back("client gone")
        s = open_idle(self.api)
        s.sendall(b"G")
        self.assertTrue(closed_within(s, 2.0))                                           # 見出しの時間切れ
        s.close()
        self.back("header timeout")
        s = open_idle(self.files)                                                        # 返す途中で消えた（大きなファイル）
        s.sendall(f"GET /{self.R}/big.bin HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
        s.close()
        self.back("client gone while sending")
        SSE(self.api).close()                                                            # 眠った・消えた iPad
        self.back("SSE client gone")
        conns = [SSE(self.api) for _ in range(5)]                                        # 5本目が一番古いのを追い出す
        self.assertEqual([c.status for c in conns], [200] * 5)
        self.assertTrue(conns[0].ends(), "追い出された接続が終わらない")
        self.assertTrue(wait_until(lambda: self.open_now() == 4), self.open_now())   # 枠は閉じたあとに戻る。相手の EOF が先に見える
        for c in conns:
            c.close()
        self.back("SSE evicted and closed")


# ---------------------------------------------------------------- 本体の道

class ApiTest(WebCase):
    def test_sessions_and_usage(self):
        r = get(self.api, "/v1/sessions", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        j = r.json()
        self.assertEqual({s["bridgeSessionId"] for s in j["sessions"]}, {B, B_HOME})
        self.assertEqual({p["rootId"]: p["filesVisible"] for p in j["projects"]}, {self.R: True, self.R_HOME: False})
        self.assertEqual(get(self.api, "/v1/usage", TOKEN).json(), {"available": False})
        self.usage.write_text(json.dumps({"capturedAt": 1792941510, "rate_limits": {
            "five_hour": {"used_percentage": 41.2, "resets_at": 1792950000}}}), encoding="utf-8")
        self.assertEqual(get(self.api, "/v1/usage", TOKEN).json(), {
            "available": True, "capturedAt": 1792941510,
            "fiveHour": {"usedPercentage": 41.2, "resetsAt": 1792950000}, "sevenDay": None})

    def test_transcript_merges_status_from_catalog(self):
        self.add(1, cwd=str(self.root), status="waiting", waitingFor="permission prompt", statusUpdatedAt=1792941999999)
        self.hub.step()                                         # 一覧を読み直すのは見回りだけ
        r = get(self.api, f"/v1/sessions/{B}/transcript", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        j = r.json()
        self.assertEqual(set(j), {"bridgeSessionId", "sessionId", "cursor", "reset", "items", "updates", "state"})
        self.assertEqual((j["bridgeSessionId"], j["sessionId"], j["reset"], j["updates"]), (B, SID, False, []))
        self.assertEqual(j["state"], {
            "status": "waiting", "waitingFor": "permission prompt", "statusUpdatedAt": 1792941999999,   # catalog
            "lastRecordAt": TS_LAST, "pendingCall": {"name": "Bash", "brief": "git status", "since": TS},  # store
            "queue": {"count": 1, "texts": ["待っている指示"]}})
        self.assertEqual([(it["kind"], it["id"]) for it in j["items"]],
                         [("instruction", "u1"), ("instruction", "u-img"), ("instruction", "u-odd"), ("text", "a1")])
        self.assertEqual(j["items"][0]["text"], SECRET_TEXT)
        self.assertEqual(j["items"][1]["images"], [{"n": 0, "mediaType": "image/png", "bytes": len(PNG_1x1)}])
        # cursor で差分だけ
        nxt = get(self.api, f"/v1/sessions/{B}/transcript?cursor={quote(j['cursor'])}", TOKEN).json()
        self.assertEqual((nxt["items"], nxt["reset"]), ([], False))
        self.append_log(human("u2", "足した"))
        nxt = get(self.api, f"/v1/sessions/{B}/transcript?cursor={quote(nxt['cursor'])}", TOKEN).json()
        self.assertEqual([it["id"] for it in nxt["items"]], ["u2"])
        self.assertEqual(nxt["state"]["status"], "waiting")

    def test_unknown_session_404(self):
        for path in ["/v1/sessions/session_9/transcript", "/v1/sessions/session_9/images/u-img/0",
                     "/v1/sessions/session_9/written", "/v1/sessions/session_9/resolve?path=a.md",
                     "/v1/roots/0000000000000000/tree", "/v1/nope", "/v1/sessions/", "/", "/v1/health/x",
                     "/v1/sessions//transcript", "/v1/sessions/session_1/transcript/x"]:
            self.assert_error(get(self.api, path, TOKEN), 404, "not_found", path)
        for path in ["/0000000000000000/a.md", "/", f"/{self.R}", f"/{self.R}/"]:
            self.assert_error(get(self.files, path, TOKEN), 404, "not_found", path)

    def test_session_that_ended_is_404(self):   # プロセスが終わった
        self.assertEqual(get(self.api, f"/v1/sessions/{B}/transcript", TOKEN).status, 200)
        (self.sessions / "1.json").unlink()
        self.live.discard(1)
        self.hub.step()
        self.assert_error(get(self.api, f"/v1/sessions/{B}/transcript", TOKEN), 404, "not_found")
        self.assertEqual(get(self.api, "/v1/health").status, 200)

    def test_image_route_content_type(self):
        r = get(self.api, f"/v1/sessions/{B}/images/u-img/0", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"], r.body), (200, "image/png", PNG_1x1))
        self.assertEqual(r.headers["Content-Length"], str(len(PNG_1x1)))
        # 画像の種類でないもの（会話ログに書かれた media_type）はそのまま返さない
        r = get(self.api, f"/v1/sessions/{B}/images/u-odd/0", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "application/octet-stream"))
        for rest in ("u-img/1", "u-img/x", "u-img/-1", "u-img/" + quote("０"), "u-img/" + "9" * 30, "u1/0", "none/0"):
            self.assert_error(get(self.api, f"/v1/sessions/{B}/images/{rest}", TOKEN), 404, "not_found", rest)

    def test_written_and_resolve(self):
        r = get(self.api, f"/v1/sessions/{B}/written", TOKEN)
        self.assertEqual(r.status, 200)
        j = r.json()
        self.assertEqual((j["bridgeSessionId"], j["sessionId"]), (B, SID))
        self.assertEqual(j["files"], [                         # 新しい順。見せてよいフォルダの外は null（見に行かない）
            {"path": str(self.outside / "x"), "rootId": None, "relPath": None, "lastTs": TS, "exists": None},
            {"path": str(self.root / "new.md"), "rootId": self.R, "relPath": "new.md", "lastTs": TS, "exists": False},
            {"path": str(self.root / "a.md"), "rootId": self.R, "relPath": "a.md", "lastTs": TS, "exists": True}])
        want = {"rootId": self.R, "relPath": "a.md", "preview": "md", "exists": True}
        for path in ("a.md", str(self.root / "a.md"), "./sub/../a.md"):
            r = self.resolve(path)
            self.assertEqual((r.status, r.json()), (200, want), path)
        self.assertEqual(self.resolve("sub/b.png").json()["preview"], "image")
        # フォルダ・起点そのもの・無いファイル・拒む名前・外・外へ出るリンクは 404
        for path in ("", ".", "sub", str(self.root), "new.md", ".env", ".git/config", str(self.outside / "x"),
                     "link_out/x", "\0"):
            self.assert_error(self.resolve(path), 404, "not_found", repr(path))

    def test_written_exists_is_null_when_its_folder_cannot_be_read(self):
        if os.geteuid() == 0:
            self.skipTest("root は chmod 000 でも読める")
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "in.md").write_text("x", encoding="utf-8")
        self.append_log(tool("a6", "t5", "Write", file_path=str(locked / "in.md")), result("r5", "t5"))
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        r = get(self.api, f"/v1/sessions/{B}/written", TOKEN)
        self.assertEqual(r.status, 200)
        row = [f for f in r.json()["files"] if f["path"] == str(locked / "in.md")]
        self.assertEqual([(f["rootId"], f["relPath"], f["exists"]) for f in row], [(self.R, "locked/in.md", None)])

    def test_plus_in_query_path_stays_a_plus(self):
        # ?path= の + は + のまま（空白に戻さない）。アプリは + を %2B にしなくてよい
        (self.root / "a+b.md").write_text("plus", encoding="utf-8")
        (self.root / "a b.md").write_text("space", encoding="utf-8")       # 空白の名前と取り違えない
        (self.root / "C++").mkdir()
        (self.root / "C++" / "m.cpp").write_text("z", encoding="utf-8")
        (self.root / "C  ").mkdir()                                          # + を空白に戻すと着いてしまうフォルダ
        (self.root / "C  " / "wrong.txt").write_text("z", encoding="utf-8")
        for query, rel in (("a+b.md", "a+b.md"), ("a%2Bb.md", "a+b.md"), ("a%20b.md", "a b.md")):
            r = get(self.api, f"/v1/sessions/{B}/resolve?path={query}", TOKEN)
            self.assertEqual((r.status, r.json()["relPath"]), (200, rel), query)
        for query in ("C++", "C%2B%2B"):
            r = get(self.api, f"/v1/roots/{self.R}/tree?path={query}", TOKEN)
            j = r.json()
            self.assertEqual((r.status, j["path"], [e["name"] for e in j["entries"]]), (200, "C++", ["m.cpp"]), query)

    def test_requests_never_refresh_the_catalog(self):
        # 一覧の読み直し（変わったかの印を消費する）は見回りだけがする
        with mock.patch.object(self.catalog, "refresh", wraps=self.catalog.refresh) as spy:
            for path in ("/v1/sessions", f"/v1/sessions/{B}/transcript", f"/v1/sessions/{B}/images/u-img/0",
                         f"/v1/sessions/{B}/written", f"/v1/sessions/{B}/resolve?path=a.md",
                         f"/v1/roots/{self.R}/tree", "/v1/usage", "/v1/health"):
                self.assertEqual(get(self.api, path, TOKEN).status, 200, path)
            self.assertEqual(get(self.files, f"/{self.R}/a.md", TOKEN).status, 200)
            with SSE(self.api, f"?watch={B}") as ev:
                self.assertEqual(ev.status, 200)
        spy.assert_not_called()


# ---------------------------------------------------------------- ツリーとファイル口

class FileTest(WebCase):
    def test_tree_and_file_port(self):
        r = self.tree()
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        j = r.json()
        self.assertEqual((j["rootId"], j["path"]), (self.R, ""))
        self.assertEqual([e["name"] for e in j["entries"]], ["sub", "a.md", "big.bin"])   # 拒む名前・外へのリンクは出ない
        j = self.tree("sub").json()
        self.assertEqual((j["rootId"], j["path"]), (self.R, "sub"))
        self.assertEqual([(e["name"], e["preview"]) for e in j["entries"]], [("b.png", "image")])
        r = get(self.files, f"/{self.R}/a.md", TOKEN)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.body.decode("utf-8"), f"# 見出し\n{FILE_TEXT}\n")
        self.assertEqual(r.headers["Content-Length"], str(len(r.body)))
        r = get(self.files, f"/{self.R}/big.bin", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"], r.body), (200, "application/octet-stream", self.big))
        r = get(self.files, f"/{self.R}/sub/b.png", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"], r.body), (200, "image/png", PNG_1x1))

    def test_file_port_denies(self):
        os.mkfifo(self.root / "pipe")                          # 開くと待ってしまう種類も、待たずに 404
        R = self.R
        # 拒む名前（名前だけで決まる。あるかどうかを見せない）は 403 hidden。ほかは 404
        for rel in ["../x", "%2e%2e/x", "..%2fx", ".env", ".git/config", "credentials.json", "k.pem", "x.sqlite"]:
            self.assert_error(get(self.files, f"/{R}/{rel}", TOKEN), 403, "forbidden", rel, reason="hidden")
        for rel in ["link_out/x", "sub", "sub/", "pipe", "nope.md", "a.md%00.png"]:
            self.assert_error(get(self.files, f"/{R}/{rel}", TOKEN), 404, "not_found", rel)
        for rel in ["..", ".env", ".git", "pipe/.."]:
            self.assert_error(self.tree(rel), 403, "forbidden", rel, reason="hidden")
        for rel in ["link_out", "pipe", "nope"]:
            self.assert_error(self.tree(rel), 404, "not_found", rel)

    def test_a_denied_name_is_hidden_whether_or_not_it_exists(self):   # 存在の有無を見せない
        for rel in (".env", "nope/.env", ".secret-not-there", "id_rsa", "AuthKey_X.p8"):
            self.assert_error(get(self.files, f"/{self.R}/{rel}", TOKEN), 403, "forbidden", rel, reason="hidden")
            self.assert_error(self.tree(rel), 403, "forbidden", rel, reason="hidden")

    def test_symlink_to_a_denied_name_stays_404_so_it_is_not_an_existence_oracle(self):
        (self.root / "plain").symlink_to(self.root / ".env")        # 名前は無害、行き先が拒む名前
        self.assert_error(get(self.files, f"/{self.R}/plain", TOKEN), 404, "not_found")
        self.assert_error(get(self.files, f"/{self.R}/plain2", TOKEN), 404, "not_found")   # 無いものと同じ

    def test_file_port_nfc_request_for_nfd_name(self):
        pa, bi, da = "ぱ.md", "び.md", "データ"
        for s in (pa, bi, da):
            self.assertNotEqual(nfd(s), s)
        (self.root / nfd(pa)).write_text("NFD の名前", encoding="utf-8")
        (self.root / bi).write_text("NFC の名前", encoding="utf-8")
        (self.root / nfd(da)).mkdir()
        (self.root / nfd(da) / "c.md").write_text("NFD のフォルダ", encoding="utf-8")
        for rel, body in ((quote(pa), "NFD の名前"), (quote(nfd(bi)), "NFC の名前"), (quote(f"{da}/c.md"), "NFD のフォルダ")):
            r = get(self.files, f"/{self.R}/{rel}", TOKEN)
            self.assertEqual((r.status, r.body.decode("utf-8")), (200, body), rel)
        self.assertIn(nfd(pa), [e["name"] for e in self.tree().json()["entries"]])      # ツリーはディスクにある綴りのまま
        self.assertEqual([e["name"] for e in self.tree(da).json()["entries"]], ["c.md"])
        r = self.resolve(pa)
        self.assertEqual((r.status, r.json()["rootId"]), (200, self.R))

    def test_invisible_root_is_403_hidden_and_an_unknown_root_is_404(self):
        (self.home / "visible.md").write_text("ホームのファイル", encoding="utf-8")
        self.assertIn(self.R_HOME, {s["rootId"] for s in get(self.api, "/v1/sessions", TOKEN).json()["sessions"]})
        # 見せない起点（一覧には filesVisible: false で出ている）：中身は返さず、理由を言う
        self.assert_error(get(self.api, f"/v1/roots/{self.R_HOME}/tree", TOKEN), 403, "forbidden", reason="hidden")
        self.assert_error(get(self.files, f"/{self.R_HOME}/visible.md", TOKEN), 403, "forbidden", reason="hidden")
        # 知らない起点・resolve は今までどおり 404
        for rid in ("0000000000000000", "x"):
            self.assert_error(get(self.api, f"/v1/roots/{rid}/tree", TOKEN), 404, "not_found", rid)
            self.assert_error(get(self.files, f"/{rid}/visible.md", TOKEN), 404, "not_found", rid)
        self.assert_error(self.resolve("visible.md", bridge=B_HOME), 404, "not_found")
        self.assert_error(self.resolve(str(self.home / "visible.md")), 404, "not_found")

    def test_permission_error_is_403(self):   # macOS の守り・chmod で読めない
        if os.geteuid() == 0:
            self.skipTest("root は chmod 000 でも読める")
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "in.md").write_text("x", encoding="utf-8")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        secret = self.root / "locked.md"
        secret.write_text("x", encoding="utf-8")
        os.chmod(secret, 0o000)
        self.addCleanup(os.chmod, secret, 0o644)
        self.assert_error(self.tree("locked"), 403, "forbidden", reason="tcc")
        self.assert_error(get(self.files, f"/{self.R}/locked.md", TOKEN), 403, "forbidden", reason="tcc")
        self.assert_error(get(self.files, f"/{self.R}/locked/in.md", TOKEN), 403, "forbidden", reason="tcc")
        self.assert_error(self.resolve("locked/in.md"), 403, "forbidden", reason="tcc")

    def test_tcc_reason(self):
        # PermissionError が起点なら reason は tcc（macOS の「フォルダへのアクセス」の守りも chmod も同じ PermissionError）
        with mock.patch("bblist.web.list_dir", side_effect=PermissionError(1, "Operation not permitted")):
            self.assert_error(self.tree(), 403, "forbidden", reason="tcc")
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})
        with mock.patch("bblist.web.list_dir", side_effect=PermissionError(1, "Operation not permitted")):
            self.assert_error(self.tree("sub"), 403, "forbidden", reason="tcc")      # 同じ起点は1つと数える
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})
        other = self.other_root()
        with mock.patch("bblist.web.list_dir", side_effect=PermissionError):
            r = get(self.api, f"/v1/roots/{other}/tree", TOKEN)
        self.assert_error(r, 403, "forbidden", reason="tcc")
        d = self.diagnostics()
        self.assertEqual(d["tcc"], {"blockedRoots": 2})
        self.assertNotIn(self.R.encode(), json.dumps(d).encode())                    # 数だけ。rootId もパスも出さない
        self.assertNotIn(other.encode(), json.dumps(d).encode())
        # 読めるようになった（利用者が許可した）起点は、次に読めた時点で数から外れる
        self.assertEqual(self.tree().status, 200)
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})
        self.assertEqual(get(self.files, f"/{other}/b.md", TOKEN).status, 200)
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 0})

    def test_tcc_reason_on_the_file_port_and_unknown_roots_are_not_recorded(self):
        real_open = os.open

        def deny(path, *a, **kw):
            if str(path).endswith("a.md"):
                raise PermissionError(1, "Operation not permitted")
            return real_open(path, *a, **kw)

        with mock.patch("bblist.web.os.open", deny):
            self.assert_error(get(self.files, f"/{self.R}/a.md", TOKEN), 403, "forbidden", reason="tcc")
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})
        self.assertEqual(self.tree().status, 200)
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 0})
        # 知らない起点・見せない起点は PermissionError に届かない（数えない）
        get(self.api, "/v1/roots/0000000000000000/tree", TOKEN)
        get(self.api, f"/v1/roots/{self.R_HOME}/tree", TOKEN)
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 0})

    def test_resolve_records_a_blocked_root_and_clears_it_when_it_reads_again(self):
        # resolve も、ツリー・ファイル口と同じ決まり：読めなかった起点を覚え、次に読めたら外す
        real_is_file = Path.is_file

        def is_file(path, *a, **kw):
            if path.name == "a.md":
                raise PermissionError(1, "Operation not permitted")
            return real_is_file(path, *a, **kw)

        with mock.patch.object(Path, "is_file", is_file):
            self.assert_error(self.resolve("a.md"), 403, "forbidden", reason="tcc")
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})
        self.assertEqual(self.resolve("a.md").status, 200)                          # 許可された：数から外れる
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 0})
        # 無いファイルの 404 は読めたことにならない（覚えたまま）
        with mock.patch.object(Path, "is_file", is_file):
            self.assert_error(self.resolve("a.md"), 403, "forbidden", reason="tcc")
        self.assert_error(self.resolve("nope.md"), 404, "not_found")
        self.assertEqual(self.diagnostics()["tcc"], {"blockedRoots": 1})

    def test_not_found_and_other_errors_have_no_reason(self):
        for r in (self.tree("nope"), get(self.files, f"/{self.R}/nope.md", TOKEN), get(self.api, "/v1/nope", TOKEN),
                  get(self.api, "/v1/sessions")):
            self.assertNotIn("reason", r.json())

    def test_vanished_file_is_404(self):   # ツリーの後に消えた
        (self.root / "gone.md").write_text("x", encoding="utf-8")
        (self.root / "gone-dir").mkdir()
        names = [e["name"] for e in self.tree().json()["entries"]]
        self.assertIn("gone.md", names)
        self.assertIn("gone-dir", names)
        (self.root / "gone.md").unlink()
        (self.root / "gone-dir").rmdir()
        self.assert_error(get(self.files, f"/{self.R}/gone.md", TOKEN), 404, "not_found")
        self.assert_error(self.tree("gone-dir"), 404, "not_found")

    def test_too_large_413(self):
        self.ctx.max_file_bytes = 10
        (self.root / "ten.txt").write_bytes(b"0123456789")
        r = get(self.files, f"/{self.R}/ten.txt", TOKEN)
        self.assertEqual((r.status, r.body), (200, b"0123456789"))                       # ちょうど上限は返す
        self.assert_error(get(self.files, f"/{self.R}/a.md", TOKEN), 413, "too_large")


# 試験が見張る値：html の CSP は tools/csp-check.mjs（WebKit）で決めたもの。svg・xml は script を止める形
HTML_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals; connect-src 'none'"
XML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
# 外への読み込みを止める厳しい方（Context.preview_external が False のとき）。WebKit で 17/17
HTML_CSP_STRICT = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
                   "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'none'")
# html・svg・xml 以外の中身（.mht・message/rfc822 を文書として開かれても何も読み込まない）
OTHER_CSP = "default-src 'none'; sandbox"


class FileKeyTest(WebCase):
    """ファイル口の鍵は起点ごと（クッキー bb_file）。旧い bb_token のクッキーは受けない。"""

    def test_file_cookie_bound_to_root(self):
        other = self.other_root()
        a, b = f"/{self.R}/a.md", f"/{other}/b.md"
        self.assertEqual(get(self.files, a, Cookie=self.cookie(self.R)).status, 200)
        self.assertEqual(get(self.files, b, Cookie=self.cookie(other)).status, 200)
        self.assert_error(get(self.files, b, Cookie=self.cookie(self.R)), 401, "unauthorized")    # 起点 A の鍵で B は開かない
        self.assert_error(get(self.files, a, Cookie=self.cookie(other)), 401, "unauthorized")
        both = f"{self.cookie(self.R)}; {self.cookie(other)}"                                    # 両方の鍵があっても、URL の起点の分だけ効く
        self.assertEqual([get(self.files, p, Cookie=both).status for p in (a, b)], [200, 200])
        self.assert_error(get(self.files, f"/{self.R}/sub/b.png", Cookie=self.cookie(other)), 401, "unauthorized")

    def test_old_bb_token_cookie_rejected(self):
        for cookie in (f"bb_token={TOKEN}", f"theme=dark; bb_token={TOKEN}; x=1",
                       f"bb_token={TOKEN}; bb_file=x", f"bb_file={TOKEN}"):                       # 端末の鍵を bb_file に入れても通らない
            self.assert_error(get(self.files, f"/{self.R}/a.md", Cookie=cookie), 401, "unauthorized", cookie)
        self.assertEqual(get(self.files, f"/{self.R}/a.md", TOKEN).status, 200)                   # Authorization: Bearer は今までどおり

    def test_cookie_made_with_another_secret_or_prefix_rejected(self):
        import hashlib
        import hmac
        d, r = self.DEV, self.R
        for bad in (file_key(b"\x01" * 32, d, r),                                                 # 別の secret の鍵
                    hmac.new(SECRET, r.encode(), hashlib.sha256).hexdigest(),                    # 接頭辞が違う
                    hmac.new(SECRET, b"file:" + r.encode(), hashlib.sha256).hexdigest(),         # 端末が入っていない（旧い形）
                    file_key(SECRET, d, r).upper(), file_key(SECRET, d, r)[:32], ""):
            self.assert_error(get(self.files, f"/{r}/a.md", Cookie=f"bb_file={d}.{bad}"), 401, "unauthorized", bad)
            self.assert_error(get(self.files, f"/{r}/a.md", Cookie=f"bb_file={bad}"), 401, "unauthorized", bad)

    def test_valid_cookie_for_an_invisible_or_unknown_root_is_not_a_door(self):
        (self.home / "visible.md").write_text("ホームのファイル", encoding="utf-8")
        # 見せない起点は理由つきの 403、知らない起点は 404。どちらも中身は返さない（クッキーが通っても戸にならない）
        self.assert_error(get(self.files, f"/{self.R_HOME}/visible.md", Cookie=self.cookie(self.R_HOME)),
                          403, "forbidden", self.R_HOME, reason="hidden")
        self.assert_error(get(self.files, "/0000000000000000/visible.md", Cookie=self.cookie("0000000000000000")),
                          404, "not_found", "0000000000000000")

    def test_root_id_salted(self):
        # rootId は secret を混ぜた値。パスだけから導いた旧い rootId（塩なし）は、もう起点として通らない
        import hashlib
        real = os.path.realpath(self.root)
        self.assertEqual(self.R, hashlib.sha256(SECRET + b"root:" + real.encode("utf-8")).hexdigest()[:16])
        old = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
        self.assertNotEqual(old, self.R)
        ids = {s["rootId"] for s in get(self.api, "/v1/sessions", TOKEN).json()["sessions"]}
        self.assertIn(self.R, ids)
        self.assertNotIn(old, ids)
        self.assert_error(get(self.api, f"/v1/roots/{old}/tree", TOKEN), 404, "not_found")
        self.assert_error(get(self.files, f"/{old}/a.md", TOKEN), 404, "not_found")

    def test_filekey_endpoint(self):
        r = get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        self.assertEqual(r.json(), {"cookie": "bb_file", "value": f"{self.DEV}.{file_key(SECRET, self.DEV, self.R)}",
                                    "path": f"/{self.R}/"})
        self.assertEqual(list(r.json()), ["cookie", "value", "path"])
        # 渡された鍵で、その起点のファイルが開く
        key = r.json()["value"]
        self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=f"bb_file={key}").status, 200)
        # Bearer が無い・違う → 401（道があるかを見せない）。見せない起点・知らない rootId・rootId なし → 404
        self.assert_error(get(self.api, f"/v1/filekey?rootId={self.R}"), 401, "unauthorized")
        self.assert_error(get(self.api, f"/v1/filekey?rootId={self.R}", "x"), 401, "unauthorized")
        for query in (f"?rootId={self.R_HOME}", "?rootId=0000000000000000", "?rootId=", "", "?root=" + self.R,
                      f"?rootId={self.R}%00", f"?rootId=../{self.R}"):
            self.assert_error(get(self.api, "/v1/filekey" + query, TOKEN), 404, "not_found", query)
        self.assert_error(get(self.api, "/v1/filekey/" + self.R, TOKEN), 404, "not_found")
        self.assertEqual(call("POST", self.api, f"/v1/filekey?rootId={self.R}", TOKEN).status, 405)

    def test_filekey_differs_per_root_and_is_not_served_by_the_file_port(self):
        other = self.other_root()
        keys = {rid: get(self.api, f"/v1/filekey?rootId={rid}", TOKEN).json()["value"] for rid in (self.R, other)}
        self.assertEqual(len(set(keys.values())), 2)
        self.assertEqual(get(self.api, f"/v1/filekey?rootId={other}", TOKEN).json()["path"], f"/{other}/")
        self.assertEqual(get(self.files, "/v1/filekey", TOKEN).status, 404)                   # ファイル口には無い

    def test_html_has_csp_and_nosniff(self):
        (self.root / "page.html").write_text("<p>x</p>", encoding="utf-8")
        (self.root / "old.htm").write_text("<p>x</p>", encoding="utf-8")
        for name in ("page.html", "old.htm"):
            for auth in ({"Cookie": self.cookie()}, {"Authorization": f"Bearer {TOKEN}"}):
                r = get(self.files, f"/{self.R}/{name}", **auth)
                self.assertEqual(r.status, 200, name)
                self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(r.headers.get_all("Content-Security-Policy"), [HTML_CSP], name)
                self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"], name)

    def test_svg_has_strict_csp(self):
        (self.root / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        (self.root / "data.xml").write_text("<a/>", encoding="utf-8")
        for name, ctype in (("pic.svg", "image/svg+xml; charset=utf-8"), ("data.xml", "text/xml; charset=utf-8")):
            r = get(self.files, f"/{self.R}/{name}", Cookie=self.cookie())
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, ctype), name)
            self.assertEqual(r.headers.get_all("Content-Security-Policy"), [XML_CSP], name)
            self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"], name)

    def test_other_files_have_nosniff_and_a_closed_csp(self):
        # html・svg・xml 以外の中身にも「何も読み込まない」CSP。文書として開かれても（.mht・message/rfc822 など）読みに行かない
        for name in ("page.mhtml", "mail.eml", "data.json", "app.js", "style.css", "note.txt"):
            (self.root / name).write_text("x", encoding="utf-8")
        for name in ("a.md", "sub/b.png", "big.bin", "page.mhtml", "mail.eml", "data.json", "app.js", "style.css", "note.txt"):
            r = get(self.files, f"/{self.R}/{name}", Cookie=self.cookie())
            self.assertEqual(r.status, 200, name)
            self.assertEqual(r.headers.get_all("Content-Security-Policy"), [OTHER_CSP], name)
            self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"], name)
        self.assertEqual(get(self.files, f"/{self.R}/page.mhtml", TOKEN).headers["Content-Type"], "message/rfc822")
        # エラーの応答（JSON）には付けない：文書ではない
        self.assertIsNone(get(self.files, f"/{self.R}/nope.md", TOKEN).headers.get("Content-Security-Policy"))
        self.assertIsNone(get(self.api, "/v1/sessions", TOKEN).headers.get("Content-Security-Policy"))

    def test_csp_for_picks_by_content_type(self):
        csp = web.csp_for
        for ctype in ("text/html", "text/html; charset=utf-8", "application/xhtml+xml", "TEXT/HTML"):
            self.assertEqual(csp(ctype), HTML_CSP, ctype)
        for ctype in ("image/svg+xml", "image/svg+xml; charset=utf-8", "text/xml", "application/xml",
                      "application/rss+xml", "application/mathml+xml"):
            self.assertEqual(csp(ctype), XML_CSP, ctype)
        for ctype in ("text/markdown; charset=utf-8", "text/plain", "text/css", "application/javascript",
                      "application/json", "image/png", "application/pdf", "application/octet-stream",
                      "message/rfc822", "multipart/related"):
            self.assertEqual(csp(ctype), OTHER_CSP, ctype)

    def test_csp_for_strict_changes_only_html(self):
        csp = web.csp_for
        for ctype in ("text/html", "text/html; charset=utf-8", "application/xhtml+xml"):
            self.assertEqual(csp(ctype, preview_external=False), HTML_CSP_STRICT, ctype)
            self.assertEqual(csp(ctype, preview_external=True), HTML_CSP, ctype)
        for ctype, want in (("image/svg+xml", XML_CSP), ("text/xml", XML_CSP), ("text/markdown", OTHER_CSP),
                            ("image/png", OTHER_CSP), ("application/octet-stream", OTHER_CSP)):
            self.assertEqual(csp(ctype, preview_external=False), want, ctype)

    def test_context_allows_external_preview_by_default(self):
        self.assertIs(self.ctx.preview_external, True)
        self.assertIs(Context.__dataclass_fields__["preview_external"].default, True)

    def test_every_reply_says_nosniff(self):
        # どの応答にも：本体の道（JSON・画像・エラー・SSE）もファイル口（中身・エラー）も、HEAD への 405 も
        asked = [
            ("health", get(self.api, "/v1/health")), ("sessions", get(self.api, "/v1/sessions", TOKEN)),
            ("image", get(self.api, f"/v1/sessions/{B}/images/u-img/0", TOKEN)), ("tree", self.tree()),
            ("filekey", get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN)),
            ("401", get(self.api, "/v1/sessions")), ("404", get(self.api, "/v1/nope", TOKEN)),
            ("405", call("POST", self.api, "/v1/sessions", TOKEN)), ("HEAD", call("HEAD", self.api, "/v1/sessions", TOKEN)),
            ("421", get(self.api, "/v1/health", Host="rebind.example")),
            ("file", get(self.files, f"/{self.R}/a.md", Cookie=self.cookie())),
            ("file bin", get(self.files, f"/{self.R}/big.bin", TOKEN)),
            ("file 401", get(self.files, f"/{self.R}/a.md")), ("file 404", get(self.files, f"/{self.R}/nope.md", TOKEN)),
            ("file 405", call("POST", self.files, f"/{self.R}/a.md", TOKEN)),
        ]
        for name, r in asked:
            self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"], name)
        with SSE(self.api) as ev:
            self.assertEqual((ev.status, ev.resp.headers.get_all("X-Content-Type-Options")), (200, ["nosniff"]))

    def test_nosniff_on_the_standard_error_replies_too(self):
        # 要求が _serve に届く前に、標準の部品が返すエラー（431・400）にも付く
        too_many = b"".join(b"X-H%d: 1\r\n" % i for i in range(33))
        for name, port, request, status in (
                ("431 api", self.api, b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n" + too_many + b"\r\n", 431),
                ("431 files", self.files, b"GET /x/y HTTP/1.1\r\nHost: 127.0.0.1\r\n" + too_many + b"\r\n", 431),
                ("400 api", self.api, b"GET\r\n\r\n", 400),
                ("505 api", self.api, b"GET /v1/health HTTP/2.0\r\nHost: 127.0.0.1\r\n\r\n", 505)):
            head = exchange_raw(port, request).partition(b"\r\n\r\n")[0].decode("latin-1")
            self.assertTrue(head.startswith(f"HTTP/1.0 {status} "), (name, head[:40]))
            self.assertEqual(head.lower().count("x-content-type-options: nosniff"), 1, name)

    def test_http_09_request_line_is_dropped_not_answered_without_headers(self):
        # 「GET /パス」（HTTP/0.9）は、標準の部品だと見出しなしの本文だけを返す（nosniff も Host の確かめもない）。受けない
        for port, path in ((self.api, "/v1/health"), (self.files, f"/{self.R}/a.md")):
            self.assertEqual(exchange_raw(port, f"GET {path}\r\n\r\n".encode()), b"", path)
        self.assertEqual(get(self.api, "/v1/health").status, 200)                      # ふつうの要求は通る


class DeviceAuthTest(WebCase):
    """端末ごとの鍵。本体もファイル口も、端末の鍵（Bearer）か、端末の id を含むクッキーで通す。
    取り消した端末は、すぐ（再起動なしで）どちらの口でも断る。"""

    def test_device_token_auth(self):
        dev2, tok2 = self.add_device()
        for tok in (TOKEN, tok2):                                                # どの端末の鍵でも通る
            self.assertEqual(get(self.api, "/v1/sessions", tok).status, 200)
            self.assertEqual(get(self.files, f"/{self.R}/a.md", tok).status, 200)
        for tok in ("x", TOKEN + "x", TOKEN[:-1], dev2, self.DEV, ""):           # 端末の id や、似た文字列は通らない
            self.assert_error(get(self.api, "/v1/sessions", tok), 401, "unauthorized", tok)
            self.assert_error(get(self.files, f"/{self.R}/a.md", tok), 401, "unauthorized", tok)

    def test_no_devices_means_nobody_gets_in(self):
        with tempfile.TemporaryDirectory() as d:
            self.ctx.devices = DeviceStore(Path(d))                              # 組んだ端末が1台も無い
            for port, path in ((self.api, "/v1/sessions"), (self.files, f"/{self.R}/a.md")):
                self.assert_error(get(port, path, TOKEN), 401, "unauthorized")
            self.assert_error(get(self.files, f"/{self.R}/a.md", Cookie=self.cookie()), 401, "unauthorized")
            self.assertEqual(get(self.api, "/v1/health").status, 200)

    def test_revoked_device_rejected(self):
        dev2, tok2 = self.add_device()
        cookie = self.cookie()
        self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=cookie).status, 200)
        self.assertTrue(DeviceStore(self.own[0]).revoke(self.DEV))              # 別のプロセス（bbctl revoke）が取り消す
        self.assert_error(get(self.api, "/v1/sessions", TOKEN), 401, "unauthorized")
        self.assert_error(get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN), 401, "unauthorized")
        self.assert_error(get(self.files, f"/{self.R}/a.md", TOKEN), 401, "unauthorized")
        self.assert_error(get(self.files, f"/{self.R}/a.md", Cookie=cookie), 401, "unauthorized")   # 渡してあったクッキーも
        with SSE(self.api, token=TOKEN) as ev:
            self.assertEqual(ev.status, 401)
        self.assertEqual(get(self.api, "/v1/sessions", tok2).status, 200)       # ほかの端末は今までどおり
        self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=self.cookie(device=dev2)).status, 200)

    def test_filekey_cookie_embeds_the_device_id_and_differs_per_device(self):
        dev2, tok2 = self.add_device()
        a = get(self.api, f"/v1/filekey?rootId={self.R}", TOKEN).json()
        b = get(self.api, f"/v1/filekey?rootId={self.R}", tok2).json()
        self.assertEqual(a["value"], f"{self.DEV}.{file_key(SECRET, self.DEV, self.R)}")
        self.assertEqual(b["value"], f"{dev2}.{file_key(SECRET, dev2, self.R)}")
        self.assertNotEqual(a["value"], b["value"])                               # 同じ起点でも端末ごとに別の鍵
        for r in (a, b):
            self.assertRegex(r["value"], r"\A[0-9a-f]{16}\.[0-9a-f]{64}\Z")
            self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=f"bb_file={r['value']}").status, 200)

    def test_cookie_of_another_or_unknown_device_is_rejected(self):
        dev2, _ = self.add_device()
        r = self.R
        mine = file_key(SECRET, self.DEV, r)
        for cookie in (f"bb_file={dev2}.{mine}",                                  # 他の端末の id に自分の鍵
                       f"bb_file={self.DEV}.{file_key(SECRET, dev2, r)}",         # 自分の id に他の端末の鍵
                       f"bb_file={'0' * 16}.{file_key(SECRET, '0' * 16, r)}",     # 組んでいない端末（鍵は正しく計算されている）
                       f"bb_file={self.DEV.upper()}.{mine}", f"bb_file=.{mine}", f"bb_file={self.DEV[:8]}.{mine}",
                       f"bb_file={self.DEV}..{mine}", f"bb_file={self.DEV}.{mine}.{mine}", f"bb_file={self.DEV}:{mine}"):
            self.assert_error(get(self.files, f"/{r}/a.md", Cookie=cookie), 401, "unauthorized", cookie)

    def test_cookie_of_a_revoked_device_is_rejected_even_with_a_valid_mac(self):
        dev2, tok2 = self.add_device()
        cookie = self.cookie(device=dev2)
        self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=cookie).status, 200)
        self.assertTrue(self.devices.revoke(dev2))
        self.assert_error(get(self.files, f"/{self.R}/a.md", Cookie=cookie), 401, "unauthorized")
        self.assertNotIn(dev2, [d["id"] for d in self.devices.list()])

    def test_file_key_is_checked_for_the_urls_root(self):
        other = self.other_root()
        key_for_r = self.cookie(self.R)
        self.assert_error(get(self.files, f"/{other}/b.md", Cookie=key_for_r), 401, "unauthorized")   # URL の起点で確かめる
        self.assertEqual(get(self.files, f"/{other}/b.md", Cookie=self.cookie(other)).status, 200)

    def test_last_seen_is_recorded_for_bearer_and_cookie(self):
        self.assertIsNone(self.devices.list()[0]["lastSeenAt"])
        get(self.api, "/v1/sessions", TOKEN)
        first = self.devices.list()[0]["lastSeenAt"]
        self.assertIsNotNone(first)
        dev2, _ = self.add_device()
        get(self.files, f"/{self.R}/a.md", Cookie=self.cookie(device=dev2))
        self.assertIsNotNone({d["id"]: d for d in self.devices.list()}[dev2]["lastSeenAt"])


class PairTest(WebCase):
    """POST /v1/pair：5分で切れる一度きりの符号を、端末の鍵に替える。本体の口の、認証なしの、唯一の POST。"""

    CTX = {"header_timeout": 1.0}

    def pair(self, code=None, name="Tomo's iPad", **headers) -> Reply:
        body = json.dumps({"code": code if code is not None else self.code, "deviceName": name}).encode("utf-8")
        return post(self.api, "/v1/pair", body, **{"Content-Type": "application/json", **headers})

    def setUp(self):
        super().setUp()
        self.code = None

    def new_code(self) -> str:
        with mock.patch("secrets.token_urlsafe", return_value=CODE):
            self.code = self.pairing.create()
        return self.code

    def assert_failed(self, r: Reply, msg=None) -> None:
        self.assert_error(r, 403, "pair_failed", msg)

    def relax_pair_limits(self) -> None:
        """回数の上限を試さない試験のために、上限を大きくする。"""
        self.ctx.pair_limits = PairLimiter(per_ip=10_000, total=10_000, clock=self.clock)

    # ---- 組める

    def test_pair_success_then_code_unusable(self):
        self.new_code()
        r = self.pair()
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, JSON))
        j = r.json()
        self.assertEqual(list(j), ["deviceId", "token", "apiVersion"])
        self.assertEqual(j["apiVersion"], web.API_VERSION)
        self.assertEqual(web.API_VERSION, 3)
        self.assertRegex(j["deviceId"], r"\A[0-9a-f]{16}\Z")
        self.assertEqual(self.devices.verify(j["token"]), j["deviceId"])           # 渡した鍵が、その端末の鍵
        self.assertEqual([d["name"] for d in self.devices.list()], ["Test iPad", "Tomo's iPad"])
        self.assertFalse((self.own[0] / "pairing.json").exists())
        self.assert_failed(self.pair())                                            # 使用済み：何度送っても断る
        self.assert_failed(self.pair())
        self.assertEqual(len(self.devices.list()), 2)

    def test_paired_token_opens_both_ports_and_gets_a_file_key(self):
        self.new_code()
        j = self.pair().json()
        self.assertEqual(get(self.api, "/v1/sessions", j["token"]).status, 200)
        self.assertEqual(get(self.files, f"/{self.R}/a.md", j["token"]).status, 200)
        key = get(self.api, f"/v1/filekey?rootId={self.R}", j["token"]).json()["value"]
        self.assertTrue(key.startswith(j["deviceId"] + "."))
        self.assertEqual(get(self.files, f"/{self.R}/a.md", Cookie=f"bb_file={key}").status, 200)

    def test_reply_is_not_cached_and_has_nosniff(self):
        self.new_code()
        r = self.pair()
        self.assertEqual(r.headers.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"])
        self.assertEqual(cors(r), [])

    def test_pair_needs_no_authorization_and_ignores_a_bad_one(self):
        self.new_code()
        self.assertEqual(self.pair(Authorization="Bearer wrong").status, 200)

    def test_pair_expired(self):
        self.new_code()
        self.clock.now += 301
        self.assert_failed(self.pair())
        self.assertEqual(len(self.devices.list()), 1)

    def test_pair_expiry_uses_monotonic_clock(self):
        self.new_code()
        self.clock.now += 301
        with mock.patch("time.time", return_value=time.time() - 7200):             # 壁の時計を戻しても延びない
            self.assert_failed(self.pair())

    def test_pair_invalid_after_reboot(self):
        self.new_code()
        self.boot[0] += 120
        self.assert_failed(self.pair())

    def test_all_failures_look_the_same(self):
        # 違う・期限切れ・使用済みで、状態も本文も分けない（当てずっぽうの手がかりを渡さない）
        self.new_code()
        wrong = self.pair(code="W" * 22)
        self.assertTrue(self.pair().status == 200)
        used = self.pair()
        self.new_code()
        self.clock.now += 301
        expired = self.pair()
        nothing = self.pair(code="")
        for r in (wrong, used, expired, nothing):
            self.assert_failed(r)
            self.assertEqual(r.body, wrong.body)

    def test_a_wrong_code_does_not_use_up_the_right_one(self):
        self.new_code()
        for _ in range(3):
            self.assert_failed(self.pair(code="W" * 22))
        self.assertEqual(self.pair().status, 200)

    def test_pair_rate_limited(self):
        # 送り元ごとに1分に5回。6回目は、正しい符号でも 429
        self.new_code()
        for i in range(5):
            self.assert_failed(self.pair(code="W" * 22), i)
        r = self.pair()
        self.assert_error(r, 429, "rate_limited")
        self.assertEqual(r.headers.get("Retry-After"), "60")
        self.assert_error(self.pair(), 429, "rate_limited")
        self.assertEqual(len(self.devices.list()), 1)
        self.clock.now += 61                                                        # 1分たてば、また受ける（符号はまだ有効）
        self.assertEqual(self.pair().status, 200)

    def test_pair_rate_limit_counts_every_kind_of_request(self):
        for _ in range(5):
            post(self.api, "/v1/pair", b"not json")
        self.assert_error(post(self.api, "/v1/pair", b"not json"), 429, "rate_limited")

    def test_pair_rate_limit_is_per_source_address(self):
        # 1つの送り元が使い切っても、別の送り元は組める（同じ tailnet の誰かに締め出されない）
        self.new_code()
        sources = {"ip": "100.64.0.1"}
        with mock.patch.object(web._ApiHandler, "_peer", lambda handler: sources["ip"]):
            for _ in range(5):
                self.assert_failed(self.pair(code="W" * 22))
            self.assert_error(self.pair(code="W" * 22), 429, "rate_limited")
            sources["ip"] = "100.64.0.2"
            self.assertEqual(self.pair().status, 200)

    def test_pair_rate_limit_has_a_global_cap_of_30_per_minute(self):
        # 送り元を変えながら叩いても、全体で1分に30回まで（31回目は新しい送り元でも 429）
        self.new_code()
        sources = {"ip": ""}
        with mock.patch.object(web._ApiHandler, "_peer", lambda handler: sources["ip"]):
            for n in range(6):
                sources["ip"] = f"100.64.1.{n}"
                for _ in range(5):
                    self.assert_failed(self.pair(code="W" * 22))
            sources["ip"] = "100.64.1.200"
            self.assert_error(self.pair(), 429, "rate_limited")
            self.assertEqual(len(self.devices.list()), 1)
            self.clock.now += 61
            self.assertEqual(self.pair().status, 200)

    def test_the_peer_address_is_the_connections_not_a_header(self):
        # X-Forwarded-For などを送っても、送り元の数え方を替えられない
        self.new_code()
        for i in range(5):
            self.assert_failed(self.pair(code="W" * 22, **{"X-Forwarded-For": f"100.64.9.{i}"}))
        self.assert_error(self.pair(**{"X-Forwarded-For": "100.64.9.99"}), 429, "rate_limited")

    def test_the_limiter_is_asked_about_the_sockets_peer(self):
        self.new_code()
        asked = []
        real = self.ctx.pair_limits.allow
        self.ctx.pair_limits.allow = lambda ip: (asked.append(ip), real(ip))[1]
        self.assertEqual(self.pair(**{"X-Forwarded-For": "100.64.9.1", "Forwarded": "for=100.64.9.2"}).status, 200)
        self.assertEqual(asked, [HOST])

    def test_pair_body_limit(self):
        self.new_code()
        big = json.dumps({"code": self.code, "deviceName": "x" * 5000}).encode()
        self.assertGreater(len(big), 4096)
        self.assert_error(post(self.api, "/v1/pair", big), 413, "too_large")
        self.assert_error(post(self.api, "/v1/pair", b"x" * 100_000), 413, "too_large")
        self.assertEqual(len(self.devices.list()), 1)
        pad = 4096 - len(json.dumps({"code": self.code, "deviceName": ""}).encode())
        exact = json.dumps({"code": self.code, "deviceName": "n" * pad}).encode()
        self.assertEqual(len(exact), 4096)
        r = post(self.api, "/v1/pair", exact)                                       # ちょうど 4 KiB は受ける（符号は消えていない）
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.devices.list()[-1]["name"]), 64)                  # 名前は 64 字まで

    def test_body_limit_is_not_fooled_by_a_lying_length(self):
        self.new_code()
        self.relax_pair_limits()
        head = f"POST /v1/pair HTTP/1.1\r\nHost: {HOST}\r\n"
        # Content-Length が無い・数でない・負・chunked → 413 でなく 400。本文を読まない
        for extra in ("", "Content-Length: abc\r\n", "Content-Length: -1\r\n", "Content-Length: 1e3\r\n",
                      "Transfer-Encoding: chunked\r\n"):
            status, body = exchange(self.api, (head + extra + "\r\n").encode() + b"{}")
            self.assertEqual((status, json.loads(body)), (400, {"error": "bad_request"}), extra)
        # 言った長さより短くて、送る側を閉じた → 400
        with socket.create_connection((HOST, self.api), timeout=5) as s:
            s.sendall((head + "Content-Length: 100\r\n\r\n").encode() + b'{"code"')
            s.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := s.recv(4096):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 400 "), data[:40])
        self.assertEqual(json.loads(data.partition(b"\r\n\r\n")[2]), {"error": "bad_request"})
        self.assertEqual(self.pair().status, 200)                                   # 符号は消えていない

    def test_bad_bodies_are_400_and_do_not_use_up_the_code(self):
        self.new_code()
        self.relax_pair_limits()                                                    # ここは回数の上限を試さない
        for body in (b"", b"not json", b"[]", b'"x"', b"null", b"{}", b'{"code": "a"}', b'{"deviceName": "a"}',
                     b'{"code": 5, "deviceName": "a"}', b'{"code": "a", "deviceName": 5}',
                     b'{"code": ["a"], "deviceName": "a"}', b"\xff\xfe", b'{"code": "\xff", "deviceName": "a"}'):
            self.assert_error(post(self.api, "/v1/pair", body), 400, "bad_request", body)
        self.assertEqual(self.pair().status, 200)                                   # 壊れた本文のあとも、符号は生きている

    def test_deeply_nested_json_is_400_not_a_crash(self):
        self.new_code()
        self.relax_pair_limits()
        with self.assertNoLogs_compat("bblist.web"):
            for body in (b"[" * 4000, b"[" * 4096, b'{"code": "' + CODE.encode() + b'", "deviceName": ' + b"[" * 3000):
                self.assert_error(post(self.api, "/v1/pair", body), 400, "bad_request", body[:20])
        self.assertEqual(self.pair().status, 200)                                   # サーバーは生きていて、符号も使える

    def test_device_name_is_cleaned_before_it_is_stored_and_never_empty(self):
        self.new_code()
        self.pair(name="iPad\x1b[2J\nx")
        self.assertEqual(self.devices.list()[-1]["name"], "iPad[2Jx")
        self.new_code()
        self.pair(name="  \x07 ")
        self.assertEqual(self.devices.list()[-1]["name"], "Unnamed device")

    def test_only_post_is_taken_and_only_on_the_api_port(self):
        self.new_code()
        for method in ("GET", "PUT", "DELETE", "PATCH", "HEAD"):
            r = call(method, self.api, "/v1/pair")
            self.assertEqual(r.status, 405, method)
            self.assertEqual(r.headers["Allow"], "POST", method)
        r = post(self.files, "/v1/pair", b"{}")                                     # ファイル口は GET だけ
        self.assertEqual((r.status, r.headers["Allow"]), (405, "GET"))
        self.assertEqual(post(self.api, "/v1/pair/", b"{}").status, 405)            # 道が合わない POST は今までどおり 405
        self.assertEqual(post(self.api, "/v1/sessions", b"{}").status, 405)
        self.assertEqual(post(self.api, "/v1/health", b"{}").status, 405)
        self.assertEqual(self.pair().status, 200)

    def test_host_is_checked(self):
        self.new_code()
        self.assert_error(self.pair(Host="rebind.example"), 421, "bad_host")
        self.assertEqual(self.pair().status, 200)

    def test_a_slow_body_is_cut(self):
        self.new_code()
        s = open_idle(self.api)
        self.addCleanup(s.close)
        s.sendall(f"POST /v1/pair HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 60\r\n\r\n".encode() + b'{"code"')
        self.assertTrue(closed_within(s, 4.0), "本文を送らない接続が切られない")
        self.assertEqual(self.pair().status, 200)

    def test_pair_log_has_no_code_token_or_name(self):
        self.new_code()
        with capture_log() as rec:
            self.assert_failed(self.pair(code="W" * 22, name="Secret Name"))
            ok = self.pair(name="Secret Name")
            self.assertEqual(ok.status, 200)
            self.assert_error(post(self.api, "/v1/pair", b"bad"), 400, "bad_request")
            self.assertTrue(wait_until(lambda: len(rec.lines) >= 3))
        text = "\n".join(rec.lines)
        for secret in (CODE, "W" * 22, ok.json()["token"], ok.json()["deviceId"], "Secret Name", SECRET.hex()):
            self.assertNotIn(secret, text)
        for want in ("api POST /v1/pair 403", "api POST /v1/pair 200", "api POST /v1/pair 400"):
            self.assertIn(want, text)
        for ln in rec.lines:
            self.assertRegex(ln, r"\A(api|files) (GET|POST) \S+ \d{3} \d+ms\Z")

    def test_unexpected_error_is_500_with_type_name_only(self):
        self.new_code()
        with mock.patch.object(self.devices, "add", side_effect=OSError(f"disk {CODE}")):
            with self.assertLogs("bblist.web", level="WARNING") as cm:
                r = self.pair()
        self.assert_error(r, 500, "unreadable")
        self.assertNotIn(CODE, "\n".join(cm.output))
        self.assertIn("OSError", "\n".join(cm.output))


class StrictPreviewTest(WebCase):
    """Context.preview_external が False：html は外への読み込みも止める CSP（設定 previewExternal からの配線は MainTest）。"""

    CTX = {"preview_external": False}

    def test_html_gets_the_strict_csp(self):
        (self.root / "page.html").write_text("<p>x</p>", encoding="utf-8")
        for auth in ({"Cookie": self.cookie()}, {"Authorization": f"Bearer {TOKEN}"}):
            r = get(self.files, f"/{self.R}/page.html", **auth)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get_all("Content-Security-Policy"), [HTML_CSP_STRICT])
            self.assertEqual(r.headers.get_all("X-Content-Type-Options"), ["nosniff"])

    def test_diagnostics_reports_it(self):
        self.assertIs(self.diagnostics()["previewExternal"], False)

    def test_other_types_are_unchanged(self):
        (self.root / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        for name, want in (("pic.svg", XML_CSP), ("a.md", OTHER_CSP), ("sub/b.png", OTHER_CSP)):
            r = get(self.files, f"/{self.R}/{name}", Cookie=self.cookie())
            self.assertEqual(r.headers.get_all("Content-Security-Policy"), [want], name)


class GitIgnoreWebTest(WebCase):
    """git で無視されたファイルは、ツリーにも出さず、ファイル口でも resolve でも返さない（既定）。

    起点を本物の git の作業コピーにする（WebCase が作る偽の .git は作り直す）。利用者の git の設定は外す。
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, isolated_git_env(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        shutil.rmtree(self.root / ".git")
        init_repo(self.root, "out/\n*.log\nsecret/\n")
        for rel, text in {"out/a.html": "<p>ignored</p>", "out/sub/b.html": "<p>deeper</p>", "keep/c.html": "<p>shown</p>",
                          "app.log": "log", "secret/s.txt": "s"}.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.append_log(*[row for i, rel in enumerate(("out/a.html", "keep/c.html", "app.log"))
                          for row in (tool(f"w{i}", f"tw{i}", "Write", file_path=str(self.root / rel)),
                                      result(f"wr{i}", f"tw{i}"))])

    def names(self, rel: str = "") -> list:
        return [e["name"] for e in self.tree(rel).json()["entries"]]

    def test_gitignored_hidden_by_default(self):
        self.assertEqual(self.names(), ["keep", "sub", "a.md", "big.bin"])         # out・secret・app.log が出ない
        self.assertEqual(self.names("keep"), ["c.html"])
        self.assert_error(self.tree("out"), 404, "not_found")                       # フォルダそのものも開けない
        self.assert_error(self.tree("out/sub"), 404, "not_found")
        self.assert_error(self.tree("secret"), 404, "not_found")
        for rel in ("out/a.html", "out/sub/b.html", "secret/s.txt", "app.log"):     # フォルダの下のファイルも
            self.assert_error(get(self.files, f"/{self.R}/{rel}", TOKEN), 404, "not_found", rel)
        r = get(self.files, f"/{self.R}/keep/c.html", TOKEN)
        self.assertEqual((r.status, r.body), (200, b"<p>shown</p>"))
        for path in ("out/a.html", "app.log", str(self.root / "out" / "a.html")):
            self.assert_error(self.resolve(path), 404, "not_found", path)
        self.assertEqual(self.resolve("keep/c.html").json()["relPath"], "keep/c.html")
        j = get(self.api, f"/v1/sessions/{B}/written", TOKEN).json()
        rows = {f["path"]: f for f in j["files"]}
        for rel in ("out/a.html", "app.log"):                                       # 見えないものと同じ null
            self.assertEqual(rows[str(self.root / rel)], {"path": str(self.root / rel), "rootId": None, "relPath": None,
                                                          "lastTs": TS, "exists": None}, rel)
        self.assertEqual(rows[str(self.root / "keep/c.html")]["exists"], True)
        self.assertEqual(rows[str(self.root / "keep/c.html")]["relPath"], "keep/c.html")
        self.assertEqual(rows[str(self.root / "a.md")]["exists"], True)             # 無視されないものは今までどおり

    def test_ignored_cannot_be_reached_through_a_link_or_another_spelling(self):
        os.symlink(self.root / "out", self.root / "latest")                         # 無視されたフォルダへのリンク
        os.symlink(self.root / "out" / "a.html", self.root / "page.html")           # 無視されたファイルへのリンク
        self.assertEqual(self.names(), ["keep", "sub", "a.md", "big.bin"])         # リンクも出さない
        for rel in ("latest/a.html", "latest/sub/b.html", "page.html"):
            self.assert_error(get(self.files, f"/{self.R}/{rel}", TOKEN), 404, "not_found", rel)
        self.assert_error(self.tree("latest"), 404, "not_found")
        self.assert_error(self.resolve("latest/a.html"), 404, "not_found")
        self.assert_error(self.resolve("page.html"), 404, "not_found")
        if os.path.exists(self.root / "OUT"):                                       # 大文字小文字を区別しないディスク（APFS）
            self.assert_error(get(self.files, f"/{self.R}/OUT/a.html", TOKEN), 404, "not_found")
            self.assert_error(get(self.files, f"/{self.R}/Out/Sub/B.html", TOKEN), 404, "not_found")
            self.assert_error(get(self.files, f"/{self.R}/APP.LOG", TOKEN), 404, "not_found")
            self.ctx.gitignore = GitIgnore(show_ignored=True)                       # 対照：隠すのをやめれば、同じ綴りで開く
            for rel in ("OUT/a.html", "Out/Sub/B.html", "APP.LOG"):
                self.assertEqual(get(self.files, f"/{self.R}/{rel}", TOKEN).status, 200, rel)

    def test_names_that_look_like_pathspec_magic_do_not_stop_the_listing(self):
        # 一覧に `:!x` のような名前があっても、ほかの無視されたファイルを隠す（git が 128 で止まって全部見せない、にしない）
        for name in (":!x", ":^x", ":(glob)y"):
            (self.root / name).write_text("x", encoding="utf-8")
        names = self.names()
        for name in (":!x", ":^x", ":(glob)y", "a.md"):
            self.assertIn(name, names)
        for name in ("out", "secret", "app.log"):                                  # 無視されたものは、並んでいても隠れる
            self.assertNotIn(name, names)
        self.assertIs(self.ctx.gitignore.available, True)
        self.assertEqual(get(self.files, f"/{self.R}/{quote(':!x')}", TOKEN).status, 200)

    def make_clone_under_ignored_folder(self) -> None:
        """vendor/ を無視していて、中に git clone したリポジトリがある：中のリポジトリには vendor/ の規則がない。"""
        (self.root / ".gitignore").write_text("out/\n*.log\nsecret/\nvendor/\n", encoding="utf-8")
        clone = init_repo(self.root / "vendor" / "clone")
        (clone / "sub").mkdir()
        (clone / "plan.txt").write_text("clone plan", encoding="utf-8")
        (clone / "sub" / "x.txt").write_text("deeper", encoding="utf-8")
        probe = subprocess.run(["git", "-C", str(clone), "check-ignore", "plan.txt"], capture_output=True)
        self.assertEqual(probe.returncode, 1)                                       # 前提：中のリポジトリだけを見ると無視ではない

    def test_clone_under_an_ignored_folder_file_port(self):
        self.make_clone_under_ignored_folder()
        for rel in ("vendor/clone/plan.txt", "vendor/clone/sub/x.txt"):
            self.assert_error(get(self.files, f"/{self.R}/{rel}", TOKEN), 404, "not_found", rel)
        self.ctx.gitignore = GitIgnore(show_ignored=True)                           # 対照：旗を立てれば開く
        self.assertEqual(get(self.files, f"/{self.R}/vendor/clone/plan.txt", TOKEN).body, b"clone plan")

    def test_clone_under_an_ignored_folder_resolve_and_written(self):
        self.make_clone_under_ignored_folder()
        path = str(self.root / "vendor/clone/plan.txt")
        self.append_log(tool("v1", "tv1", "Write", file_path=path), result("vr1", "tv1"))
        for rel in ("vendor/clone/plan.txt", "vendor/clone/sub/x.txt", path):
            self.assert_error(self.resolve(rel), 404, "not_found", rel)
        rows = {f["path"]: f for f in get(self.api, f"/v1/sessions/{B}/written", TOKEN).json()["files"]}
        row = rows[path]                                                            # 見えない起点の外と同じ null
        self.assertEqual((row["rootId"], row["relPath"], row["exists"]), (None, None, None))
        self.ctx.gitignore = GitIgnore(show_ignored=True)
        self.assertEqual(self.resolve("vendor/clone/plan.txt").json()["relPath"], "vendor/clone/plan.txt")

    def test_clone_under_an_ignored_folder_tree(self):
        self.make_clone_under_ignored_folder()
        self.assertNotIn("vendor", self.names())
        for rel in ("vendor", "vendor/clone", "vendor/clone/sub"):                  # 直接開いても
            self.assert_error(self.tree(rel), 404, "not_found", rel)
        self.ctx.gitignore = GitIgnore(show_ignored=True)
        self.assertEqual(self.names("vendor/clone/sub"), ["x.txt"])

    def test_names_with_glob_characters_next_to_a_tracked_match_are_hidden(self):
        # 追跡済みの tracked.log があっても、`[t]racked.log` `t?acked.log` `*.log` という名前の無視されたファイルは隠れる
        for name in ("tracked.log", "[t]racked.log", "t?acked.log", "*.log"):
            (self.root / name).write_text("glob " + name, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "tracked.log"], check=True, capture_output=True)
        names = self.names()
        self.assertIn("tracked.log", names)                                         # 追跡済みは見える
        for name in ("[t]racked.log", "t?acked.log", "*.log", "app.log"):
            self.assertNotIn(name, names)
            self.assert_error(get(self.files, f"/{self.R}/{quote(name)}", TOKEN), 404, "not_found", name)
            self.assert_error(self.resolve(name), 404, "not_found", name)
        self.assertEqual(get(self.files, f"/{self.R}/tracked.log", TOKEN).status, 200)

    def test_tracked_file_that_matches_a_pattern_is_shown(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "out/a.html"], check=True, capture_output=True)
        r = get(self.files, f"/{self.R}/out/a.html", TOKEN)
        self.assertEqual((r.status, r.body), (200, b"<p>ignored</p>"))              # git の履歴にあるものは無視ではない
        self.assertEqual(self.names("out"), ["a.html"])                             # 追跡のないほかは出ない

    def test_gitignored_shown_when_flag_on(self):
        self.ctx.gitignore = GitIgnore(show_ignored=True)
        self.assertEqual(self.names(), ["keep", "out", "secret", "sub", "a.md", "app.log", "big.bin"])
        self.assertEqual(self.names("out"), ["sub", "a.html"])
        for rel in ("out/a.html", "out/sub/b.html", "secret/s.txt", "app.log"):
            self.assertEqual(get(self.files, f"/{self.R}/{rel}", TOKEN).status, 200, rel)
        self.assertEqual(self.resolve("out/a.html").json()["relPath"], "out/a.html")
        rows = {f["path"]: f for f in get(self.api, f"/v1/sessions/{B}/written", TOKEN).json()["files"]}
        self.assertEqual(rows[str(self.root / "out/a.html")]["exists"], True)
        self.assertEqual(rows[str(self.root / "out/a.html")]["rootId"], self.R)

    def test_non_git_folder_unaffected(self):
        shutil.rmtree(self.root / ".git")                                           # .gitignore は残るが、git ではない
        self.assertEqual(self.names(), ["keep", "out", "secret", "sub", "a.md", "app.log", "big.bin"])
        self.assertEqual(get(self.files, f"/{self.R}/out/a.html", TOKEN).status, 200)
        self.assertEqual(self.resolve("out/a.html").status, 200)
        self.assertIs(self.ctx.gitignore.available, True)

    def test_gitignore_unavailable_shows_and_reports(self):
        self.ctx.gitignore = GitIgnore(show_ignored=False, git=lambda directory, names, root=None: None)   # git が動かない
        with self.assertLogs("bblist.gitignore", "WARNING"):
            self.assertEqual(self.names(), ["keep", "out", "secret", "sub", "a.md", "app.log", "big.bin"])   # 隠さない
        self.assertEqual(get(self.files, f"/{self.R}/out/a.html", TOKEN).status, 200)
        self.assertEqual(self.resolve("out/a.html").status, 200)
        self.assertEqual(get(self.api, f"/v1/sessions/{B}/written", TOKEN).status, 200)
        self.assertIs(self.ctx.gitignore.available, False)
        self.assertEqual(self.diagnostics()["gitignore"], {"available": False, "showIgnored": False})

    def test_one_git_call_per_listing_and_it_is_remembered(self):
        for i in range(2000):
            (self.root / "many").mkdir(exist_ok=True)
            (self.root / "many" / f"f{i:04d}.{'log' if i % 2 else 'md'}").write_text("x", encoding="utf-8")
        calls = []

        def counting(directory, names, root=None):
            calls.append((str(directory), len(names)))
            return run_check_ignore(directory, names, root)

        self.ctx.gitignore = GitIgnore(show_ignored=False, git=counting)
        self.assertEqual(len(self.names()), 5)                                      # keep many sub a.md big.bin
        self.assertEqual(len(calls), 1)                                             # 起点の一覧：1回
        listed = self.tree("many").json()
        self.assertEqual(len(listed["entries"]), 1000)                              # *.log を隠した残り
        self.assertEqual(len(calls), 2)                                             # 親の一覧で覚えた many そのものの答えは使い、中身で1回
        self.assertEqual((Path(calls[1][0]).name, calls[1][1]), ("many", 2000))     # 2000 件を1回にまとめて
        self.names()
        self.tree("many")
        self.assertEqual(len(calls), 2)                                             # 30 秒以内は呼ばない


class WorktreeTest(WebCase):
    """見せてよいプロジェクト（proj）の中の . で始まるフォルダにある worktree のセッション。

    git は差し替える（worktree の作業フォルダにだけ、起点＝worktree・common-dir＝proj/.git と答える）。
    """

    WTS = {"session_7": ".worktrees/feat", "session_8": ".claude/worktrees/feat"}

    def setUp(self):
        super().setUp()
        self.wt, self.RW = {}, {}
        for i, (bridge, rel) in enumerate(self.WTS.items(), 7):
            wt = self.root / rel
            (wt / "docs").mkdir(parents=True)
            (wt / "docs" / "x.md").write_text(FILE_TEXT, encoding="utf-8")
            (wt / ".env").write_text("K=1", encoding="utf-8")            # worktree の中の . で始まる名前は出さないまま
            admin = self.root / ".git" / "worktrees" / f"{wt.name}{i}"      # 本物の worktree と同じ作り（gitdir が .git へ戻る）
            admin.mkdir(parents=True)
            (admin / "gitdir").write_text(f"{wt}/.git\n", encoding="utf-8")
            (wt / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            sid = f"sid-wt{i}"
            with open(self.logs / f"{sid}.jsonl", "ab") as f:
                f.write(line(tool("w1", "tw", "Write", file_path=str(wt / "docs" / "x.md"))) + line(result("w2", "tw")))
            self.add(i, cwd=str(wt), bridgeSessionId=bridge, sessionId=sid)
            self.wt[bridge], self.RW[bridge] = wt, folder_id(wt)
        self.hub.step()                                         # 一覧を読み直すのは見回りだけ

    def make_catalog(self, git=None, alive="fake"):
        root = self.lab / "30_cat" / "proj"
        answers = {str(root / rel): (str(root / rel), str(root / ".git")) for rel in self.WTS.values()}
        return super().make_catalog(git=answers.get, alive=alive)

    def test_sessions_rows_show_the_worktree_root_as_visible(self):
        rows = {s["bridgeSessionId"]: s for s in get(self.api, "/v1/sessions", TOKEN).json()["sessions"]}
        for bridge in self.WTS:
            s = rows[bridge]
            self.assertEqual((s["projectId"], s["rootId"], s["worktree"], s["filesVisible"]),
                             (self.R, self.RW[bridge], "feat", True), bridge)
        self.assertEqual((rows[B]["filesVisible"], rows[B_HOME]["filesVisible"]), (True, False))

    def test_tree_file_written_and_resolve_work_in_the_worktree(self):
        for bridge in self.WTS:
            RW, wt = self.RW[bridge], self.wt[bridge]
            j = get(self.api, f"/v1/roots/{RW}/tree", TOKEN).json()
            self.assertEqual((j["rootId"], [e["name"] for e in j["entries"]]), (RW, ["docs"]), bridge)   # .env は出ない
            j = get(self.api, f"/v1/roots/{RW}/tree?path=docs", TOKEN).json()
            self.assertEqual([e["name"] for e in j["entries"]], ["x.md"], bridge)
            r = get(self.files, f"/{RW}/docs/x.md", TOKEN)
            self.assertEqual((r.status, r.body.decode("utf-8")), (200, FILE_TEXT), bridge)
            self.assert_error(get(self.files, f"/{RW}/.env", TOKEN), 403, "forbidden", bridge, reason="hidden")
            j = get(self.api, f"/v1/sessions/{bridge}/written", TOKEN).json()
            self.assertEqual(j["files"], [{"path": str(wt / "docs" / "x.md"), "rootId": RW, "relPath": "docs/x.md",
                                           "lastTs": TS, "exists": True}], bridge)
            want = {"rootId": RW, "relPath": "docs/x.md", "preview": "md", "exists": True}
            for path in ("docs/x.md", str(wt / "docs" / "x.md")):
                r = self.resolve(path, bridge=bridge)
                self.assertEqual((r.status, r.json()), (200, want), (bridge, path))
            self.assert_error(self.resolve(".env", bridge=bridge), 404, "not_found", bridge)
            # 親のセッションの文章の中の worktree のパスも、見せてよい worktree の起点に引ける
            r = self.resolve(str(wt / "docs" / "x.md"))
            self.assertEqual((r.status, r.json()), (200, want), bridge)
        # 親のツリーには . で始まるフォルダ（.worktrees・.claude）を出さないまま
        self.assertEqual([e["name"] for e in self.tree().json()["entries"]], ["sub", "a.md", "big.bin"])


# ---------------------------------------------------------------- 知らせ（SSE）

class OsErrorTest(WebCase):
    """名前が長すぎる・ソケットのファイルなど、OS のエラーは 500 でなく 404（無いのと同じ）。"""

    LONG = "a" * 300                                       # 1つの名前は 255 バイトまで（APFS）。ENAMETOOLONG

    def make_socket(self, name: str = "s.sock") -> Path:
        """起点の中に Unix ソケットのファイルを作る。AF_UNIX のパスは 104 字までなので、フォルダに入って短い名前で bind する。"""
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            sock.bind(name)
        finally:
            os.chdir(cwd)
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.root / name).st_mode))
        return self.root / name

    def test_a_too_long_name_is_404_on_every_road(self):
        for rel in (self.LONG, f"sub/{self.LONG}", f"{self.LONG}/x", f"{self.LONG}.md"):
            with self.subTest(rel=rel[-20:]):
                self.assert_error(get(self.files, f"/{self.R}/{rel}", TOKEN), 404, "not_found")
                self.assert_error(self.tree(rel), 404, "not_found")
                self.assert_error(self.resolve(str(self.root / rel)), 404, "not_found")
                self.assert_error(self.resolve(rel), 404, "not_found")

    def test_a_socket_file_is_404_and_does_not_break_the_tree(self):
        self.make_socket()
        self.assert_error(get(self.files, f"/{self.R}/s.sock", TOKEN), 404, "not_found")
        self.assert_error(get(self.files, f"/{self.R}/s.sock", Cookie=self.cookie()), 404, "not_found")
        self.assert_error(self.tree("s.sock"), 404, "not_found")
        self.assert_error(self.resolve(str(self.root / "s.sock")), 404, "not_found")
        self.assertEqual(self.tree().status, 200)              # 一覧は出る
        self.assertEqual(get(self.files, f"/{self.R}/a.md", TOKEN).status, 200)

    def test_these_are_not_logged_as_failures(self):
        self.make_socket()
        with capture_log() as rec:
            get(self.files, f"/{self.R}/{self.LONG}", TOKEN)
            get(self.files, f"/{self.R}/s.sock", TOKEN)
            self.assertTrue(wait_until(lambda: len(rec.lines) >= 2))
        self.assertEqual([l for l in rec.lines if "failed" in l.lower()], [], rec.lines)
        self.assertTrue(all(" 404 " in l for l in rec.lines), rec.lines)

    def test_a_real_io_error_is_still_500(self):
        # 「無い」と言えないエラー（EIO など）を 404 にしない。原因を隠さないため
        import errno

        real_open = os.open

        def boom(path, flags, *a, **kw):
            if str(path).endswith("a.md"):                  # 鍵の置き場など、ほかの open は通す
                raise OSError(errno.EIO, "Input/output error")
            return real_open(path, flags, *a, **kw)

        with mock.patch("bblist.web.os.open", side_effect=boom), capture_log() as rec:
            self.assert_error(get(self.files, f"/{self.R}/a.md", TOKEN), 500, "unreadable")
            self.assertTrue(wait_until(lambda: len(rec.lines) >= 2))
        self.assertTrue(any("OSError" in l for l in rec.lines), rec.lines)   # 原因の型は記録に残る（文の言語には頼らない）
        self.assertTrue(any(" 500 " in l for l in rec.lines), rec.lines)


class LogMethodTest(WebCase):
    """記録に書く要求の方法は、英字 16 字までだけ。ほかは ?（端末の制御文字などを記録に入れない）。"""

    def logged(self, request: bytes) -> list:
        with capture_log() as rec:
            status, _ = exchange(self.api, request)
            self.assertTrue(wait_until(lambda: len(rec.lines) >= 1))
        return rec.lines

    def test_a_hostile_method_is_logged_as_a_question_mark(self):
        for name, method in (("escape", b"G\x1b[2JET"), ("non-ascii", "G\u00c9T".encode("latin-1")),
                             ("digits", b"GET1"), ("too long", b"A" * 17), ("percent", b"%1b%1b")):
            with self.subTest(name):
                lines = self.logged(method + b" /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertRegex(lines[0], r"\Aapi \? /v1/health 405 \d+ms\Z", lines)
                self.assertNotIn("\x1b", lines[0])

    def test_ordinary_methods_are_kept(self):
        for method, status in ((b"GET", 200), (b"POST", 405), (b"DELETE", 405), (b"get", 405), (b"A" * 16, 405)):
            with self.subTest(method):
                lines = self.logged(method + b" /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertRegex(lines[0], rf"\Aapi {method.decode()} /v1/health {status} \d+ms\Z", lines)


class SSETest(WebCase):
    HUB = {"poll_sessions": 0.05, "poll_log": 0.05, "poll_usage": 0.05, "debounce": 0.0}
    START_HUB = True

    def test_sse_events_and_ping(self):
        with SSE(self.api, f"?watch={B}") as ev:
            self.assertEqual(ev.status, 200)
            self.assertEqual(ev.resp.getheader("Content-Type"), "text/event-stream")
            self.assertEqual(ev.resp.getheader("Cache-Control"), "no-store")
            self.append_log(human("u-new", "足した"))
            self.assertEqual(ev.wait_event("transcript"), {"bridgeSessionId": B, "sessionId": SID})
            ev.wait_ping()

    def test_sse_dead_client_frees_slot(self):
        conns = [SSE(self.api) for _ in range(4)]
        self.addCleanup(lambda: [c.close() for c in conns])
        self.assertEqual([c.status for c in conns], [200] * 4)
        conns[0].resp.close()                                  # 黙って閉じる（眠った iPad）。サーバーには何も言わない
        # ping の書き込みに失敗して枠が空く（白箱：購読の数で待つ。5本目は追い出しでも 200 になるので、それでは確かめられない）
        self.assertTrue(wait_until(lambda: len(self.hub._subs) == 3), "閉じたまま消えた相手の枠が空かない")
        fifth = SSE(self.api)
        conns.append(fifth)
        self.assertEqual(fifth.status, 200)
        for c in conns[1:]:                                    # 空いた枠に入ったので、生きている接続は1本も切られない
            self.assertTrue(c.alive_for(3 * self.PING))

    def test_sse_of_a_revoked_device_ends_at_the_next_ping_and_others_go_on(self):
        # 取り消した端末の開いたままの /v1/events：次の ping で端末がもう組まれていないと分かり、枠を返して閉じる
        other, other_token = self.add_device()
        with SSE(self.api) as revoked, SSE(self.api, token=other_token) as kept:
            self.assertEqual((revoked.status, kept.status), (200, 200))
            self.assertTrue(wait_until(lambda: len(self.hub._subs) == 2))
            self.assertTrue(DeviceStore(self.own[0]).revoke(self.DEV))              # 別のプロセス（bbctl revoke）が取り消す
            t0 = time.monotonic()
            self.assertTrue(revoked.ends(), "取り消した端末の流れが終わらない")
            self.assertLess(time.monotonic() - t0, 3 * self.PING + 0.5)            # ping の間隔のうちに
            self.assertTrue(wait_until(lambda: len(self.hub._subs) == 1), "取り消した端末の枠が空かない")
            self.assertTrue(kept.alive_for(3 * self.PING))                          # ほかの端末は切られない

    def test_sse_of_a_revoked_device_gets_no_event_after_the_revoke(self):
        with SSE(self.api, f"?watch={B}") as ev:
            self.assertEqual(ev.status, 200)
            self.assertTrue(DeviceStore(self.own[0]).revoke(self.DEV))
            self.append_log(human("u-after", "取り消したあとの変化"))
            try:
                ev.wait_event("transcript", timeout=1.0)
            except (AssertionError, EOFError):
                pass                                                                # 来ない（流れが閉じるか、来ないまま時間切れ）
            else:
                self.fail("取り消した端末に、変化の知らせが届いた")

    def test_sse_fifth_evicts_the_oldest(self):
        for _ in range(3):                                     # 401 は枠を使わない
            with SSE(self.api, token="x") as bad:
                self.assertEqual(bad.status, 401)
        conns = [SSE(self.api) for _ in range(4)]
        self.addCleanup(lambda: [c.close() for c in conns])
        self.assertEqual([c.status for c in conns], [200] * 4)
        fifth = SSE(self.api)                                  # 断らない（眠ったまま消えた iPad に、目覚めた iPad を締め出させない）
        conns.append(fifth)
        self.assertEqual(fifth.status, 200)
        t0 = time.monotonic()
        self.assertTrue(conns[0].ends(), "一番古い接続が終わらない")
        self.assertLess(time.monotonic() - t0, self.PING + 0.3)   # ping の間隔のうちに終わる
        for c in conns[1:]:                                    # ほかは切られない
            self.assertTrue(c.alive_for(3 * self.PING))


class EventLatencyTest(WebCase):
    HUB = {"poll_log": 0.5, "debounce": 0.3}                   # 本物の間隔
    START_HUB = True

    def test_event_latency_median_under_1s(self):              # 変化から知らせが届くまでの秒数
        samples = []
        with SSE(self.api, f"?watch={B}") as ev:
            self.assertEqual(ev.status, 200)
            for i in range(10):
                self.append_log(human(f"lat-{i}", "書き足し"))
                t0 = time.monotonic()
                ev.wait_event("transcript")
                samples.append(time.monotonic() - t0)
        med, worst = statistics.median(samples), max(samples)
        log.info("Event latency: median %.3f worst %.3f all %s", med, worst, [round(s, 3) for s in samples])
        self.assertLessEqual(med, 1.0, samples)
        self.assertLessEqual(worst, 2.0, samples)


# ---------------------------------------------------------------- 起動口

class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.support = self.tmp / "support"
        self.logs = self.tmp / "logs"
        (self.tmp / "claude" / "sessions").mkdir(parents=True)
        (self.tmp / "lab").mkdir()
        self.support.mkdir()

    def args(self, *extra: str) -> list:
        return ["--claude-dir", str(self.tmp / "claude"),
                "--support-dir", str(self.support), "--log-dir", str(self.logs), *extra]

    def log_text(self) -> str:
        return (self.logs / "listserver.log").read_text(encoding="utf-8")

    # ---- 待ち受け（Tailscale の IP・MagicDNS の名前・設定）。偽の tailscale_ip / magicdns_name / make_servers を差し替える

    def serve_until(self, argv, probe):
        """main を主の糸で走らせ、別の糸の probe が終わったら SIGTERM で止める。(終了コード, probe の返した値)。
        probe の例外は、止めたあとで上げ直す。"""
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        box, done = {}, threading.Event()

        def worker():
            try:
                box["value"] = probe()
            except BaseException as e:                         # noqa: BLE001 止めたあとで上げ直す
                box["error"] = e
            finally:
                while not done.is_set():                       # main が止め方を入れるまで待つ。本物の合図は送らず、入れた受け手を直接呼ぶ
                    handler = signal.getsignal(signal.SIGTERM)     # （合図を送ると、受け手が戻された後では試験ごと落ちる）
                    if callable(handler):
                        handler(signal.SIGTERM, None)
                        break
                    time.sleep(0.02)

        threads_before = set(threading.enumerate())
        t = threading.Thread(target=worker)
        t.start()
        try:
            code = listserver.main(argv)
        finally:
            done.set()
            t.join()
        if "error" in box:
            raise box["error"]
        self.assertEqual(join_new_threads(threads_before), [])    # 見回り・口・要求の糸がどれも残らない
        return code, box.get("value")

    def wait_log(self, text: str, timeout: float = 10.0, count: int = 1) -> bool:
        def seen():
            f = self.logs / "listserver.log"
            return f.exists() and f.read_text(encoding="utf-8").count(text) >= count
        return wait_until(seen, timeout)

    def ports_in_log(self, nth: int = 0):
        found = re.findall(r"api port (\d+), file port (\d+)", self.log_text())
        return int(found[nth][0]), int(found[nth][1])

    def fake_make(self):
        """web.make_servers の代わり。呼ばれた (host, 本体の口, ファイル口, allowed_hosts, ctx) を calls に残し（made の後）、
        実際には loopback の port 0 に立てる（Tailscale の IP は、この Mac では結べない）。"""
        self.calls, self.made = [], []

        def make(host, api_port, file_port, ctx):
            servers = make_servers(HOST, 0, 0, ctx)
            self.made.append(servers)
            self.calls.append((host, api_port, file_port, ctx.allowed_hosts, ctx))   # 最後に足す：calls を待った糸は made も使える
            return servers

        return mock.patch.object(listserver, "make_servers", side_effect=make)

    @staticmethod
    def answers(*values):
        """呼ぶたびに values を順に返し、尽きたら最後の値を返し続ける関数。"""
        it = iter(values)
        last = []

        def fn(*args):
            try:
                last[:] = [next(it)]
            except StopIteration:
                pass
            return last[0]

        return fn

    def patches(self, ip, name="mac.example.ts.net", interval=0.05):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.ip = stack.enter_context(mock.patch.object(listserver, "tailscale_ip", side_effect=ip))
        self.name = stack.enter_context(mock.patch.object(listserver, "magicdns_name", side_effect=name))
        stack.enter_context(mock.patch.object(listserver, "IP_CHECK_SECONDS", interval))
        return stack

    def test_main_waits_for_tailscale_instead_of_exiting(self):
        # Tailscale が落ちている間（IP が取れない）、終了せずに確かめ直す。launchd に10秒ごとに立て直させない
        self.patches(self.answers(None, None, None, None, HOST), name=lambda *a: "mac.example.ts.net")

        def probe():
            self.assertTrue(self.wait_log("Listening on"), "IP が戻ったのに待ち受けない")
            api, _ = self.ports_in_log()
            return (get(api, "/v1/health", Host="mac.example.ts.net").status, get(api, "/v1/health", Host="rebind.example").status)

        code, seen = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, seen), (0, (200, 421)))        # 戻ってから待ち受け、MagicDNS の名前の Host も通る
        self.assertGreaterEqual(self.ip.call_count, 5)
        text = self.log_text()
        self.assertEqual(text.count("Waiting for Tailscale"), 1)   # 確かめ直すたびには書かない
        self.assertNotIn("launchd", text)

    def test_main_does_not_exit_when_tailscale_never_comes_back(self):
        self.patches(lambda *a: None)

        def probe():
            time.sleep(0.4)                                    # 8回ほど確かめても終わらない
            return self.ip.call_count

        code, calls = self.serve_until(self.args(), probe)
        self.assertEqual(code, 0)                              # 終わったのは SIGTERM のとき（1 で自分から終わらない）
        self.assertGreaterEqual(calls, 5)
        self.assertNotIn("Listening on", self.log_text())
        self.assertIn("Waiting for Tailscale", self.log_text())

    def test_relisten_on_ip_change(self):
        # IP が変わったら、古い待ち受けを閉じて新しい IP で待ち受け直す。Host も新しい IP になる
        self.patches(self.answers("100.64.0.1", "100.64.0.1", "100.64.0.2"), name=lambda *a: "mac.example.ts.net")
        patch = self.fake_make()

        def probe():
            self.assertTrue(wait_until(lambda: len(self.calls) >= 2), "IP が変わっても待ち受け直さない")
            new_api = self.made[1][0].server_address[1]
            seen = {"new_ip": get(new_api, "/v1/health", Host="100.64.0.2").status,
                    "old_ip": get(new_api, "/v1/health", Host="100.64.0.1").status,
                    "name": get(new_api, "/v1/health", Host="mac.example.ts.net").status}
            time.sleep(0.4)                                    # IP が同じ間は、確かめ直しても待ち受け直さない
            seen["calls"] = len(self.calls)
            seen["old_closed"] = [s.socket.fileno() for s in self.made[0]]
            return seen

        with patch:
            code, seen = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"new_ip": 200, "old_ip": 421, "name": 200, "calls": 2, "old_closed": [-1, -1]})
        self.assertEqual([c[0] for c in self.calls], ["100.64.0.1", "100.64.0.2"])
        self.assertIn("100.64.0.1", self.calls[0][3])
        self.assertNotIn("100.64.0.1", self.calls[1][3])
        self.assertIn("100.64.0.2", self.calls[1][3])
        self.assertIn("Tailscale IP changed from 100.64.0.1 to 100.64.0.2", self.log_text())
        self.assertEqual([s.socket.fileno() for pair in self.made for s in pair], [-1] * 4)   # 止めたら全部閉じる

    def test_one_missed_answer_does_not_drop_the_listener_but_a_lasting_loss_does(self):
        # 1回取れなかっただけでは閉じない（つながっている SSE を切らない）。続けて取れなければ閉じ、戻ったら（同じ IP でも）開き直す
        self.patches(self.answers("100.64.0.1", None, "100.64.0.1", None, None, None, "100.64.0.1"))
        patch = self.fake_make()

        def probe():
            self.assertTrue(wait_until(lambda: len(self.calls) >= 2), "戻っても待ち受け直さない")
            return [s.socket.fileno() for s in self.made[0]]

        with patch:
            code, closed = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, closed, [c[0] for c in self.calls]), (0, [-1, -1], ["100.64.0.1", "100.64.0.1"]))
        text = self.log_text()
        self.assertEqual(text.count("stopped listening"), 1)    # 1回だけの取りこぼしの方では書かない
        self.assertEqual(text.count("Listening on"), 2)

    def test_the_magicdns_name_is_refreshed_without_listening_again(self):
        self.patches(lambda *a: HOST, name=self.answers("old.example.ts.net", "new.example.ts.net"))
        patch = self.fake_make()

        def probe():
            self.assertTrue(self.wait_log("Listening on"))
            api = self.made[0][0].server_address[1]
            self.assertTrue(wait_until(lambda: get(api, "/v1/health", Host="new.example.ts.net").status == 200))
            return get(api, "/v1/health", Host="old.example.ts.net").status

        with patch:
            code, old = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, old, len(self.calls)), (0, 421, 1))
        self.assertIn("old.example.ts.net", self.calls[0][3])  # 起動のときの名前が Host に入っていた

    def test_a_failed_name_lookup_keeps_the_last_good_name(self):
        self.patches(lambda *a: HOST, name=self.answers("mac.example.ts.net", None))
        patch = self.fake_make()

        def probe():
            self.assertTrue(self.wait_log("Listening on"))
            api = self.made[0][0].server_address[1]
            self.assertTrue(wait_until(lambda: self.name.call_count >= 4))
            return get(api, "/v1/health", Host="mac.example.ts.net").status

        with patch:
            code, status = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, status), (0, 200))

    def test_a_bind_failure_on_a_found_ip_is_retried_not_fatal(self):
        self.patches(lambda *a: "100.64.0.1")
        real = make_servers
        tries = []

        def flaky(host, api_port, file_port, ctx):
            tries.append(host)
            if len(tries) < 3:
                raise OSError(48, "Address already in use")
            return real(HOST, 0, 0, ctx)

        def probe():
            self.assertTrue(self.wait_log("Listening on"), "ポートが空いても待ち受けない")

        with mock.patch.object(listserver, "make_servers", side_effect=flaky):
            code, _ = self.serve_until(self.args("--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, len(tries)), (0, 3))
        self.assertEqual(self.log_text().count("Cannot listen on 100.64.0.1"), 1)   # 同じ問題は確かめ直すたびには書かない

    def test_a_found_ip_outside_the_tailscale_range_is_never_listened_on(self):
        # tailscale が（壊れて）別の範囲の IP を答えても、結ばない・終わらない。待ち受けは Tailscale の IPv4 か loopback だけ
        for bad in ("192.168.1.5", "0.0.0.0", "8.8.8.8"):
            with self.subTest(ip=bad):
                self.patches(lambda *a, bad=bad: bad)
                real = make_servers
                seen = []
                with mock.patch.object(listserver, "make_servers", side_effect=lambda *a: seen.append(a[0]) or real(*a)):
                    code, calls = self.serve_until(self.args(), lambda: (time.sleep(0.3), len(seen))[1])
                self.assertEqual(code, 0)
                self.assertGreaterEqual(calls, 5)                  # 確かめ直すたびに断られる
        text = self.log_text()
        self.assertNotIn("Listening on", text)
        self.assertEqual(text.count("Cannot listen on 192.168.1.5: refusing to listen"), 1)   # 記録は1回

    def test_a_fixed_host_that_cannot_be_listened_on_exits_1(self):
        for host in ("192.168.1.10", "0.0.0.0", "0", "8.8.8.8"):   # Tailscale の範囲・loopback の外は、どれも結ばない
            with self.subTest(host=host):
                self.assertEqual(listserver.main(self.args("--host", host)), 1)
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind((HOST, 0))
        taken.listen()
        self.assertEqual(listserver.main(self.args("--host", HOST, "--api-port", str(taken.getsockname()[1]),
                                                   "--file-port", "0")), 1)       # 口が使われている
        self.assertEqual(self.log_text().count("Cannot listen on"), 5)
        self.assertEqual(logging.getLogger().handlers, [])                        # 記録の先を残さない（何度呼んでも増えない）

    def test_the_same_api_and_file_port_exits_2_before_anything_starts(self):
        """本体の口とファイル口が同じ番号なら、起動のときに記録して終了コード 2（フラグと config のどの組み合わせでも）。
        0（OS に選ばせる）は2つあっても別の口になるので、同じとは数えない。"""
        def config(**items):
            (self.support / "config.json").write_text(json.dumps(items), encoding="utf-8")
            os.chmod(self.support / "config.json", 0o600)

        cases = [("flags", {}, ("--api-port", "54321", "--file-port", "54321")),
                 ("flag api = default file port", {}, ("--api-port", "8773")),
                 ("flag file = default api port", {}, ("--file-port", "8772")),
                 ("flag api = config file port", {"filePort": 54322}, ("--api-port", "54322")),
                 ("flag file = config api port", {"apiPort": 54323}, ("--file-port", "54323"))]
        for label, items, flags in cases:
            with self.subTest(label):
                shutil.rmtree(self.logs, ignore_errors=True)
                config(**items)
                with mock.patch.object(listserver, "make_servers", side_effect=AssertionError("started")) as make:
                    self.assertEqual(listserver.main(self.args("--host", HOST, *flags)), 2)
                make.assert_not_called()
                self.assertTrue(any(w in self.log_text() for w in ("must differ", "same")), self.log_text())
                self.assertFalse((self.support / "secret").exists())                  # 秘密も作る前に終わる
        shutil.rmtree(self.logs, ignore_errors=True)
        with self.fake_make():                                                    # 0 と 0 は別の口：これまでどおり立つ
            code, _ = self.serve_until(self.args("--host", HOST, "--api-port", "0", "--file-port", "0"),
                                       lambda: wait_until(lambda: len(self.calls) >= 1))
        self.assertEqual(code, 0)

    def test_a_fixed_host_never_asks_tailscale(self):
        self.patches(lambda *a: self.fail("tailscale ip を呼んだ"), name=lambda *a: self.fail("magicdns を呼んだ"))

        def probe():
            self.assertTrue(self.wait_log("Listening on"))
            api, _ = self.ports_in_log()
            time.sleep(0.3)
            return get(api, "/v1/health").status

        code, status = self.serve_until(self.args("--host", HOST, "--api-port", "0", "--file-port", "0"), probe)
        self.assertEqual((code, status), (0, 200))
        self.assertEqual((self.ip.call_count, self.name.call_count), (0, 0))

    # ---- 設定ファイル（config.json）が配線されている

    def write_config(self, data, mode=0o600):
        p = self.support / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(p, mode)

    def test_config_json_is_wired_into_the_server(self):
        lab = self.tmp / "my-lab"
        for rel in ("10_a/Alpha", "20_b/Beta"):
            (lab / rel).mkdir(parents=True)
        other_claude = self.tmp / "other-claude"
        (other_claude / "sessions").mkdir(parents=True)
        self.write_config({"apiPort": 18772, "filePort": 18773, "claudeDir": str(other_claude),
                           "showGitIgnored": True, "previewExternal": False,
                           "launcherRoots": [{"path": str(lab), "depth": 2}]})
        patch = self.fake_make()
        args = ["--support-dir", str(self.support), "--log-dir", str(self.logs), "--host", HOST]

        def probe():
            self.assertTrue(wait_until(lambda: len(self.calls) >= 1))
            ctx = self.calls[0][4]
            _, token = DeviceStore(self.support).add("Config iPad")             # 組んだ端末の鍵（bbctl pair の代わり）
            diag = get(self.made[0][0].server_address[1], "/v1/diagnostics", token).json()   # 本物の口の答えにも出る
            return (ctx.preview_external, ctx.gitignore.show_ignored,
                    sorted((p["name"], p["category"]) for p in ctx.catalog.overview()["projects"]),
                    diag["previewExternal"], diag["gitignore"]["showIgnored"])

        with patch, mock.patch.object(listserver, "Catalog", wraps=listserver.Catalog) as catalog:
            code, seen = self.serve_until(args, probe)
        self.assertEqual(code, 0)
        self.assertEqual(seen, (False, True, [("Alpha", "10_a"), ("Beta", "20_b")], False, True))
        self.assertEqual(self.calls[0][1:3], (18772, 18773))                      # 設定の口
        self.assertEqual(catalog.call_args.args[0], other_claude)                 # 設定の claudeDir
        self.assertIs(catalog.call_args.kwargs["launcher_auto"], True)            # 起点が空のときだけ使われる旗は、いつも立てる

    def test_command_line_flags_win_over_config_json(self):
        self.write_config({"apiPort": 18772, "filePort": 18773, "claudeDir": str(self.tmp / "ignored-claude")})
        patch = self.fake_make()

        def probe():
            self.assertTrue(wait_until(lambda: len(self.calls) >= 1))

        with patch, mock.patch.object(listserver, "Catalog", wraps=listserver.Catalog) as catalog:
            self.serve_until(self.args("--host", HOST, "--api-port", "0", "--file-port", "1"), probe)
        self.assertEqual(self.calls[0][1:3], (0, 1))
        self.assertEqual(catalog.call_args.args[0], self.tmp / "claude")

    def test_defaults_without_a_config_file(self):
        patch = self.fake_make()
        with patch, mock.patch.object(listserver, "Catalog", wraps=listserver.Catalog):
            self.serve_until(["--support-dir", str(self.support), "--log-dir", str(self.logs), "--host", HOST,
                              "--claude-dir", str(self.tmp / "claude")],
                             lambda: wait_until(lambda: len(self.calls) >= 1))
        ctx = self.calls[0][4]
        self.assertEqual(self.calls[0][1:3], (8772, 8773))
        self.assertEqual((ctx.preview_external, ctx.gitignore.show_ignored), (True, False))   # 外は許す・無視されたものは隠す
        self.assertFalse((self.support / "config.json").exists())                 # サーバーは設定を作らない

    def test_an_unsafe_config_json_is_ignored_and_logged_without_its_values(self):
        self.write_config({"apiPort": 18772, "filePort": 18773, "showGitIgnored": True, "tailscale": "/tmp/evil-secret"},
                          mode=0o666)
        patch = self.fake_make()
        with patch:
            self.serve_until(self.args("--host", HOST), lambda: wait_until(lambda: len(self.calls) >= 1))
        self.assertEqual(self.calls[0][1:3], (8772, 8773))
        self.assertFalse(self.calls[0][4].gitignore.show_ignored)
        text = self.log_text()
        self.assertIn("config.json: not read (writable by group or others", text)
        self.assertNotIn("evil-secret", text)

    def test_the_old_lab_dir_option_is_gone(self):
        # ランチャーの起点は設定の launcherRoots。--lab-dir は無い（引数の解釈で終わる）
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            listserver.main(self.args("--lab-dir", str(self.tmp / "lab")))

    def test_listserver_has_no_token_file_any_more(self):
        self.assertFalse(hasattr(listserver, "read_token"))

    def test_main_passes_the_real_claude_dir_to_the_catalog_as_a_place_to_hide(self):
        # --claude-dir がリンクでも（~/.claude → dotfiles/claude）、実体のフォルダを「見せない」場所に入れる
        real = self.tmp / "dotfiles" / "claude"
        (real / "sessions").mkdir(parents=True)
        link = self.tmp / "claude-link"
        link.symlink_to(real)

        class Stop(Exception):
            pass

        with mock.patch.object(listserver, "Catalog", side_effect=Stop) as catalog:
            with self.assertRaises(Stop):
                listserver.main(["--claude-dir", str(link), "--support-dir", str(self.support),
                                 "--log-dir", str(self.logs), "--host", HOST])
        own_dirs = catalog.call_args.args[3]
        self.assertEqual(own_dirs, [self.support, self.logs, Path(os.path.realpath(real))])

    def test_main_creates_the_secret_and_gives_it_to_the_catalog_and_the_context(self):
        (self.tmp / "claude" / "sessions").mkdir(parents=True, exist_ok=True)

        class Stop(Exception):
            pass

        with mock.patch.object(listserver, "Catalog", side_effect=Stop) as catalog:
            with self.assertRaises(Stop):
                listserver.main(self.args("--host", HOST))
        secret = (self.support / "secret").read_bytes()
        self.assertEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(os.stat(self.support / "secret").st_mode), 0o600)
        self.assertEqual(catalog.call_args.kwargs["secret"], secret)
        self.assertNotIn(secret.hex(), self.log_text())

    def test_main_reuses_the_secret_so_ids_and_keys_survive_a_restart(self):
        seen = []

        class Stop(Exception):
            pass

        with mock.patch.object(listserver, "Catalog", side_effect=Stop) as catalog:
            for _ in range(2):
                with self.assertRaises(Stop):
                    listserver.main(self.args("--host", HOST))
                seen.append(catalog.call_args.kwargs["secret"])
        self.assertEqual(seen[0], seen[1])

    def test_main_removes_stale_qr_images_at_start_but_not_a_fresh_one(self):
        # bbctl が SIGKILL などで残した PNG（5分より古い）は、サーバーの起動で消す。組んでいる最中の新しい PNG は残す
        folder = self.support / "pair"
        folder.mkdir()
        old, fresh = folder / "qr-1.png", folder / "qr-2.png"
        for p in (old, fresh):
            p.write_bytes(b"x")
        t = time.time() - 3600
        os.utime(old, (t, t))

        class Stop(Exception):
            pass

        with mock.patch.object(listserver, "Catalog", side_effect=Stop):
            with self.assertRaises(Stop):
                listserver.main(self.args("--host", HOST))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_main_exits_1_when_the_secret_cannot_be_used(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_bytes(b"x" * 32)
        (self.support / "secret").symlink_to(elsewhere)                   # リンクの secret は使わない
        self.assertEqual(listserver.main(self.args("--host", HOST)), 1)
        text = self.log_text()
        self.assertIn("Cannot read or create the secret (OSError)", text)       # 型の名前と、置き場のパス
        self.assertIn(str(self.support), text)
        self.assertNotIn("Errno", text)                                         # 例外の文（行き先のパスが入りうる）は書かない
        self.assertNotIn(str(elsewhere), text)
        self.assertNotIn("x" * 32, text)
        self.assertEqual(elsewhere.read_bytes(), b"x" * 32)

    def test_main_serves_on_loopback_until_sigterm(self):
        (self.support / "token").write_text(TOKEN + "\n", encoding="utf-8")   # 旧い token のファイルは、もう見ない
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        seen = {}

        def probe():
            m = None
            deadline = time.monotonic() + 10
            while m is None and time.monotonic() < deadline:
                time.sleep(0.05)
                if (self.logs / "listserver.log").exists():
                    m = re.search(r"api port (\d+), file port (\d+)", self.log_text())
            if m is None:
                return                                                        # 起動していない。main はもう終わっている
            api, files = int(m.group(1)), int(m.group(2))
            try:
                seen["health"] = get(api, "/v1/health").json()["ok"]
                seen["name"] = get(api, "/v1/health", Host="Mac.Example.ts.net:8772").status      # --magicdns-name の名前
                seen["rebind"] = get(api, "/v1/health", Host="rebind.example").status
                seen["old_token_file"] = get(api, "/v1/sessions", TOKEN).status          # 端末の鍵が1つも無い間は誰も通らない
                # 別のプロセスの bbctl がすること：符号を作る（pairing.json）→ アプリが POST → 動いているサーバーが引き換える
                code = Pairing(self.support).create()
                paired = post(api, "/v1/pair", json.dumps({"code": code, "deviceName": "Probe iPad"}).encode())
                seen["pair"] = paired.status
                token, dev = paired.json()["token"], paired.json()["deviceId"]
                seen["token"] = token
                seen["pair_again"] = post(api, "/v1/pair", json.dumps({"code": code, "deviceName": "x"}).encode()).status
                seen["sessions"] = get(api, "/v1/sessions", token).status
                seen["files"] = get(files, "/0000000000000000/a.md", token).status
                seen["listed"] = [d["name"] for d in DeviceStore(self.support).list()]
                seen["hub"] = any(t.name == "bblist-hub" for t in threading.enumerate())
                DeviceStore(self.support).revoke(dev)                                    # bbctl revoke。再起動なしで効く
                seen["revoked"] = get(api, "/v1/sessions", token).status
            finally:
                if callable(signal.getsignal(signal.SIGTERM)):                # main の止め方が入っているときだけ送る
                    os.kill(os.getpid(), signal.SIGTERM)

        threads_before = set(threading.enumerate())
        t = threading.Thread(target=probe)
        t.start()
        code = listserver.main(self.args("--host", HOST, "--api-port", "0", "--file-port", "0",
                                         "--magicdns-name", "mac.example.ts.net"))
        t.join()
        self.assertEqual(code, 0)
        token = seen.pop("token")
        self.assertEqual(seen, {"health": True, "name": 200, "rebind": 421, "old_token_file": 401, "pair": 200,
                                "pair_again": 403, "sessions": 200, "files": 404, "listed": ["Probe iPad"], "hub": True,
                                "revoked": 401})
        self.assertEqual(join_new_threads(threads_before), [])               # 見回り・口・要求の糸がどれも残らない
        self.assertIs(signal.getsignal(signal.SIGTERM), before)              # 止め方を元に戻す
        text = self.log_text()
        for want in ("Listening on 127.0.0.1", "api GET /v1/health 200", "api GET /v1/sessions 200", "api POST /v1/pair 200",
                     "api POST /v1/pair 403", "files GET /*/* 404", "Stopped"):
            self.assertIn(want, text)
        for secret in (TOKEN, token, "Probe iPad"):
            self.assertNotIn(secret, text)


    def test_bbctl_pairs_a_device_with_the_running_server(self):
        # bbctl（別のプロセスのつもり）と動いているサーバーが、置き場のファイルだけを通して組み、取り消せる（通しの試験）
        import bbctl
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        seen = {}
        said = io.StringIO()
        dns = mock.patch.object(bbctl, "magicdns_status", return_value=(True, "mac.example.ts.net"))   # 本物の tailscale は呼ばない
        dns.start()
        self.addCleanup(dns.stop)

        def run_bbctl(*argv):
            with contextlib.redirect_stdout(said):
                seen.setdefault("codes", []).append(bbctl.main(list(argv)))

        def probe():
            m = None
            deadline = time.monotonic() + 10
            while m is None and time.monotonic() < deadline:
                time.sleep(0.05)
                if (self.logs / "listserver.log").exists():
                    m = re.search(r"api port (\d+), file port (\d+)", self.log_text())
            if m is None:
                return
            api = int(m.group(1))
            try:
                # 符号を作る前の /v1/health は、本物の bbctl.check_health で、いま動いているこのサーバーに当てる
                pair = threading.Thread(target=run_bbctl, args=("pair", "--support-dir", str(self.support), "--ip", HOST,
                                                                 "--api-port", str(api), "--host", "mac.example.ts.net", "--no-open"))
                with mock.patch("secrets.token_urlsafe", side_effect=lambda n=None: CODE if n == 16 else REAL_TOKEN_URLSAFE(n)):
                    pair.start()
                    while not (self.support / "pairing.json").exists() and pair.is_alive():
                        time.sleep(0.02)
                    r = post(api, "/v1/pair", json.dumps({"code": CODE, "deviceName": "E2E iPad"}).encode())
                pair.join(15)
                seen["pair"], seen["alive"] = r.status, pair.is_alive()
                token, dev = r.json()["token"], r.json()["deviceId"]
                seen["token"] = token
                seen["sessions"] = get(api, "/v1/sessions", token).status
                run_bbctl("devices", "--support-dir", str(self.support))
                run_bbctl("revoke", dev, "--support-dir", str(self.support))
                seen["revoked"] = get(api, "/v1/sessions", token).status
            finally:
                if callable(signal.getsignal(signal.SIGTERM)):
                    os.kill(os.getpid(), signal.SIGTERM)

        original = bbctl.POLL_SECONDS
        bbctl.POLL_SECONDS = 0.05
        self.addCleanup(setattr, bbctl, "POLL_SECONDS", original)
        t = threading.Thread(target=probe)
        t.start()
        code = listserver.main(self.args("--host", HOST, "--api-port", "0", "--file-port", "0"))
        t.join()
        self.assertEqual(code, 0)
        token = seen.pop("token")
        self.assertEqual(seen, {"pair": 200, "alive": False, "sessions": 200, "revoked": 401, "codes": [0, 0, 0]})
        out = said.getvalue()
        self.assertIn("paired: E2E iPad\n", out)
        self.assertIn("E2E iPad", out.split("paired: E2E iPad\n", 1)[1])          # devices の一覧にも出る
        self.assertRegex(out, r"revoked: [0-9a-f]{16}\n")
        for secret in (CODE, token):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log_text())
        self.assertEqual(list((self.support / "pair").glob("qr-*.png")), [])

if __name__ == "__main__":
    unittest.main()
