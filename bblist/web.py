"""HTTP の2つの口（本体とファイル口）。端末の鍵・組む口・SSE・1要求1行の記録。

Python 3.9 の標準ライブラリだけで動かす。

本体（8772）は JSON と SSE、ファイル口（8773）はプロジェクトのファイルの中身。口を分けるのは、プレビューの html の
JS から会話の道を読ませないため（オリジンが別になり、本体は CORS の見出しを返さない）。
/v1/health と POST /v1/pair 以外は合言葉。合言葉は端末ごとの鍵（devices.json にハッシュだけ。bbctl pair で組む。取り消した
端末は、再起動なしですぐ断る）。本体は Authorization: Bearer だけを受ける。ファイル口は Bearer か、クッキー bb_file
（`<端末の id>.<鍵>`。鍵は file_key(secret, 端末の id, その URL の起点)＝端末ごと・起点ごとに別の値。端末が取り消されていれば
鍵が合っても断る）を受ける。クッキーは口でなく名前で届くので、本体は受けない
（ファイル口で開いた html の JS が読んだり付けたまま本体を叩いたりできる）。クッキー bb_token はもう受けない。
受けるのは GET だけ（HEAD を含め、ほかは 405）。例外は本体の口の POST /v1/pair（認証なし）：5分で切れる一度きりの符号を端末の鍵に替える。
本文は 4 KiB まで・送り元ごとに1分に5回・全体で1分に30回まで（超えると 429）・失敗は理由を分けず 403 pair_failed。符号・鍵・端末の名前は記録に書かない。
どの応答にも X-Content-Type-Options: nosniff（要求の行が崩れたときの標準のエラーにも。HTTP/0.9 の行は答えずに閉じる）。
ファイル口の中身の応答には CSP を付ける：html は開いた html の JS が同じ起点のほかのファイルを読めないように、
svg・xml は script を止め、ほかは文書として開かれても何も読み込まないように（残る道は HTML_CSP の上のコメント）。
一覧の読み直し（Catalog.refresh）は見回りの糸（Hub）だけがする。ここは読むだけ。
記録は1要求1行：口・方法・道の形（ID は *）・状態コード・ms。問い合わせ・相対パス・本文・鍵・符号は書かない。

認証の前の守り：同時に受ける接続の数（全体・送り元ごと）に上限を置き、要求の行と見出しは header_timeout 秒の
うちに読み終えなければ切る（1バイトずつ送り続ける相手も、合計の時間で切る）。見出しの1行は 8 KiB・本数は 32 まで。
Host は allowed_hosts にある名前だけ受け（DNS リバインディング対策）、違えば 421 bad_host。/v1/health は ok と apiVersion だけ。
版・起動時刻・Claude Code の形が合わないものの数は /v1/diagnostics（認証あり）。
診断の数は「普段は 0。0 でないとき Claude Code の形が変わった疑い」：transcripts の unreadableLines（読めない行）・
unknownTypes（知っている種類に無い type の行の数。units.KNOWN_TYPES＝描く種類＋描かないと分かっている種類）・
unknownTypeNames（その名前。最大8個・64字まで・[A-Za-z0-9_.-] だけ）・unknownOriginKinds（人の指示の目印 origin.kind が
知っている値に無かった回数。units.KNOWN_ORIGIN_KINDS）と、sessions の malformed・procMismatch（sessions/*.json の形）。
sessions の dead（終わったセッション）と noBridge（Remote Control を使わないセッション）は普通に増えるので、files との割合で見る
（一覧が空なのに noBridge ばかり、なら bridgeSessionId の名前が変わった疑い）。transcripts は覚えている会話ログ（最大8本）の
合計で、累計ではない。知っている種類・値は本物の会話ログを数えて決めた一覧（units.py の先頭）。
Claude Code が新しい種類を足すと、そこに無い名前が unknownTypeNames に出る。
ツリーとファイル口の 403 は理由を持つ：{"error":"forbidden","reason":"tcc"|"hidden"}。tcc は PermissionError（macOS の
「フォルダへのアクセス」の守りも chmod も）、hidden は見せない起点・拒む名前（名前だけで決める。あるかどうかを見せない）。
無い・知らない起点・git で無視されたものは今までどおり 404。
"""
from __future__ import annotations

import errno
import hmac
import http.client
import io
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

from . import VERSION
from .devices import DEVICE_ID, DeviceStore
from .discover import TAILSCALE_NET
from .files import content_type, list_dir, locate
from .gitignore import GitIgnore
from .hub import Hub
from .pairing import PairLimiter, Pairing
from .paths import has_denied_name, rel_below, resolve_in_root
from .secrets_store import file_key
from .sessions import Catalog
from .transcript import TranscriptStore
from .usage import read_usage

log = logging.getLogger(__name__)

JSON = "application/json; charset=utf-8"
CHUNK = 64 * 1024                                         # ファイル口が1回に送る大きさ
_IMAGE_TYPES = re.compile(r"image/(png|jpeg|gif|webp)")   # 貼った画像の種類。ほかは application/octet-stream で返す
_CLIENT_GONE = (ConnectionError, socket.timeout)          # 相手が切った・眠った（返す先が無い）
API_VERSION = 3                                           # /v1/health と /v1/pair が返す。3＝端末ごとの鍵・組む口・クッキーの値の形
MAX_PAIR_BODY = 4096                                      # POST /v1/pair の本文の上限（バイト）
MAX_DRAIN = 256 * 1024                                    # 上限を超えた本文を、断る前に読み捨てる量（相手が断りの応答を読めるように）
PAIR_RETRY_AFTER = "60"                                   # 429 の Retry-After（秒）。PairLimiter の窓と同じ
_FILE_MAC = re.compile(r"[0-9a-f]{64}")
_METHOD = re.compile(r"[A-Za-z]{1,16}")                   # 記録に書く要求の方法。ほかは ?（制御文字・長い文字列を記録に入れない）
# 「そんな名前・ファイルは無い」と言ってよい OS のエラー。名前が長すぎる・リンクが輪になった・ソケットなど開けない種類
_NOT_FOUND_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.ELOOP, errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP, errno.EILSEQ})

