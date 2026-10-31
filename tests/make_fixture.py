#!/usr/bin/env python3
"""手本（本物の会話ログを短く切り、形はそのまま本文だけを置き換えたもの）を作る道具。

使い方: python3 tests/make_fixture.py --src <jsonl> --lines A-B[,C-D…] --out <jsonl>
Python 3.9 の標準ライブラリだけで動かす。

置き換えの決まり
- 文字列の値は KEEP_KEYS の項目を除いて「文{n}」にする。同じ文字列は同じ n にする（同じパス・同じ本文のつながりを残す）
- 空の文字列は空のまま（enqueue の本文の有無を残す）
- <task-notification> を含む文字列は <task-notification>文{n}</task-notification>
- <command-…>・<local-command-…>・<bash-…>・<agent-message で始まる文字列は、先頭の札の名前だけを残して
  <札>文{n}</札> にする（属性と本文は消す）。一覧サーバーはこの札でコマンドの行・知らせを見分けるので、
  消すと手本で決まりを試せない（/clear の行が指示に見えた）
- base64 の画像の data は 1×1 の PNG。cwd は /fixture/project
- 辞書の鍵が名前の形（英字で始まる英数字・_・-、40字まで）でなければ、鍵も「文{n}」にする。
  AskUserQuestion の答え（toolUseResult.answers）は問いの文が鍵になるため【実測】
- 本物の ID は、形を保ったまま作り物に替える（FakeIds）。UUID（uuid・parentUuid・sessionId など）は f1a7e000-0000-4000-8000-<番号>、
  msg_・toolu_・req_ の ID は同じ長さの FIXTURE<番号>。同じ ID は同じ作り物にする（つながりを残す）
- 行の順・改行は変えない。JSON は Claude Code と同じ詰めた形（区切りに空白なし）で書く。
  一覧サーバーは `"type":"user"` などの文字列で行を読み飛ばすので、形を変えると手本にならない

置き換えた後、元の行の 20 文字以上の文字列（置き換えたものだけ）が、書く行に残っていないかを探す。
残れば何も書かずに止まる。同じ行で KEEP_KEYS の値として残した文字列（uuid など）は探さない。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # リポジトリの直下を足す（直接走らせる道具の決まり）

KEEP_KEYS = frozenset({
    "type", "subtype", "uuid", "parentUuid", "sessionId", "timestamp", "operation", "name", "id",
    "tool_use_id", "source_uuid", "media_type", "role", "kind", "commandMode", "status", "version",
    "isMeta", "isSidechain", "isCompactSummary",
})
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_1x1_B64 = base64.b64encode(PNG_1x1).decode("ascii")
CWD = "/fixture/project"
TASK_MARK = "<task-notification>"
CHECK_LEN = 20
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_TAG_RE = re.compile(r"<((?:command|local-command|bash)-[a-z-]+|agent-message)\b")


FAKE_UUID_PREFIX = "f1a7e000-0000-4000-8000"
FAKE_BODY_MARK = "FIXTURE"
_REAL_ID = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                      r"|(?:msg|toolu|srvtoolu|req)_[0-9A-Za-z]{16,})(?![0-9A-Za-z])")


class FakeIds:
    """行の文字列の中の本物の ID（UUID・msg_・toolu_・srvtoolu_・req_）を、形を保った作り物に替える。同じ ID には同じ作り物を返す。
    作り物は出てきた順の番号から作る（元の ID から計算しない。元の ID は復元も照合もできない）。"""

    def __init__(self):
        self.map: dict[str, str] = {}

    def fake(self, real: str) -> str:
        if real not in self.map:
            n = len(self.map) + 1
            if "-" in real:
                self.map[real] = f"{FAKE_UUID_PREFIX}-{n:012x}"
            else:
                prefix, _, body = real.partition("_")
                self.map[real] = f"{prefix}_{FAKE_BODY_MARK}{str(n).zfill(len(body) - len(FAKE_BODY_MARK))}"
        return self.map[real]

    def apply(self, text: str) -> str:
        return _REAL_ID.sub(lambda m: self.fake(m.group(0)), text)


class LeakError(ValueError):
    """置き換えた後に元の本文が残っていた。本文そのものは載せない（行の番号だけ）。"""


def parse_lines(spec: str) -> list[int]:
    """`A-B[,C-D…]`（1 始まり・両端を含む）を行の番号の昇順の並びにする。重なりは1つにまとめる。"""
    out = set()
    for part in spec.split(","):
        a, sep, b = part.strip().partition("-")
        lo, hi = int(a), int(b if sep else a)
        if lo < 1 or hi < lo:
            raise ValueError(f"行の範囲が正しくない: {part!r}")
        out.update(range(lo, hi + 1))
    return sorted(out)


class Scrubber:
    """1行ずつ置き換える。同じ文字列には同じ「文{n}」を返す（作った手本の中で通し番号）。"""

    def __init__(self):
        self.words: dict[str, str] = {}

    def word(self, s: str) -> str:
        if s not in self.words:
            self.words[s] = f"文{len(self.words) + 1}"
        return self.words[s]

    def _walk(self, v, key, parent, replaced: set, kept: set):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if _NAME_RE.fullmatch(k):
                    nk = k
                else:
                    nk = self.word(k)
                    replaced.add(k)
                out[nk] = self._walk(x, k, v, replaced, kept)
            return out
        if isinstance(v, list):    # 並びの中の値は、並びを持つ項目の値として扱う
            return [self._walk(x, key, parent, replaced, kept) for x in v]
        if not isinstance(v, str):
            return v
        if key in KEEP_KEYS:
            kept.add(v)
            return v
        if v == "":
            return v
        replaced.add(v)
        if key == "cwd":
            return CWD
        if key == "data" and isinstance(parent, dict) and parent.get("type") == "base64":
            return PNG_1x1_B64
        if TASK_MARK in v:
            return f"{TASK_MARK}{self.word(v)}</task-notification>"
        m = _TAG_RE.match(v)
        if m:
            return f"<{m.group(1)}>{self.word(v)}</{m.group(1)}>"
        return self.word(v)

    def row(self, o, lineno: int = 0):
        """1行（JSON に直したもの）を置き換えて返す。元の本文が残っていれば LeakError。"""
        replaced, kept = set(), set()
        out = self._walk(o, None, None, replaced, kept)
        text = dumps(out)
        for s in replaced - kept:
            if len(s) >= CHECK_LEN and json.dumps(s, ensure_ascii=False)[1:-1] in text:
                raise LeakError(f"{lineno} 行目：置き換えた後に元の本文（{len(s)} 文字）が残っている")
        return out


def dumps(o) -> str:
    """Claude Code の会話ログと同じ詰めた JSON（`"type":"user"` の形）。"""
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def make(src: Path, line_numbers: list[int]) -> list[str]:
    """src の指定した行を置き換えた JSON の行（改行なし）を、元の順に返す。"""
    want = set(line_numbers)
    s, ids = Scrubber(), FakeIds()
    out, seen = [], set()
    with open(src, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i not in want:
                continue
            seen.add(i)
            try:
                o = json.loads(line)
            except ValueError:
                raise ValueError(f"{i} 行目が JSON として読めない（置き換えられないので止める）") from None
            out.append(ids.apply(dumps(s.row(o, i))))
    missing = want - seen
    if missing:
        raise ValueError(f"元のファイルに無い行: {min(missing)} 行目から")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会話ログから手本を作る（形を残し、本文を置き換える）")
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--lines", required=True, help="A-B[,C-D…]（1 始まり・両端を含む）")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        lines = make(a.src, parse_lines(a.lines))
    except (OSError, ValueError) as e:     # LeakError も ValueError。何も書かない
        print(f"止めた: {e}", file=sys.stderr)
        return 1
    a.out.write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")
    print(f"{a.out}: {len(lines)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
