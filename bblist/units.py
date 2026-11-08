"""会話ログの行から表示の単位を作る（transcript の items・updates・state と、written の中身）。

Python 3.9 の標準ライブラリだけで動かす。

行は会話ログの順に1行ずつ feed する。単位はその行の先頭のバイト位置を覚え、items_since(pos) で
「pos より後ろの行から生まれた単位」を返す（cursor で差分を取るため。読むのは transcript.py）。
貼った画像のバイト列は覚えない。指示の行の位置だけを覚え、求められたらその1行を読み直す。
_text_of は、最初に作った使い捨ての読み取り器（公開していない）から写した。その _human_text は、
人の行かの判定（_is_human）と本文（_instruction_text）に分けた。画像だけを貼った指示を本文 "" で拾うため（その読み取り器は数えない）。
"""
from __future__ import annotations

from collections import deque

WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
BRIEF_KEYS = ("command", "file_path", "pattern", "url", "description")
BRIEF_LEN = 60
EARLY_RESULTS_MAX = 256           # 呼び出しより先に来た答えを覚えておく数（呼び出しの来ない答えを覚え続けない）
TASK_MARK = "<task-notification>"
AGENT_MARK = "<agent-message"
COMMAND_MARKS = ("<command-", "<local-command-", "<bash-")

# 知っている形（診断 transcripts.unknownTypes・unknownOriginKinds の「普段は 0」の基準。1か所の定数）。
# 本物の会話ログ（~/.claude/projects/**/*.jsonl、3652 本・84 万行）の type と、user の行の origin.kind を数えて決めた
# （2026-10-31）。Claude Code が新しい種類を足すと、ここに無い名前として数に出る＝
# 形が変わった知らせ。足すときは、表示に使うか（DRAWN）使わないか（UNDRAWN）を決めて、該当の一覧に足す。
DRAWN_TYPES = ("user", "assistant", "attachment", "system", "queue-operation")     # 表示の単位になる種類（読む目印と同じ）
UNDRAWN_TYPES = (                                                                   # 表示に使わないと分かっている種類
    "ai-title", "custom-title", "last-prompt", "agent-name", "mode", "permission-mode", "bridge-session", "atis-latch",
    "api-request", "api-request-blob", "api-request-shape", "cost-state", "tools-used",
    "file-history-snapshot", "file-history-delta", "worktree-state", "relocated", "pr-link", "frame-link",
    "artifact-autoreact-ledger", "artifact-comment-monitor", "started", "result", "launched",
    "teleported-from", "teleport-skipped-branch", "session-owner")
KNOWN_TYPES = frozenset(DRAWN_TYPES + UNDRAWN_TYPES)
KNOWN_ORIGIN_KINDS = frozenset(("human", "peer", "task-notification", "coordinator"))   # user の行の origin.kind