# 見出しの1行は 8 KiB・本数は 32 まで（標準は 64 KiB・100 本）。http.client のモジュール変数なので、このプロセス全体に効く。
# このプロセスは一覧サーバーだけで、要求の見出しは URLSession と WKWebView が付ける数本だけ（読む側の応答の見出しも小さい）。
# 標準は見出しの終わりの空行も数える（len(headers) > _MAXHEADERS）ので、32 本を通すには 33 にする（Host も 1 本に数える）
http.client._MAXLINE = 8192
http.client._MAXHEADERS = 32 + 1

# 本体の道。* は ID 1つ（空は合わない）。記録に書く道の形も、これを / でつないだもの
_API_ROUTES = (
    (("v1", "health"), "_health"),
    (("v1", "sessions"), "_sessions"),
    (("v1", "sessions", "*", "transcript"), "_transcript"),
    (("v1", "sessions", "*", "images", "*", "*"), "_image"),
    (("v1", "sessions", "*", "written"), "_written"),
    (("v1", "sessions", "*", "resolve"), "_resolve"),
    (("v1", "roots", "*", "tree"), "_tree"),
    (("v1", "filekey"), "_filekey"),
    (("v1", "usage"), "_usage"),
    (("v1", "diagnostics"), "_diagnostics"),
    (("v1", "events"), "_events"),
    (("v1", "pair"), "_pair"),
)
_PUBLIC_ROUTES = frozenset({"_health", "_pair"})          # 合言葉なしで答える道
_POST_ROUTES = frozenset({"_pair"})                       # 受ける方法が POST の道（ほかは GET）


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

# ファイル口の応答に付ける CSP。html は tools/csp-check.mjs で WebKit に当てて決めた（実測の値は csp-check の出力）。
#
# 既定（HTML_CSP）：sandbox で opaque origin にする＝開いた html の JS は、同じ起点のほかのファイルを fetch でも iframe 越しでも
# 読めず、クッキー bb_file も読めない。スクリプトは動く（allow-scripts）。connect-src 'none' は fetch・XHR・WebSocket・EventSource を止める。
# 外の画像・CDN・フォントの読み込みは止めない（作者の html の半分近くが外の図・CDN・Google Fonts を使う）。
#
# 残る道（CSP では止められない）。読める材料（1・2）と、外へ運ぶ道（3・4・5）の組み合わせで、同じ起点の値が外へ出うる：
#  1. 同じ起点の JS の型のファイルは <script src> で実行され、値が読める。クラシックな .js（CommonJS の module.exports も
#     短い shim で）が対象で、.mjs は `export` を含まないものだけ（実測）。自分の app.js と同じ型・同じ起点なので区別できない。
#     既定でも strict でも残る。
#  2. 同じ起点の .css は <link> で読み込まれ、カスタムプロパティ（--x: 値）を getComputedStyle で読める。既定でも strict でも残る。
#  3. 外の読み込み（<img>・css・フォントなど）に値を載せて運べる。既定（HTML_CSP）では止まらない。strict では止まる。
#  4. WebRTC の ICE（STUN）が外へ出る。値は宛先のアドレス・ポートに載せられる（実測）。CSP の `webrtc 'block'` は
#     WebKit 26.6 で効かなかった。既定でも strict でも残る。アプリの遷移の判断でも止まらない。プレビューの WKWebView で
#     RTCPeerConnection を無効にする（アプリの仕事）。
#  5. location・form・window.open による外への遷移。アプリの WKNavigationDelegate で別オリジンへの遷移を断る。
#     既定でも strict でも、アプリが断るまで残る。
# 読めない・漏れないもの：.json・.md・.txt・.env など JS でも CSS でもないファイル（nosniff で script・stylesheet として
# 使われない。fetch は connect-src、iframe は sandbox の opaque origin で止まる）・拒む名前（そもそも 404）・ほかの起点
# （クッキーの path と鍵が起点ごと）・クッキー bb_file（sandbox で document.cookie が読めない）。
# よって strict（preview_external=False）は、3 だけを止める。作者の手元の html は半分近くが外の図・CDN・Google Fonts を
# 使うので、strict は崩れる html が多い。切り替えは設定 previewExternal（config.json。既定 true）。1・2・4・5 は設定に関わらず残るので、
# 「strict なら同じ起点の値は外へ出ない」とは言えない。
HTML_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals; connect-src 'none'"
HTML_CSP_STRICT = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
                   "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'none'")
# svg・xml は script も外の読み込みも要らない。sandbox で script を止め、見た目の style と data: の画像だけ許す
XML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
# ほかの中身：文書として開かれても（.mht・message/rfc822 など）何も読み込まない・スクリプトも動かさない。文書でないものには害がない
OTHER_CSP = "default-src 'none'; sandbox"
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_XML_TYPES = frozenset({"image/svg+xml", "text/xml", "application/xml"})


def csp_for(ctype: str, preview_external: bool = True) -> str:
    """Content-Type（charset などの付属は無視）に付ける CSP。html・xhtml は HTML_CSP（preview_external が False なら
    HTML_CSP_STRICT）、svg・xml（+xml を含む）は XML_CSP、ほかは OTHER_CSP。"""
    base = ctype.split(";", 1)[0].strip().lower()
    if base in _HTML_TYPES:
        return HTML_CSP if preview_external else HTML_CSP_STRICT
    if base in _XML_TYPES or base.endswith("+xml"):
        return XML_CSP
    return OTHER_CSP


