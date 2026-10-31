"""会話ログを増えた分だけ読み、表示の単位を cursor で差分にして渡す（transcript・画像・written の中身）。

Python 3.9 の標準ライブラリだけで動かす。

cursor は [sessionId, inode, 改行まで読み終えたバイト位置] の JSON を、URL で使える base64（= を外す）にしたもの。
アプリは中身を見ない。会話ログはセッションIDで glob して探し、フォルダ名を組み立てない（NFC/NFD が混在している）。
読み方は、最初に作った使い捨ての読み取り器（公開していない）の LogTail を写して直した：
改行まで書かれた行だけを読む・短くなった/入れ替わった（inode）ら最初から、は同じ。直したのは、
開いたファイルの fstat で inode を見る・1行ずつ読み飛ばしを決める・行の先頭の位置を UnitBuilder に渡す、の3つ。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from .units import DRAWN_TYPES, KNOWN_TYPES, UnitBuilder, extract_images

# どれも含まない行は JSON に直さない。最初の読みも書き足した分も、行ごとに同じ決まりで飛ばす。
# 目印は `"type":"user"`・`"assistant"`・`"attachment"`・`"system"`・`"queue-operation"`（units.DRAWN_TYPES。一覧は1か所）。
# Claude Code は今は詰めた形（区切りに空白なし）で書くが、書き方が変わっても（`"type": "user"` など）空にならないよう、
# 区切りの空白は何であってもよい形で探す。詰めた形の行の答えは今までと同じ（正規表現は詰めた形を含む）。速さは、
# 5つの文字列を別々に探す今までより速い（1回の走査で済む）。実測で、表示に使う種類の行に目印の無いものは無かった
_MARK_RE = re.compile(rb'"type"\s*:\s*"(?:' + b"|".join(re.escape(t.encode("ascii")) for t in DRAWN_TYPES) + rb')"')
# 読み飛ばした行の type（診断の unknownTypes）：JSON には直さず、行の一番上の type を文字列として探す（_top_type）。
# 目印の無い行は 2 MB を超えることがあり、JSON に直さないことで速さを保っている
TYPE_NAME = b'"type"'
_TYPE_VALUE = re.compile(rb'\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')    # キーの後ろの `:` と、閉じている文字列の値（空白があってもよい）
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")       # 診断に名前を載せてよい type（合わないものは数だけ）
MAX_UNKNOWN_NAMES = 8
CURSOR_MAX = 256                                  # sessionId 80字＋数2つの JSON を base64 にしても 200 字に届かない
TS_NAME = b'"timestamp"'
_TS_SEP = re.compile(rb'\s*:\s*"')                 # キーの後ろの `:` と値の始まりの引用符（空白があってもよい）
TS_MAX = 40                                       # ISO 8601 の時刻はこれより短い
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_STR_RE = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')    # JSON の文字列（エスケープを含む）

log = logging.getLogger(__name__)


def encode_cursor(session_id: str, ino: int, pos: int) -> str:
    raw = json.dumps([session_id, ino, pos], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(s: str) -> tuple[str, int, int] | None:
    """encode_cursor の逆。形の違うもの・壊れたものは None（cursor なしと同じ扱いにする）。"""
    if not isinstance(s, str) or not s or len(s) > CURSOR_MAX:
        return None
    try:
        v = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    except ValueError:                            # binascii.Error・UnicodeDecodeError・JSONDecodeError を含む
        return None
    if (isinstance(v, list) and len(v) == 3 and isinstance(v[0], str)
            and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in v[1:])):
        return v[0], v[1], v[2]
    return None


def _wanted(line: bytes) -> bool:
    """表示に使う種類の行か（目印がある）。"""
    return _MARK_RE.search(line) is not None


def _top_type(line: bytes) -> str | None:
    """読み飛ばす行の、一番上の type（文字列の値）を JSON に直さずに読む。辞書の一番上に type が無い・値が文字列でなければ None。
    値の中身は見ない（エスケープも生のまま返すので、知っている種類とは合わない）。

    入れ子の中の type（snapshot や content の中）は取らない。深さの数え方は _top_time と同じ：見つけた `"type"` の前を、
    文字列の外の括弧で数える。`"type"` の後ろが `:` と文字列でなければ、値としての文字列なので飛ばす。
    今ある種類の読み飛ばす行は、type が行の頭にある【実測：表示に使わない種類の約 18.3 万行すべて、頭の 1 KiB の中】ので、最初の1つで終わる。
    表示に使う種類の assistant・attachment は type が message の後ろ（数 KiB〜）にある。それらは JSON に直して読むので
    ここは通らない。ただし名前が変わって目印に合わなくなると、ここを通って行の終わりまで探す。
    """
    if line[:1] != b"{" and line[:64].lstrip()[:1] != b"{":     # 辞書の行だけ（行全体の写しは作らない）
        return None
    depth, start = 0, 0
    i = line.find(TYPE_NAME)
    while i >= 0:
        m = _TYPE_VALUE.match(line, i + len(TYPE_NAME))
        if m is not None:
            seg = _STR_RE.sub(b"", line[start:i])
            depth += seg.count(b"{") + seg.count(b"[") - seg.count(b"}") - seg.count(b"]")
            if depth == 1:
                return m.group(1).decode("utf-8", "replace")
            start = i
        i = line.find(TYPE_NAME, i + len(TYPE_NAME))
    return None


def _top_time(line: bytes) -> str | None:
    """読み飛ばす行の一番上の timestamp を、JSON に直さずに探す。無ければ None。

    入れ子の中の timestamp は取らない【実測：file-history-snapshot は一番上に無く snapshot.timestamp だけがあり、
    file-history-delta は一番上の timestamp の前に backup の辞書がある】。見つけた位置の深さを、文字列の外の括弧で数える。
    文字列の中の引用符は \\" になるので、キー `"timestamp"` が文字列の中で見つかることは無い。見つけた `"timestamp"` の後ろが
    `:` と引用符（空白があってもよい）でなければ、値としての文字列なので飛ばす（次の区間で文字列として取り除かれる）。
    """
    depth, start = 0, 0
    i = line.find(TS_NAME)
    while i >= 0:
        m = _TS_SEP.match(line, i + len(TS_NAME))
        if m is not None:
            seg = _STR_RE.sub(b"", line[start:i])
            depth += seg.count(b"{") + seg.count(b"[") - seg.count(b"}") - seg.count(b"]")
            if depth == 1:
                a = m.end()
                j = line.find(b'"', a, a + TS_MAX + 1)   # 閉じる引用符が近くに無い（長すぎる値）なら取らない
                v = line[a:j] if j > a else b""
                return v.decode("ascii") if v and v.isascii() and b"\\" not in v else None
            start = i
        i = line.find(TS_NAME, i + len(TS_NAME))
    return None


class _Log:
    """1本の会話ログの読んだところまでと、そこから作った表示の単位。lock を握って触る。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.path: Path | None = None
        self.ino: int | None = None
        self._restart()

    def _restart(self) -> None:
        self.pos = 0                              # 改行まで読み終えたバイト位置
        self.units = UnitBuilder()
        self.bad_json = 0
        self.unknown_types = 0                    # 知っている種類（units.KNOWN_TYPES）に無い type の行の数（診断。読み飛ばした行も）
        self.unknown_names: list[str] = []        # その type の名前（最大 MAX_UNKNOWN_NAMES 個・重ならない・NAME_RE に合うものだけ）
        self.last_ts: str | None = None           # 最後の行の時刻（読み飛ばした行も含める）

    @property
    def bad(self) -> int:
        """読めなかった行の数（JSON に直せない行と、形の崩れた行）。"""
        return self.bad_json + self.units.bad_rows

    @property
    def unknown_origin_kinds(self) -> int:
        """人の指示かを決める origin.kind が、知っている値でなかった回数（units が数える）。"""
        return self.units.unknown_origin_kinds

    def _note_type(self, t) -> None:
        """行の一番上の type を診断に数える（文字列でなければ、知っている種類なら何もしない）。名前は短い定型のものだけ載せる。"""
        if not isinstance(t, str) or t in KNOWN_TYPES:
            return
        self.unknown_types += 1
        if len(self.unknown_names) < MAX_UNKNOWN_NAMES and t not in self.unknown_names and NAME_RE.fullmatch(t):
            self.unknown_names.append(t)

    def read_new(self, path: Path) -> int | None:
        """増えた分を読んで units に渡し、新たに読めなかった行の数を返す。開けなければ None。
        短くなった・入れ替わった（inode）ら最初から読み直す。"""
        try:
            f = open(path, "rb")
        except OSError:
            return None
        with f:
            st = os.fstat(f.fileno())             # 開いたファイルそのものの inode（stat と open の間の入れ替わりを拾わない）
            if (self.ino is not None and st.st_ino != self.ino) or st.st_size < self.pos:
                self._restart()
            self.path, self.ino = path, st.st_ino
            before = self.bad
            f.seek(self.pos)
            for line in f:
                if not line.endswith(b"\n"):      # 書きかけの最後の行。改行が来るまで読まない
                    break
                off, self.pos = self.pos, self.pos + len(line)
                if not _wanted(line):
                    self.last_ts = _top_time(line) or self.last_ts
                    self._note_type(_top_type(line))
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    self.bad_json += 1
                    continue
                ts = row.get("timestamp") if isinstance(row, dict) else None
                if isinstance(ts, str):
                    self.last_ts = ts
                if isinstance(row, dict):
                    self._note_type(row.get("type"))
                self.units.feed(row, off)
        return self.bad - before


