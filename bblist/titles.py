"""会話ログの題（一覧のセッション名に使う）。

Claude Code が名前を付けていないセッションの `name` は `フォルダ名-16進2桁` で（nameSource が "derived"）、見分けがつかない。
claude.ai の一覧は会話ログの ai-title を出している【実測 2026-11-04】ので、同じものを使う。

会話ログは大きい（実測で 82MB）ので、末尾だけを読む。大きさと更新時刻が前回と同じなら開き直さない。
題が末尾から流れ出たら、前に読んだ題を使う。文字列として扱えないものは題にしない。
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import unicodedata
from pathlib import Path

TAIL_BYTES = 256 * 1024                 # 実測：ログが見つかった 26 本すべてで、ここに題があった
MAX_TITLE = 200
MISSING_RETRY_SECONDS = 30              # ログが無いセッションを探し直す間（見回りは毎秒なので、毎回は探さない）
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


class LogTitles:
    """sessionId → 題。1つの糸（Catalog の見回り）から呼ぶ。"""

    def __init__(self, projects_dir: Path, clock=time.monotonic):
        self._dir = Path(projects_dir)
        self._clock = clock
        self._seen: dict[str, dict] = {}

    def title(self, session_id) -> str | None:
        """題。無ければ None（例外は出さない）。"""
        if not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
            return None
        rec = self._seen.setdefault(session_id, {"path": None, "stamp": None, "title": None, "retry": None})
        stamp = self._stamp(rec["path"]) if rec["path"] else None
        if stamp is None:
            now = self._clock()
            if rec["retry"] is not None and now < rec["retry"]:
                return rec["title"]
            rec["path"] = self._find(session_id)
            stamp = self._stamp(rec["path"]) if rec["path"] else None
            if stamp is None:
                rec["path"], rec["retry"] = None, now + MISSING_RETRY_SECONDS
                return rec["title"]
        rec["retry"] = None
        if stamp != rec["stamp"]:
            found = _read_title(rec["path"])
            rec["stamp"] = stamp
            if found is not None:
                rec["title"] = found
        return rec["title"]

    def keep(self, session_ids) -> None:
        """いま要るセッションの分だけ残す。"""
        self._seen = {k: v for k, v in self._seen.items() if k in session_ids}

    def _find(self, session_id: str) -> Path | None:
        """2つのフォルダにあれば更新の新しい方（古い控えが残る。TranscriptStore.log_path と同じ）。"""
        found = []
        try:
            for p in self._dir.glob(f"*/{session_id}.jsonl"):
                stamp = self._stamp(p)
                if stamp is not None:
                    found.append((stamp[1], str(p), p))
        except OSError:
            return None
        return max(found, key=lambda t: t[:2])[2] if found else None

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        """通常のファイルの (大きさ, 更新時刻 ns)。リンクはたどらない。"""
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            return None
        return (st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None


def _read_title(path: Path) -> str | None:
    """末尾の中で最後の custom-title、無ければ最後の ai-title。"""
    data = _read_tail(path)
    if data is None:
        return None
    lines = data[0].split(b"\n")
    if data[1]:                         # 途中から読んだので、最初の行は欠けている
        lines = lines[1:]
    ai_title = None
    for raw in reversed(lines):
        if b'"custom-title"' in raw:
            found = _title_of(raw, "custom-title", "customTitle")
            if found:
                return found
        elif ai_title is None and b'"ai-title"' in raw:
            ai_title = _title_of(raw, "ai-title", "aiTitle")
    return ai_title


def _read_tail(path: Path) -> tuple[bytes, bool] | None:
    """(末尾の中身, 途中から読んだか)。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        start = max(0, st.st_size - TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], st.st_size - start
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks), start > 0
    except OSError:
        return None
    finally:
        os.close(fd)


def _title_of(raw: bytes, kind: str, key: str) -> str | None:
    try:
        o = json.loads(raw)
        if not isinstance(o, dict) or o.get("type") != kind or not isinstance(o.get(key), str):
            return None
        title = " ".join(o[key].split())[:MAX_TITLE]
        title.encode("utf-8")           # 孤立サロゲートは、あとで JSON にして返せない
    except ValueError:                  # UnicodeError も ValueError の仲間
        return None
    return unicodedata.normalize("NFC", title) or None