def _host_key(name: str) -> str:
    """Host の名前を比べる形に直す（小文字・末尾の点なし）。"""
    return name.strip().lower().rstrip(".")


def build_allowed_hosts(listen_ip: str, magicdns: str | None = None) -> frozenset[str]:
    """受ける Host の名前の集まり：待ち受ける IP・loopback・MagicDNS の名前（あれば）。"""
    names = {listen_ip, *LOOPBACK_HOSTS}
    if magicdns:
        names.add(magicdns)
    return frozenset(k for k in map(_host_key, names) if k)


def _host_name(value: str | None) -> str | None:
    """Host の見出しの値から、比べる名前（ポートを除き、小文字・末尾の点なし）を取る。形が崩れていれば None。
    受ける形は `名前`・`名前:数字`・`[IPv6]`・`[IPv6]:数字`。複数の `:`・数字でないポート・閉じていない `[` は崩れている。
    名前の中身（`@` など）は見ない：allowed_hosts にある名前と一致したものだけが通る。"""
    if not value:
        return None
    value = value.strip()
    if value.startswith("["):
        name, close, rest = value[1:].partition("]")
        if not close or ":" not in name:                  # 閉じていない・IPv6 でない（`[127.0.0.1]` は誤り）
            return None
        if rest and not (rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit()):
            return None
    else:
        name, colon, port = value.partition(":")
        if colon and not (port.isascii() and port.isdigit()):
            return None
    return _host_key(name) or None


class BlockedRoots:
    """読もうとして PermissionError になった起点の rootId（診断の tcc.blockedRoots の数え方）。糸をまたいで触る。

    起点ごとに1つ（同じ起点で何度失敗しても1）。次にその起点で読めたら外す（利用者が許可した）。数えるときは、いま見せてよい
    起点の中だけを数えて残りを捨てる（消えた起点を数え続けない）。覚えるのは rootId だけで、パスは持たない。
    """

    def __init__(self):
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def add(self, root_id: str) -> None:
        with self._lock:
            self._ids.add(root_id)

    def discard(self, root_id: str) -> None:
        with self._lock:
            self._ids.discard(root_id)

    def count(self, known) -> int:
        with self._lock:
            self._ids.intersection_update(known)
            return len(self._ids)


@dataclass
class Context:
    """要求を受ける糸が使うもの。catalog は読むだけ（読み直すのは hub の糸）。"""
    devices: DeviceStore                                  # 組んだ端末の鍵（devices.json）。認証はこれだけ
    pairing: Pairing                                      # 組む符号（pairing.json）。POST /v1/pair が引き換える
    secret: bytes                                         # 置き場の secret（secrets_store）。端末・起点ごとのファイルの鍵 file_key の親
    catalog: Catalog
    store: TranscriptStore
    hub: Hub
    usage_path: Path
    started_at: int                                       # 起動した時刻（Unix ミリ秒）
    ping: float = 15.0                                    # SSE の注釈の行（: ping）の間隔（秒）
    max_file_bytes: int = 50 * 1024 * 1024                # ファイル口の1ファイルの上限
    max_conns: int = 64                                   # 1つの口が同時に受ける接続の数（認証の前の守り）
    max_conns_per_ip: int = 16                            # 同じ送り元からの接続の数（SSE が最大4本・眠った iPad の残り・URLSession の並列）
    header_timeout: float = 10.0                          # 要求の行と見出しを読み終えるまでの合計の秒数
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS        # 受ける Host の名前（ポートを除く）。build_allowed_hosts で作る
    # POST /v1/pair の回数の上限（送り元ごとに1分に5回・全体で1分に30回。符号の当てずっぽうを受けない）
    pair_limits: PairLimiter = field(default_factory=PairLimiter)
    # 開いた html の外への読み込み（外の画像・CDN・フォント）を許すか。False なら html は HTML_CSP_STRICT（既定は許す）
    preview_external: bool = True
    # git で無視されたファイルを隠すか（既定は隠す）。ツリー・ファイル口・resolve・written が使う
    gitignore: GitIgnore = field(default_factory=lambda: GitIgnore(show_ignored=False))
    # 読めなかった起点（macOS の守り・chmod）。診断が数だけ返す
    blocked_roots: BlockedRoots = field(default_factory=BlockedRoots)


def _param(query: str, name: str, default: str | None = None) -> str | None:
    """問い合わせの値（最初の1つ）。% は戻すが、+ は + のまま（空白に戻さない。`C++`・`a+b.md` のため）。
    parse_qs は + を空白にするので、先に %2B にしてから渡す。"""
    return parse_qs(query.replace("+", "%2B")).get(name, [default])[0]


def _roots_of(catalog: Catalog, s: dict):
    """files.locate に渡す (そのセッションの起点, 見せてよい起点の全部)。見せない起点（filesVisible: false）は入らない。
    2つを1回の写しから作る（間に見回りが一覧を差し替えても食い違わない）。"""
    roots = catalog.visible_roots()
    rid = s.get("rootId")
    return ((rid, roots[rid]) if rid in roots else None), roots


def _ignored_by_git(ctx: Context, root: Path, target: Path) -> bool:
    """実体のパス target（resolve_in_root が返したもの）が、git で無視されているか。show_ignored・git でない起点・
    git が動かないときは false。途中のシンボリックリンクは resolve_in_root でたどり終えているので、実体で聞く。"""
    rel = rel_below(root, target)
    return bool(rel) and bool(ctx.gitignore.ignored(root, [rel]))