def _empty_state() -> dict:
    return UnitBuilder().state()


class TranscriptStore:
    """セッションごとに会話ログの読んだ位置と表示の単位を覚え、増えた分だけ読む。糸をまたいで呼ばれる。

    覚えるのは max_sessions 本まで。超えたら最も長く使われていないものを捨て、再び求められたら最初から読む。
    セッションの中身はセッションごとの鍵（_Log.lock）で、覚えている一覧は _lock で守る。
    返す items の中の questions・answer は覚えている単位と共有している（写しは浅い）。受け取った側は書き換えない。
    """

    def __init__(self, projects_dir: Path, max_sessions: int = 8):
        self._projects_dir = Path(projects_dir)
        self._max = max_sessions
        self._logs: OrderedDict[str, _Log] = OrderedDict()
        self._lock = threading.Lock()

    def diagnostics(self) -> dict:
        """覚えている会話ログ（max_sessions 本まで）の合計。ファイルは読まない（読んだときに数えた値を足すだけ）。
        - unreadableLines：読めなかった行（JSON に直せない・形の崩れた）
        - unknownTypes：知っている種類（units.KNOWN_TYPES＝描く種類＋描かないと分かっている種類）に無い type の行の数。
          読み飛ばした行も数える（JSON には直さず、行の頭から一番上の type を読む）
        - unknownTypeNames：その type の名前（重ならない・並べ替え・最大 8 個・64 字まで・[A-Za-z0-9_.-] だけ。合わない名前は数だけ）
        - unknownOriginKinds：人の指示かを決める origin.kind が、知っている値の一覧に無い値だった回数
        3つとも普段は 0。0 でないとき Claude Code の会話ログの形が変わった疑い（新しい種類・名前の変更）。
        最初から読み直した会話ログは数え直し、捨てた会話ログの分は消える（累計ではない）。
        糸の鍵は取らない（長い読みの間も待たない）。数はその場の値で、読みの途中なら少し古い。"""
        with self._lock:
            logs = list(self._logs.values())
        names = sorted({n for lg in logs for n in list(lg.unknown_names)})      # list(...) は他の糸が足す途中でも落ちない
        return {"unreadableLines": sum(lg.bad for lg in logs),
                "unknownTypes": sum(lg.unknown_types for lg in logs),
                "unknownTypeNames": names[:MAX_UNKNOWN_NAMES],
                "unknownOriginKinds": sum(lg.unknown_origin_kinds for lg in logs)}

    def log_path(self, session_id: str) -> Path | None:
        """sessionId の会話ログ。ID の形でなければ glob しない。2つのフォルダにあれば更新の新しい方【実測：古い控えが残る】。"""
        if not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
            return None
        found = []
        for p in self._projects_dir.glob(f"*/{session_id}.jsonl"):
            try:
                found.append((p.stat().st_mtime_ns, str(p), p))
            except OSError:
                continue
        return max(found, key=lambda t: t[:2])[2] if found else None

    # ---------------------------------------------------------------- 覚えている一覧

    def _get(self, session_id: str) -> _Log:
        with self._lock:
            lg = self._logs.get(session_id)
            if lg is None:
                lg = self._logs[session_id] = _Log()
                if len(self._logs) > self._max:
                    self._logs.popitem(last=False)
            else:
                self._logs.move_to_end(session_id)
            return lg

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._logs.pop(session_id, None)

    @contextmanager
    def _read(self, session_id: str):
        """増えた分まで読んだ _Log を、その鍵を握ったまま渡す。会話ログが無ければ None（覚えた分も捨てる）。"""
        path = self.log_path(session_id)
        lg = self._get(session_id) if path is not None else None
        if lg is None:
            self._forget(session_id)
            yield None
            return
        with lg.lock:
            bad = lg.read_new(path)
            if bad is None:
                self._forget(session_id)
                yield None
                return
            if bad:                               # 数だけを残す。行の中身は書かない
                log.warning("Unreadable transcript lines: %s: %d new (%d in total)", session_id, bad, lg.bad)
            yield lg

    # ---------------------------------------------------------------- 渡す

    def fetch(self, session_id: str, cursor: str | None) -> dict:
        """transcript の中身（state は平らに3項目）。cursor の sessionId・inode がいまと違う、または位置が読み終えた位置
        （≦ ファイルの大きさ）を超えるときは reset: true で最初から。cursor なし・読めない cursor も最初から（reset: false）。
        会話ログが無ければ items 空で、inode 0・位置 0 の cursor を付ける。"""
        want = decode_cursor(cursor)
        with self._read(session_id) as lg:
            ino, pos = (lg.ino, lg.pos) if lg else (0, 0)
            since, reset = 0, False
            if want is not None:
                if want[0] != session_id or want[1] != ino or want[2] > pos:
                    reset = True
                else:
                    since = want[2]
            out = {"sessionId": session_id, "cursor": encode_cursor(session_id, ino, pos), "reset": reset}
            if lg is None:
                return {**out, "items": [], "updates": [], **_empty_state()}
            # lastRecordAt は読み飛ばした行も含めた最後の行の時刻（API に問い合わせ中も進む）。UnitBuilder のものは読んだ行だけ
            return {**out, "items": lg.units.items_since(since), "updates": lg.units.updates_since(since),
                    **lg.units.state(), "lastRecordAt": lg.last_ts}

    def image(self, session_id: str, item_id: str, n: int) -> tuple[str, bytes] | None:
        """指示に貼られた n 番目の画像の (種類, バイト列)。覚えた行の位置から、その1行だけを読み直す。無ければ None。"""
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return None
        with self._read(session_id) as lg:
            off = lg.units.image_row(item_id) if lg else None
            if off is None:
                return None
            try:
                with open(lg.path, "rb") as f:
                    f.seek(off)
                    row = json.loads(f.readline())
            except (OSError, ValueError):
                return None
        if not isinstance(row, dict):
            return None
        att = row.get("attachment")
        source = att.get("source_uuid") if isinstance(att, dict) else None
        if item_id not in (row.get("uuid"), source):   # 読んだ後に入れ替わったファイルの、別の行の画像を渡さない
            return None
        images = extract_images(row)
        if n >= len(images):
            return None
        media_type, data = images[n]
        try:
            return media_type, base64.b64decode(data)
        except (binascii.Error, ValueError):
            return None

    def written(self, session_id: str) -> list[dict]:
        """そのセッションが Write・Edit・NotebookEdit で書いたファイル（新しい順）。会話ログが無ければ空。"""
        with self._read(session_id) as lg:
            return lg.units.written() if lg else []

    def stat_key(self, session_id: str) -> tuple | None:
        """見張り用の (inode, 大きさ, 更新時刻 ns)。会話ログが無ければ None。読まない・覚えない。"""
        path = self.log_path(session_id)
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_ino, st.st_size, st.st_mtime_ns