def _text_of(content) -> str | None:
    """指示のテキスト。ツール結果を含む行やテキストの無い行は None。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [p for p in content if isinstance(p, dict)]
    if any(p.get("type") == "tool_result" for p in parts):
        return None
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "\n".join(texts) if texts else None


def _is_human(o: dict, has_origin: bool) -> bool:
    """その行がユーザーの打った行か（本文の有無は見ない）。"""
    if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain") or o.get("isCompactSummary"):
        return False
    return not has_origin or (o.get("origin") or {}).get("kind") == "human"


def _instruction_text(row: dict, blocks) -> str | None:
    """指示の本文（_text_of）。テキストが無く画像だけを貼った行は ""（_text_of は None を返すので、最初の読み取り器は数えない）。
    ツール結果を含む行は None のまま。"""
    text = _text_of(blocks)
    if (text is None and isinstance(blocks, list) and extract_images(row)
            and not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in blocks)):
        return ""
    return text


def extract_images(row: dict) -> list[tuple[str, str]]:
    """指示の行に貼られた画像の (media_type, base64) を順に返す。
    画像は user の message.content と、作業中に送った指示（queued_command）の prompt の両方に入る【実測】。"""
    if row.get("type") == "attachment":
        blocks = (row.get("attachment") or {}).get("prompt")
    elif row.get("type") == "user":
        blocks = (row.get("message") or {}).get("content")
    else:
        return []
    out = []
    for p in blocks if isinstance(blocks, list) else []:
        src = p.get("source") if isinstance(p, dict) and p.get("type") == "image" else None
        if isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str):
            out.append((src.get("media_type") or "application/octet-stream", src["data"]))
    return out


def _b64_len(s: str) -> int:
    """base64 を戻したときのバイト数。Claude Code が書く形（改行なし・= で埋める）を前提に、戻さずに数える。"""
    return len(s) * 3 // 4 - s[-2:].count("=")


def _is_task(content) -> bool:
    """待ち行列の行が知らせ（タスクの終わり・サブエージェント）か。人の指示ではないので数えない。"""
    return isinstance(content, str) and (TASK_MARK in content or content.startswith(AGENT_MARK))


def _public(item: dict) -> dict:
    """内部の位置（_off・_aoff）を外した写し。"""
    return {k: v for k, v in item.items() if not k.startswith("_")}


class UnitBuilder:
    """1本の会話ログの表示の単位・答えの来ていない呼び出し・待ち行列・書いたファイルを、行を読みながら作る。

    単位の _off はその単位が生まれた行の位置、質問の _aoff は答えの行の位置。
    同じ id の単位は最初の1つだけ（作業中に送った指示が後で同じ id で出ても1本）。
    形の崩れた行は飛ばして bad_rows に数える（落ちない）。
    """

    def __init__(self):
        self.bad_rows = 0
        self.unknown_origin_kinds = 0              # 人の指示かを決める origin.kind が、知っている値でなかった回数（診断）
        self._items: list[dict] = []
        self._ids: set = set()
        self._questions: dict[str, dict] = {}      # tool_use の id → 質問の単位
        self._pending: dict[str, dict] = {}        # 答えの来ていない呼び出し（始まった順）
        self._early: dict[str, tuple] = {}         # 呼び出しより先に来た答え：id → (toolUseResult, 本文, 行の位置)（来た順）
        self._queue: deque = deque()               # 待ち行列の (本文, 知らせか)。知らせも並べ、数からは外す
        self._written: dict[str, str] = {}         # path → 最後に書いた時刻（書いた順）
        self._image_rows: dict[str, int] = {}      # 画像のある指示の id → 行の位置
        self._seen_origin = False
        self._last_ts: str | None = None

    # ------------------------------------------------------------ 読む

    def feed(self, row: dict, offset: int) -> None:
        """会話ログの1行を読む。offset はその行の先頭のバイト位置（会話ログの順に増える）。"""
        if not isinstance(row, dict):
            self.bad_rows += 1
            return
        if isinstance(row.get("timestamp"), str):
            self._last_ts = row["timestamp"]
        try:
            self._feed(row, offset)
        except (AttributeError, TypeError, KeyError, ValueError, IndexError):
            self.bad_rows += 1
        if "origin" in row:
            self._seen_origin = True

    def _feed(self, row: dict, off: int) -> None:
        t = row.get("type")
        if t == "user":
            self._user(row, off)
        elif t == "assistant":
            self._assistant(row, off)
        elif t == "attachment":
            self._attachment(row, off)
        elif t == "queue-operation":
            self._queue_op(row)
        elif t == "system" and row.get("subtype") == "api_error":
            self._add(off, {"kind": "error", "id": row.get("uuid"), "ts": row.get("timestamp"), "subtype": "api_error"})

    def _user(self, row: dict, off: int) -> None:
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    self._result(row, p, off)
        if row.get("isCompactSummary"):
            self._add(off, {"kind": "compact", "id": row.get("uuid"), "ts": row.get("timestamp")})
            return
        self._note_origin(row)
        # origin の項目があればそれで決め、無ければ「それまでに origin を持つ行が無い」ときだけ人の指示（古い会話ログ）
        if not _is_human(row, "origin" in row or self._seen_origin):
            return
        text = _instruction_text(row, content)
        # origin の無いコマンドの行は指示にしない。/clear の後の会話ログは、最初の origin の行より前に
        # origin の無い <command-name>/clear… の行がある【実測】。claude.ai も出さない
        if text is None or ("origin" not in row and text.startswith(COMMAND_MARKS)):
            return
        self._instruction(row.get("uuid"), row, text, False, off)

    def _note_origin(self, row: dict) -> None:
        """人の指示かを決める origin.kind（_is_human）が、知っている値でなければ数える。決まりは変えない（数えるだけ）。
        kind を見る行だけ：origin を持つ user の行で、_is_human が先に外す行（isMeta・isSidechain）は除く。
        kind が文字列でない・origin が辞書でないのも、形が変わった印として数える。"""
        if "origin" not in row or row.get("isMeta") or row.get("isSidechain"):
            return
        origin = row["origin"]
        kind = origin.get("kind") if isinstance(origin, dict) else None
        if not (isinstance(kind, str) and kind in KNOWN_ORIGIN_KINDS):
            self.unknown_origin_kinds += 1

    def _attachment(self, row: dict, off: int) -> None:
        att = row.get("attachment")
        if att is None:
            return
        # 作業中に送った指示。直後の queue-operation remove は同じ指示の控えなので数えない
        if att.get("type") == "queued_command" and att.get("commandMode") == "prompt":
            text = _instruction_text(row, att.get("prompt"))
            if text is not None:
                self._instruction(att.get("source_uuid") or row.get("uuid"), row, text, True, off)

    def _assistant(self, row: dict, off: int) -> None:
        content = (row.get("message") or {}).get("content")
        side = bool(row.get("isSidechain"))
        n_text = 0
        for p in content if isinstance(content, list) else []:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and not side:
                uid = row.get("uuid")
                self._add(off, {"kind": "text", "id": uid if n_text == 0 else f"{uid}:{n_text}",
                                "ts": row.get("timestamp"), "markdown": p.get("text", "")})
                n_text += 1
            elif p.get("type") == "tool_use":
                self._tool_use(row, p, off, side)

    def _tool_use(self, row: dict, p: dict, off: int, side: bool) -> None:
        tid, name, inp = p.get("id"), p.get("name"), p.get("input") or {}
        ts = row.get("timestamp")
        brief = next((inp[k] for k in BRIEF_KEYS if isinstance(inp.get(k), str) and inp[k]), "")
        early = self._early.pop(tid, None) if isinstance(tid, str) else None
        if isinstance(tid, str) and early is None:
            self._pending[tid] = {"name": name, "brief": brief[:BRIEF_LEN], "since": ts}
        path = inp.get("notebook_path" if name == "NotebookEdit" else "file_path")   # NotebookEdit に file_path は無い
        if name in WRITE_TOOLS and isinstance(path, str) and path:
            self._written.pop(path, None)          # 書いた順の最後へ回す
            self._written[path] = ts
        if name == "AskUserQuestion" and not side and isinstance(tid, str):
            q = {"kind": "question", "id": tid, "ts": ts, "questions": inp.get("questions"), "answer": None}
            if self._add(off, q):
                self._questions[tid] = q
                if early is not None:
                    self._answer(q, *early)

    def _result(self, row: dict, p: dict, off: int) -> None:
        tid = p.get("tool_use_id")
        q = self._questions.get(tid)
        if isinstance(tid, str) and tid not in self._pending and q is None:
            # 答えの行が呼び出しの行より先に書かれることがある（裏で動かした Agent。2026-10-29 実測）。
            # 後から来る呼び出しを答え待ちにしないように覚えておく
            self._early[tid] = (row.get("toolUseResult"), p.get("content"), off)
            while len(self._early) > EARLY_RESULTS_MAX:
                del self._early[next(iter(self._early))]
            return
        self._pending.pop(tid, None)
        if q is not None:
            self._answer(q, row.get("toolUseResult"), p.get("content"), off)

    @staticmethod
    def _answer(q: dict, tur, content, off: int) -> None:
        # 答えは toolUseResult（辞書）。断られたときなどは文字列なので tool_result の本文にする
        q["answer"] = tur if isinstance(tur, dict) else (_text_of(content) or "")
        q["_aoff"] = off

    def _queue_op(self, row: dict) -> None:
        """待ち行列。知らせの行も並べる（dequeue の行には本文が無く【実測】、どれを外したか分からないため）。
        dequeue は先頭を外す。remove は本文の同じ最初の行を外し、無ければ先頭（Mac から送った指示は enqueue が空）。"""
        op, c = row.get("operation"), row.get("content")
        if op == "enqueue":
            self._queue.append((c, _is_task(c)))
        elif op == "dequeue" and self._queue:
            self._queue.popleft()
        elif op == "remove" and self._queue:
            del self._queue[next((k for k, (x, _) in enumerate(self._queue) if x == c), 0)]

    def _instruction(self, iid, row: dict, text: str, queued: bool, off: int) -> None:
        images = extract_images(row)
        item = {"kind": "instruction", "id": iid, "ts": row.get("timestamp"), "text": text, "queued": queued,
                "images": [{"n": n, "mediaType": mt, "bytes": _b64_len(data)} for n, (mt, data) in enumerate(images)]}
        if self._add(off, item) and images:
            self._image_rows[iid] = off

    def _add(self, off: int, item: dict) -> bool:
        """単位を足す。同じ id がもうあれば足さずに False。"""
        if item["id"] in self._ids:
            return False
        self._ids.add(item["id"])
        item["_off"] = off
        self._items.append(item)
        return True

    # ------------------------------------------------------------ 渡す

    def items_since(self, pos: int) -> list[dict]:
        """行の位置が pos 以上の単位を、会話ログの順に返す。"""
        i = len(self._items)
        while i > 0 and self._items[i - 1]["_off"] >= pos:     # 位置は増える順なので後ろから探す
            i -= 1
        return [_public(it) for it in self._items[i:]]

    def updates_since(self, pos: int) -> list[dict]:
        """pos 以上の行で答えが来た質問のうち、質問の行が pos より前にあるもの（前に渡した単位の変化）。"""
        return [{"kind": "question", "id": q["id"], "answer": q["answer"]} for q in self._questions.values()
                if q["_off"] < pos <= q.get("_aoff", -1)]

    def state(self) -> dict:
        """最後の記録の時刻・答えの来ていない最後の呼び出し・待ち行列。"""
        pending = next(reversed(self._pending.values()), None)
        texts = [c if isinstance(c, str) and c else None for c, task in self._queue if not task]
        return {"lastRecordAt": self._last_ts,
                "pendingCall": dict(pending) if pending else None,
                "queue": {"count": len(texts), "texts": texts}}

    def written(self) -> list[dict]:
        """Write・Edit の file_path と NotebookEdit の notebook_path を新しい順に（同じ path は最後の1つ）。"""
        return [{"path": p, "lastTs": ts} for p, ts in reversed(self._written.items())]

    def image_row(self, item_id: str) -> int | None:
        """画像のある指示の行の位置。画像の無い指示・知らない id は None。"""
        return self._image_rows.get(item_id)