class _Handler(BaseHTTPRequestHandler):
    """2つの口に共通：方法（GET。例外は POST /v1/pair）・端末の鍵・応答の形・1要求1行の記録。道は _route（口ごと）が決める。"""

    server_version = "bblist"                             # 版は書かない（認証の前の応答の Server 見出しで版を教えない）
    sys_version = ""                                      # Python の版を見せない
    timeout = 60                                          # 何も送ってこない・送れない（眠った相手）接続で糸を握り続けない
    port = "-"                                            # 記録に書く口の名前

    def setup(self):
        # 標準の読み手を、見出しを読み終えるまで合計の締め切りを持つものに替える（socket の timeout は1回ごとの待ちで、
        # 1バイトずつ送り続ける相手を切れない）。
        # 締め切りは接続の最初の要求だけに掛かる＝1接続1要求（protocol_version が HTTP/1.0 のままで、応答のあと必ず閉じる）が前提。
        # HTTP/1.1 の keep-alive にするなら、要求ごとに締め切りを掛け直すこと（試験 test_one_request_per_connection が見張る）
        super().setup()
        self.rfile.close()
        self._reader = _HeaderReader(self.connection, self.server.ctx.header_timeout)
        self.rfile = io.BufferedReader(self._reader)

    def parse_request(self):
        # 要求の行は handle_one_request が読み済み。ここで見出しを読む。読み終えたら（失敗も）締め切りを外し、今の timeout にする
        try:
            ok = super().parse_request()
            if ok and self.request_version == "HTTP/0.9":     # 「GET /パス」だけの行。標準の部品は見出しなしの本文だけを返す
                self.close_connection = True                  # （nosniff も Host の確かめも通らない）ので、答えずに閉じる
                return False
            return ok
        finally:
            self._reader.done()
            self.connection.settimeout(self.timeout)

    def send_error(self, code, message=None, explain=None):
        # 要求の行が崩れていて版が読めなかったとき（"GET" だけの行など）、request_version は HTTP/0.9 のままで、標準の部品は
        # 見出しなしの本文だけを返す。見出し（nosniff・Content-Type）を付けて返すため、HTTP/1.0 として返す
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        super().send_error(code, message, explain)

    def do_GET(self):
        self._serve()

    def __getattr__(self, name):
        # GET 以外のどの方法も（知らない方法も）405 にする。do_<方法> が無いと http.server は 501 を返すため
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    def log_request(self, code="-", size="-"):
        self._status = int(code)                          # send_response が呼ぶ。状態コードを覚えるだけ

    def log_message(self, format, *args):
        pass                                              # 標準エラーの記録（要求の行が入る）は出さない。記録は _serve が書く

    # ---------------------------------------------------------------- 受ける

    def _serve(self) -> None:
        t0 = time.monotonic()
        self._status = None
        raw, _, query = self.path.partition("?")
        shape, public, fn, ids, method = self._route(raw)
        self._device = None                               # 認証が通った端末の id（Bearer か、ファイル口のクッキーで）
        try:
            if not self._host_allowed():                  # 合言葉より先に。DNS リバインディングの要求は、道も方法も見ずに断る
                self._error(421, "bad_host")
            elif self.command != method:
                self._error(405, "method_not_allowed", method)
            elif not public and not self._authorized(ids):   # 無い道も 401（道があるかを見せない）
                self._error(401, "unauthorized")
            elif fn is None:
                self._error(404, "not_found")
            else:
                fn(*ids, query)
        except _CLIENT_GONE:
            self.close_connection = True
        except PermissionError:                           # macOS の守り（TCC）・chmod で読めない
            self._fail(403, "forbidden", "tcc")
        except (FileNotFoundError, NotADirectoryError):   # ツリーで見えた後に消えた
            self._fail(404, "not_found")
        except Exception as e:                            # 型の名前だけ書く（メッセージには道や本文が入りうる）
            if isinstance(e, OSError) and e.errno in _NOT_FOUND_ERRNOS:   # 名前が長すぎる・ソケットなど：無いのと同じ
                self._fail(404, "not_found")
            else:
                log.warning("%s %s failed: %s", self.port, shape, type(e).__name__)
                self._fail(500, "unreadable")
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            method = self.command if _METHOD.fullmatch(self.command) else "?"
            log.info("%s %s %s %s %dms", self.port, method, shape, self._status or "-", ms)

    def _route(self, raw: str):
        """(記録に書く道の形, 合言葉不要か, 呼ぶ関数, 道の中の ID, 受ける方法)。合わなければ関数は None（方法は GET）。"""
        raise NotImplementedError

    def _peer(self) -> str:
        """この接続の相手の IP（見出しの X-Forwarded-For などではなく、つながっているソケットの相手）。"""
        return self.client_address[0]

    def _host_allowed(self) -> bool:
        """Host の見出しが1つだけで、名前（ポートを除く）が allowed_hosts にあるか。HTTP/1.1 では Host が要る。
        見出しが無い・2つある・形が崩れている要求は通さない（アプリの URLSession・WKWebView は必ず付ける）。"""
        values = self.headers.get_all("Host") or []
        return len(values) == 1 and _host_name(values[0]) in self.server.allowed_hosts

    def _bearers(self):
        """送られてきた Authorization: Bearer の値（あれば1つ）。"""
        scheme, _, value = (self.headers.get("Authorization") or "").strip().partition(" ")
        if scheme.lower() == "bearer":
            yield value.strip()

    def _cookies(self, name: str):
        """Cookie の見出しにある、その名前の値（いくつあっても全部）。"""
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    yield value

    @staticmethod
    def _same(got: str, want: bytes) -> bool:
        """hmac.compare_digest（ASCII でない値でも落ちないようにバイト列で比べる）。"""
        return bool(want) and hmac.compare_digest(got.encode("utf-8", "replace"), want)

    def _authorized(self, ids) -> bool:
        """合言葉が合うか。Authorization: Bearer が、組まれたままの端末の鍵に合うか、口ごとのクッキー（_cookie_ok）が合う。
        通ったら、その端末の id を self._device に覚える（/v1/filekey が使う）。
        ids は道の中の ID（ファイル口では [rootId, 相対パス]）。"""
        devices = self.server.ctx.devices
        for got in self._bearers():
            device = devices.verify(got) if got else None
            if device is not None:
                self._device = device
                return True
        return self._cookie_ok(ids)

    def _cookie_ok(self, ids) -> bool:
        """クッキーで通してよいか。本体は受けない（クッキーは名前で届くので、別の口の html の JS が付けたまま叩ける）。"""
        return False

    # ---------------------------------------------------------------- 返す

    def end_headers(self):
        # どの応答にも（_head を通らない標準のエラー 400・414・431・505 も）。ファイルの種類を中身から当てさせない
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _head(self, status: int, ctype: str, length: int | None = None, *extra) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")     # どの応答も（エラー・画像・SSE・ファイル）残させない
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def _bytes(self, status: int, ctype: str, body: bytes, *extra) -> None:
        self._head(status, ctype, len(body), *extra)
        if self.command != "HEAD":                        # HEAD の応答には本体を付けない（Content-Length は付けるはずだった本体の長さ）
            self.wfile.write(body)

    def _json(self, obj) -> None:
        # 会話ログ由来の孤立サロゲートは ? にする（UTF-8 にできない1文字で、会話の道ごと 500 にしない）
        self._bytes(200, JSON, json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace"))

    def _error(self, status: int, code: str, allow: str = "GET", reason: str | None = None) -> None:
        extra = [("Allow", allow)] if status == 405 else [("Retry-After", PAIR_RETRY_AFTER)] if status == 429 else []
        body = {"error": code, **({"reason": reason} if reason else {})}   # reason は 403 forbidden だけ（tcc・hidden）
        self._bytes(status, JSON, json.dumps(body).encode("ascii"), *extra)

    def _fail(self, status: int, code: str, reason: str | None = None) -> None:
        """要求の途中の失敗。見出しをまだ送っていなければエラーを返し、送った後なら接続を切るだけ。"""
        self.close_connection = True
        if self._status is None:
            try:
                self._error(status, code, reason=reason)
            except _CLIENT_GONE:
                pass

    def _no_root(self, root_id: str) -> None:
        """root_path が None だった起点：一覧に出ている見せない起点は 403 hidden、知らない起点は 404（rootId は secret 入りで、
        一覧の持ち主＝認証を通った端末しか知らない）。"""
        if self.server.ctx.catalog.root_hidden(root_id):
            return self._error(403, "forbidden", reason="hidden")
        self._error(404, "not_found")

    def _blocked(self, root_id: str) -> None:
        """その起点が読めなかった（PermissionError）：理由 tcc の 403。診断が数えるように起点の rootId を覚える。"""
        self.server.ctx.blocked_roots.add(root_id)
        self._error(403, "forbidden", reason="tcc")


class _ApiHandler(_Handler):
    """本体の口。JSON・貼った画像・SSE。"""

    port = "api"

    def _route(self, raw: str):
        # 先に / で分けてから戻す（%2F で区切りを増やさない）
        segs = [unquote(s) for s in raw.split("/")[1:]] if raw.startswith("/") else []
        for pattern, name in _API_ROUTES:
            if len(pattern) == len(segs) and all(p == s or (p == "*" and s) for p, s in zip(pattern, segs)):
                ids = [s for p, s in zip(pattern, segs) if p == "*"]
                return ("/" + "/".join(pattern), name in _PUBLIC_ROUTES, getattr(self, name), ids,
                        "POST" if name in _POST_ROUTES else "GET")
        return "?", False, None, [], "GET"

    def _session(self, bridge: str) -> dict | None:
        """起動中のセッションの一覧の1行。無ければ 404 を返して None。"""
        s = self.server.ctx.catalog.session(bridge)
        if s is None:                                     # 終わった・知らない
            self._error(404, "not_found")
        return s

    def _health(self, query):
        self._json({"ok": True, "apiVersion": API_VERSION})   # 認証なしで見える。版・起動時刻は /v1/diagnostics（認証あり）へ

    def _diagnostics(self, query):
        """GET /v1/diagnostics（認証あり）：Claude Code のデータの形が合わないものを数えて返す。黙って空にしない。
        数えるのは見回り・会話ログの読みのとき。ここでは覚えた数を返すだけで、ファイルを数えない。パス・rootId・本文は出さない。"""
        ctx = self.server.ctx
        found = ctx.catalog.diagnostics()
        self._json({"version": VERSION, "apiVersion": API_VERSION, "startedAt": ctx.started_at,
                    "claudeCodeVersions": found["claudeCodeVersions"],
                    "sessions": found["sessions"],
                    "transcripts": ctx.store.diagnostics(),
                    "gitignore": {"available": ctx.gitignore.available, "showIgnored": ctx.gitignore.show_ignored},
                    "tcc": {"blockedRoots": ctx.blocked_roots.count(ctx.catalog.visible_roots())},
                    "previewExternal": ctx.preview_external})

    def _pair(self, query):
        """POST /v1/pair：本文 {"code", "deviceName"} の符号が合って期限内なら、端末を足してその鍵を返す。
        違う・期限切れ・使用済みは、理由を分けずに 403 pair_failed。回数の上限は本文を読む前に数える（429）。
        符号・鍵・端末の名前は、記録にも例外の文にも出さない。"""
        ctx = self.server.ctx
        if not ctx.pair_limits.allow(self._peer()):
            return self._error(429, "rate_limited")
        body = self._read_body(MAX_PAIR_BODY)
        if body is None:                                  # 断りの応答は、読む中で返した
            return
        try:
            req = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):      # 深すぎる入れ子の本文も 400（500 にしない）
            return self._error(400, "bad_request")
        code = req.get("code") if isinstance(req, dict) else None
        name = req.get("deviceName") if isinstance(req, dict) else None
        if not isinstance(code, str) or not isinstance(name, str):   # 符号を引き換える前に形を確かめる（壊れた本文で使い切らない）
            return self._error(400, "bad_request")
        if not ctx.pairing.redeem(code):
            return self._error(403, "pair_failed")
        device_id, token = ctx.devices.add(name)
        self._json({"deviceId": device_id, "token": token, "apiVersion": API_VERSION})

    def _read_body(self, limit: int) -> bytes | None:
        """要求の本文を limit バイトまで読む。Content-Length が1つの数でない（chunked も）・短く終わったら 400、limit より
        大きければ 413（読み捨てる量には上限）。断った場合は応答を返して None。読む間も header_timeout 秒の締め切りを持つ。"""
        lengths = self.headers.get_all("Content-Length") or []
        n = lengths[0].strip() if len(lengths) == 1 else ""
        if not (n.isascii() and n.isdigit()) or self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "bad_request")
            return None
        n = int(n)
        self._reader.arm(self.server.ctx.header_timeout)
        try:
            if n > limit:
                self.rfile.read(min(n, MAX_DRAIN))
                self._error(413, "too_large")
                return None
            body = self.rfile.read(n)
        finally:
            self._reader.done()
            self.connection.settimeout(self.timeout)
        if len(body) != n:                                # 言った長さに届かないまま閉じた
            self._error(400, "bad_request")
            return None
        return body

    def _sessions(self, query):
        self._json(self.server.ctx.catalog.overview())

    def _transcript(self, bridge, query):
        """TranscriptStore.fetch の結果に bridgeSessionId を足し、state に一覧の status 3項目と会話ログの3項目をまとめる。"""
        s = self._session(bridge)
        if s is None:
            return
        r = self.server.ctx.store.fetch(s["sessionId"], _param(query, "cursor"))
        state = {k: s.get(k) for k in ("status", "waitingFor", "statusUpdatedAt")}
        state.update((k, r[k]) for k in ("lastRecordAt", "pendingCall", "queue"))
        # items の中の questions・answer は store と共有している。書き換えず、そのまま JSON にする
        self._json({"bridgeSessionId": bridge, "sessionId": r["sessionId"], "cursor": r["cursor"],
                    "reset": r["reset"], "items": r["items"], "updates": r["updates"], "state": state})

    def _image(self, bridge, item_id, n, query):
        s = self._session(bridge)
        if s is None:
            return
        got = None
        if n.isascii() and n.isdigit():                   # isdigit だけだと全角の数字も通る
            got = self.server.ctx.store.image(s["sessionId"], item_id, int(n))
        if got is None:
            return self._error(404, "not_found")
        media_type, data = got
        self._bytes(200, media_type if _IMAGE_TYPES.fullmatch(media_type) else "application/octet-stream", data)

    def _written(self, bridge, query):
        """書いたファイル。見せてよいフォルダの中なら rootId・relPath・exists。外は null（外のファイルは見に行かない）。"""
        s = self._session(bridge)
        if s is None:
            return
        ctx = self.server.ctx
        own, roots = _roots_of(ctx.catalog, s)
        rows = [(w, locate(w["path"], s["cwd"], own, roots)) for w in ctx.store.written(s["sessionId"])]
        hidden = self._ignored_rows(ctx, roots, [loc for _, loc in rows])
        files = []
        for i, (w, loc) in enumerate(rows):
            if i in hidden:                                   # git で無視されたものは、見せない起点の外と同じ null
                loc = None
            files.append({"path": w["path"], "rootId": loc["rootId"] if loc else None,
                          "relPath": loc["relPath"] if loc else None, "lastTs": w["lastTs"],
                          "exists": loc["exists"] if loc else None})
        self._json({"bridgeSessionId": bridge, "sessionId": s["sessionId"], "files": files})

    @staticmethod
    def _ignored_rows(ctx: Context, roots: dict, locs: list) -> set:
        """locate の答えの並びのうち、git で無視されているファイルの位置の集まり。あるファイルだけ聞く（無いものは隠すに及ばない）。
        起点ごとにまとめて聞く（フォルダごとに git は1回。覚えている間は呼ばない）。"""
        asks: dict[str, list] = {}
        for i, loc in enumerate(locs):
            if loc is None or loc["exists"] is not True:
                continue
            root = roots.get(loc["rootId"])
            target = resolve_in_root(root, loc["relPath"]) if root is not None else None
            real = rel_below(root, target) if target is not None else None
            if real:
                asks.setdefault(loc["rootId"], []).append((i, real))
        hidden = set()
        for rid, items in asks.items():
            bad = ctx.gitignore.ignored(roots[rid], [real for _, real in items])
            hidden.update(i for i, real in items if real in bad)
        return hidden

    def _resolve(self, bridge, query):
        """Claude の文章の中のパスを (rootId, relPath) に引く。見せてよいフォルダの中の、あるファイルだけ。
        フォルダ・起点そのもの（relPath ""）・無いファイルは 404。"""
        s = self._session(bridge)
        if s is None:
            return
        path = _param(query, "path", "")
        own, roots = _roots_of(self.server.ctx.catalog, s)
        loc = locate(path, s["cwd"], own, roots) if path else None
        root = roots.get(loc["rootId"]) if loc and loc["relPath"] else None
        target = resolve_in_root(root, loc["relPath"]) if root is not None else None
        try:
            found = target is not None and target.is_file()
        except PermissionError:                           # is_file は EACCES を上げる
            return self._blocked(loc["rootId"])
        if not found:                                     # 無い・フォルダ：読めたことにはならない（覚えたまま）
            return self._error(404, "not_found")
        self.server.ctx.blocked_roots.discard(loc["rootId"])   # ファイルとして読めた：前に読めなかったなら、許可された
        if _ignored_by_git(self.server.ctx, root, target):   # git で無視されたものは、無いのと同じ
            return self._error(404, "not_found")
        self._json({"rootId": loc["rootId"], "relPath": loc["relPath"], "preview": loc["preview"], "exists": True})

    def _filekey(self, query):
        """この端末のその起点のファイルの鍵（クッキー bb_file に入れるもの。値は `<端末の id>.<鍵>`）。端末は認証で分かった端末。
        見せない起点・知らない rootId・rootId なしは 404。
        クッキーの path をその起点の下にして、別の起点のファイルへは付かないようにする（アプリが入れる）。"""
        ctx = self.server.ctx
        root_id = _param(query, "rootId", "")
        if not root_id or ctx.catalog.root_path(root_id) is None:
            return self._error(404, "not_found")
        device = self._device
        self._json({"cookie": "bb_file", "value": f"{device}.{file_key(ctx.secret, device, root_id)}", "path": f"/{root_id}/"})

    def _tree(self, root_id, query):
        ctx = self.server.ctx
        root = ctx.catalog.root_path(root_id)   # 知らない・見せない起点は None
        if root is None:
            return self._no_root(root_id)
        rel = _param(query, "path", "")
        if has_denied_name(rel):                          # 名前だけで決める（あるかどうかは見ない）
            return self._error(403, "forbidden", reason="hidden")
        try:
            found = list_dir(root, rel, ignored=ctx.gitignore.ignored)
        except PermissionError:
            return self._blocked(root_id)
        if found is None:
            return self._error(404, "not_found")
        ctx.blocked_roots.discard(root_id)                # 読めた：前に読めなかったなら、許可された
        self._json({"rootId": root_id, **found})

    def _usage(self, query):
        self._json(read_usage(self.server.ctx.usage_path))

    def _events(self, query):
        """SSE。知らせが無いまま ping 秒たてば注釈の行を書き、書けなければ（相手が消えた）枠を返して抜ける。
        枠（4本）が埋まっていれば Hub が一番古い購読を切って入れる。切られた側は get の後に closed を見て
        応答を終える（消えた相手のソケットに ping を書き続けない）。
        書く前に毎回、その端末がまだ組まれているかを確かめる（devices.json の変わり目を見るだけで軽い）。bbctl revoke で
        取り消された端末の開いたままの流れは、次に目覚めたとき（知らせか ping）に何も書かずに終わり、枠を返す。"""
        ctx = self.server.ctx
        device = self._device
        sub = ctx.hub.subscribe(_param(query, "watch"))
        try:
            self._head(200, "text/event-stream", None)
            while not self.server.closing.is_set():
                chunk = sub.get(ctx.ping)
                if sub.closed:                            # 新しい接続に枠を譲った
                    break
                if device is not None and not ctx.devices.exists(device):   # 取り消された（devices.json が読めなくなった場合も）
                    break
                self.wfile.write((chunk or ": ping\n\n").encode("utf-8"))
        except OSError:                                   # 書けない＝相手が消えた（眠った iPad など）
            self.close_connection = True
        finally:
            sub.close()


