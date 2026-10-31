"""端末ごとの鍵の置き場（devices.json）。

Python 3.9 の標準ライブラリだけで動かす。

devices.json は `[{"id": 16桁の16進, "name": 端末の名前, "hash": sha256(鍵) の16進, "createdAt": ミリ秒, "lastSeenAt": ミリ秒|null}]`
で、置き場（権限 700）の中に権限 600 で置く。鍵そのものは作った瞬間に1度だけ返し、どこにも残さない（ハッシュだけ）。
鍵は端末ごと（secrets.token_urlsafe(32)＝256 ビット）。1台を取り消しても、ほかの端末は影響を受けない。

書く側は2つある：動いているサーバー（組む口の add・lastSeenAt）と、別のプロセスの bbctl（devices・revoke）。
そこで、書くときは置き場を排他（flock）で使い、中で読み直してから一時ファイル経由で置き換える（互いの更新を踏まない）。
読む側（verify など）は、ファイルの変わり目（inode・更新時刻・大きさ）が変わったときだけ読み直す。
だから bbctl revoke は、サーバーを再起動しなくてもすぐ効く。
鍵・ハッシュ・名前は、標準出力・記録・例外の文に出さない。
壊れた devices.json（読めない・形が違う・リンク）は、誰も通さない。add・revoke は上書きせずに例外を上げる（手で直すか消す）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from pathlib import Path

from .fsutil import exclusive, write_atomic

log = logging.getLogger(__name__)

NAME = "devices.json"
LAST_SEEN_EVERY = 60.0                                    # lastSeenAt を書き直す間隔（秒）。端末ごと
MAX_NAME = 64
DEFAULT_NAME = "Unnamed device"
MAX_FILE_BYTES = 1024 * 1024                              # これより大きい devices.json は壊れているとみなす
DEVICE_ID = re.compile(r"[0-9a-f]{16}")                   # 端末の id（16桁の小文字の16進）。確かめる正規表現はここだけ
_HASH = re.compile(r"[0-9a-f]{64}")
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})   # 制御・書式（向きを変える文字など）・行の区切り


def clean_name(name) -> str:
    """端末の名前を、画面や端末に出して安全な形にする：制御文字・書式の文字・行の区切りを除き、前後の空白を除き、
    MAX_NAME 字までにする。文字列でない・空になるなら DEFAULT_NAME。名前は組んだ端末（符号を持つ者）が決めるので、
    bbctl devices が端末に出しても、エスケープ列などで画面を書き換えられないようにする。"""
    if not isinstance(name, str):
        return DEFAULT_NAME
    kept = "".join(ch for ch in name if unicodedata.category(ch) not in _DROPPED).strip()
    return kept[:MAX_NAME].strip() or DEFAULT_NAME


def _valid(row) -> bool:
    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)
    return (isinstance(row, dict)
            and isinstance(row.get("id"), str) and DEVICE_ID.fullmatch(row["id"]) is not None
            and isinstance(row.get("name"), str)
            and isinstance(row.get("hash"), str) and _HASH.fullmatch(row["hash"]) is not None
            and is_int(row.get("createdAt")) and (row.get("lastSeenAt") is None or is_int(row["lastSeenAt"])))


class DeviceStore:
    """devices.json の読み書き。wall は壁の時計（秒。createdAt・lastSeenAt に書く）、mono は単調な時計（このプロセスの中の
    lastSeenAt の間引きだけに使う）。"""

    def __init__(self, support_dir: Path, wall=time.time, mono=time.monotonic):
        self.dir = Path(support_dir)
        self.path = self.dir / NAME
        self._wall, self._mono = wall, mono
        self._mu = threading.Lock()                       # 下の3つを守る
        self._cache = None                                # (ファイルの変わり目, 行の並び|None＝読めない)
        self._touched: dict[str, float] = {}              # 端末の id → lastSeenAt を最後に書こうとした単調な時刻
        self._warned = None                               # 読めないと記録した変わり目（同じ状態で記録を埋めない）

    # ---------------------------------------------------------------- 読む

    def _signature(self):
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode)

    def _read_file(self) -> list:
        """devices.json の行の並び。無ければ []。リンク・普通でないファイルは OSError、中身が読めない・形が違えば ValueError。
        緩い権限は 600 に締める。"""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)    # リンクは ELOOP。FIFO でも待たない
        except FileNotFoundError:
            return []
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{NAME} is not a regular file")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            data = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{NAME} is too large")
        try:
            rows = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):     # 深すぎる入れ子も壊れたファイル
            raise ValueError(f"{NAME} is not valid JSON") from None
        if not isinstance(rows, list) or not all(_valid(r) for r in rows):
            raise ValueError(f"{NAME} has an unexpected shape")
        return rows

    def _snapshot(self):
        """今の行の並び（変わり目が同じなら覚えているもの）。読めなければ None（誰も通さない）。"""
        sig = self._signature()
        with self._mu:
            if self._cache is not None and self._cache[0] == sig:
                return self._cache[1]
        try:
            rows = self._read_file()
        except (OSError, ValueError) as e:
            rows = None
            with self._mu:
                warn = self._warned != sig
                self._warned = sig
            if warn:                                      # 型の名前だけ。中身は書かない
                log.warning("%s cannot be used (%s). Nobody can sign in until it is fixed or removed", NAME, type(e).__name__)
        with self._mu:
            self._cache = (sig, rows)                     # 読む前の変わり目で覚える（読む間に変わっていれば、次の呼び出しで読み直す）
        return rows

    def list(self, strict: bool = False) -> list:
        """端末の一覧（id・name・createdAt・lastSeenAt）。ハッシュは含めない。読めなければ []（strict なら OSError・ValueError。
        bbctl が「端末が無い」と「devices.json が壊れている」を見分けるのに使う）。"""
        rows = self._read_file() if strict else (self._snapshot() or [])
        return [{k: r[k] for k in ("id", "name", "createdAt", "lastSeenAt")} for r in rows]

    def exists(self, device_id) -> bool:
        """その id の端末が組まれたままか（取り消していないか）。"""
        return isinstance(device_id, str) and any(r["id"] == device_id for r in self._snapshot() or [])

    def verify(self, token) -> str | None:
        """鍵に合う端末の id。合わなければ None。全件を hmac.compare_digest で比べる（当たった時点で止めない）。"""
        if not isinstance(token, str) or not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        found = None
        for row in self._snapshot() or []:
            if hmac.compare_digest(digest, row["hash"]):
                found = row["id"]
        if found is not None:
            self.touch(found)
        return found

    def touch(self, device_id) -> None:
        """端末を使った印（lastSeenAt）。端末ごとに LAST_SEEN_EVERY 秒に1回だけ書く。書けなくても認証は止めない。"""
        if not self.exists(device_id):
            return
        now = self._mono()
        with self._mu:
            last = self._touched.get(device_id)
            if last is not None and now - last < LAST_SEEN_EVERY:
                return
            self._touched[device_id] = now                # 書けなくても間を空ける（失敗で毎回書きに行かない）
        try:
            with exclusive(self.dir):
                rows = self._read_file()                  # 読み直す（取り消された端末を書き戻さない）
                row = next((r for r in rows if r["id"] == device_id), None)
                if row is None:
                    return
                row["lastSeenAt"] = int(self._wall() * 1000)
                self._write(rows)
        except (OSError, ValueError) as e:
            log.warning("Could not record when a device was last seen (%s)", type(e).__name__)

    # ---------------------------------------------------------------- 書く

    def _write(self, rows: list) -> None:
        write_atomic(self.path, json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"))

    def add(self, name) -> tuple[str, str]:
        """端末を足す。(id, 鍵) を返す。鍵はここで1度だけ見える。壊れた devices.json があれば ValueError（上書きしない）。"""
        name = clean_name(name)
        token = secrets.token_urlsafe(32)
        with exclusive(self.dir):
            rows = self._read_file()
            taken = {r["id"] for r in rows}
            device_id = secrets.token_hex(8)
            while device_id in taken:
                device_id = secrets.token_hex(8)
            rows.append({"id": device_id, "name": name, "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                         "createdAt": int(self._wall() * 1000), "lastSeenAt": None})
            self._write(rows)
        return device_id, token

    def revoke(self, device_id) -> bool:
        """端末を取り消す。あれば True。壊れた devices.json があれば ValueError（上書きしない）。"""
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            return False
        with exclusive(self.dir):
            rows = self._read_file()
            kept = [r for r in rows if r["id"] != device_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
        with self._mu:
            self._touched.pop(device_id, None)
        return True