class _FileHandler(_Handler):
    """ファイル口。/<rootId>/<相対パス> の中身。"""

    port = "files"

    def _cookie_ok(self, ids) -> bool:
        """クッキー bb_file（`<端末の id>.<鍵>`）が、その端末の、URL の起点の鍵と合い、端末が組まれたままか
        （プレビューの WKWebView が読む css・画像は見出しを付けられない）。別の起点の鍵・他の端末の鍵・取り消した端末・
        旧い形は合わない。ids は [rootId, 相対パス]（道が合わなければ空）。"""
        if not ids:
            return False
        ctx = self.server.ctx
        for value in self._cookies("bb_file"):
            device, dot, mac = value.partition(".")
            if not dot or not DEVICE_ID.fullmatch(device) or not _FILE_MAC.fullmatch(mac):
                continue
            want = file_key(ctx.secret, device, ids[0]).encode("ascii")
            if self._same(mac, want) and ctx.devices.exists(device):
                ctx.devices.touch(device)
                self._device = device
                return True
        return False

    def _route(self, raw: str):
        parts = raw.split("/", 2)
        if len(parts) == 3 and not parts[0] and parts[1] and parts[2]:
            return "/*/*", False, self._file, [unquote(parts[1]), unquote(parts[2])], "GET"
        return "?", False, None, [], "GET"

    def _file(self, root_id, rel, query):
        ctx = self.server.ctx
        root = ctx.catalog.root_path(root_id)             # 知らない・見せない起点は None
        if root is None:
            return self._no_root(root_id)
        if has_denied_name(rel):                          # 拒む名前は、名前だけで決める（あるかどうかを見せない）
            return self._error(403, "forbidden", reason="hidden")
        target = resolve_in_root(root, rel)
        if target is None or _ignored_by_git(ctx, root, target):   # 外・リンクの行き先の拒む名前・git で無視されたもの
            return self._error(404, "not_found")
        # FIFO でも待たずに開き、種類は開いたもので見る（stat してから開く間に入れ替わっても待たない）
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        except PermissionError:
            return self._blocked(root_id)
        ctx.blocked_roots.discard(root_id)                # 開けた：前に読めなかったなら、許可された
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):              # フォルダ・FIFO など
                return self._error(404, "not_found")
            if st.st_size > ctx.max_file_bytes:
                return self._error(413, "too_large")
            os.set_blocking(fd, True)
            ctype = content_type(rel.rsplit("/", 1)[-1])
            self._head(200, ctype, st.st_size, ("Content-Security-Policy", csp_for(ctype, ctx.preview_external)))
            left = st.st_size
            while left > 0:
                chunk = os.read(fd, min(CHUNK, left))
                if not chunk:                             # 送る間に短くなった。Content-Length に届かないまま閉じる
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        finally:
            os.close(fd)


class _HeaderReader(io.RawIOBase):
    """要求の行と見出しを読む間だけ、全体で seconds 秒の締め切りを持つ読み手。recv ごとの待ちを残りの時間に縮める。
    done() で締め切りを外すと、あとは socket に今付いている timeout だけが効く。
    締め切りは作った時刻（接続の最初）から数えて1回きり：1接続1要求（HTTP/1.0）が前提で、2つ目の要求には掛からない。"""

    def __init__(self, sock: socket.socket, seconds: float):
        super().__init__()
        self._sock = sock
        self._deadline = time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("header deadline")
            self._sock.settimeout(left)
        return self._sock.recv_into(b)

    def arm(self, seconds: float) -> None:
        """今から seconds 秒の締め切りを掛け直す（要求の本文を読む間）。"""
        self._deadline = time.monotonic() + seconds

    def done(self) -> None:
        self._deadline = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    REFUSED_LOG_EVERY = 10.0                              # 断った接続の記録は、洪水で記録を埋めないよう、これだけ間を空ける（秒）

    def __init__(self, address, handler, ctx: Context):
        self.ctx = ctx
        self.closing = threading.Event()                  # 閉じ始めたら SSE の輪を抜けさせる
        self.allowed_hosts = frozenset(k for k in map(_host_key, ctx.allowed_hosts) if k)
        self._slots = threading.Lock()                    # 下の3つを守る
        self._open = 0                                    # つながっている接続の数（要求の糸が終わるまで数える）
        self._per_ip: dict[str, int] = {}
        self._refused = 0                                 # 最後に記録してから断った数
        self._refused_at = float("-inf")
        super().__init__(address, handler)

    def set_allowed_hosts(self, hosts) -> None:
        """受ける Host の名前を差し替える（MagicDNS の名前が変わった。待ち受け直さずに済む）。属性の置き換え1回なので、
        要求の糸は古い集まりか新しい集まりのどちらかを見る。"""
        self.allowed_hosts = frozenset(k for k in map(_host_key, hosts) if k)

    # ---------------------------------------------------------------- 接続の枠（認証の前の守り）

    def active_connections(self) -> int:
        with self._slots:
            return self._open

    def reserve(self, ip: str) -> str | None:
        """枠を1つ取る。取れれば None、上限なら断る理由（"total"・"per_ip"）。取ったら release で必ず返す。"""
        with self._slots:
            if self._open >= self.ctx.max_conns:
                return "total"
            if self._per_ip.get(ip, 0) >= self.ctx.max_conns_per_ip:
                return "per_ip"
            self._open += 1
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            return None

    def release(self, ip: str) -> None:
        with self._slots:
            self._open -= 1
            left = self._per_ip.get(ip, 0) - 1
            if left > 0:
                self._per_ip[ip] = left
            else:
                self._per_ip.pop(ip, None)

    def _note_refused(self, why: str) -> None:
        """断ったことを記録する。送り元・中身は書かない。洪水の間は REFUSED_LOG_EVERY 秒に1行（その間の数をまとめる）。"""
        with self._slots:
            self._refused += 1
            now = time.monotonic()
            if now - self._refused_at < self.REFUSED_LOG_EVERY:
                return
            n, self._refused, self._refused_at = self._refused, 0, now
        log.info("Refused %d connection(s) over the limit (%s)", n, why)

    def process_request(self, request, client_address):
        ip = client_address[0]
        why = self.reserve(ip)
        if why is not None:
            self._note_refused(why)
            self.shutdown_request(request)                # すぐ閉じる。読まず・返さず、糸も立てない
            return
        try:
            super().process_request(request, client_address)
        except BaseException:                             # 糸が立たなかった。終わりの release をする糸が無いのでここで返す
            self.release(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)   # 要求を処理し、接続を閉じる（失敗も受ける）
        finally:
            self.release(client_address[0])               # 正常・失敗・相手が消えた・SSE が終わった、のどれでも返す

    def server_close(self):
        """待ち受けを閉じる。要求の糸（daemon）は待たない（3.9 の ThreadingMixIn は daemon の糸を数えない）。
        つながったままの SSE の糸も、closing を見て ping の間隔のうちに抜ける。"""
        self.closing.set()
        super().server_close()

    def handle_error(self, request, client_address):
        # 要求の外の失敗（要求の行を読む間に切られた、など）。traceback を標準エラーに出さず、型の名前だけ書く
        log.info("Connection failed: %s", sys.exc_info()[0].__name__)


def make_servers(host: str, api_port: int, file_port: int, ctx: Context) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """(本体, ファイル口) を立てる（まだ待ち受けの輪は回さない）。Tailscale の範囲か loopback でなければ ValueError。"""
    # 名前を引いてから判定する（"0" は 0.0.0.0 になる）。試験用の loopback と Tailscale の範囲だけ許す
    ip = ipaddress.ip_address(socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0])
    if not (ip.is_loopback or ip in TAILSCALE_NET):
        raise ValueError(f"refusing to listen on {host} ({ip}): only a Tailscale IP (100.64.0.0/10) or loopback")
    api = _Server((str(ip), api_port), _ApiHandler, ctx)   # 確かめた IP そのものに結ぶ（名前を2度引かない）
    try:
        files = _Server((str(ip), file_port), _FileHandler, ctx)
    except BaseException:
        api.server_close()
        raise
    return api, files
