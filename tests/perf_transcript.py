#!/usr/bin/env python3
"""大きな会話ログを最初から読む秒数を測る道具。

使い方: python3 tests/perf_transcript.py [--log <path>] [--mb 140]
Python 3.9 の標準ライブラリだけで動かす。

--log を付けなければ、手本（fixtures/*.jsonl）を繰り返して tmp に --mb MB の会話ログを作る。
繰り返すたびに、すべての uuid を互いに違うまま k で区別する（手本の作り物の uuid は先頭24字が同じなので、
末尾を k 一色にすると全部が同じ id になり、単位が1つにまとまって軽く測れてしまう）。本物と同じく単位が増え続ける。
TranscriptStore.fetch(sid, None) の秒数を出し、5秒を超えたら終了コード 1。
出すのは大きさ・秒数・数だけ。会話の本文は出さない。--log の会話ログには何も書かない。
"""
from __future__ import annotations

import argparse
import os
import re
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # リポジトリの直下を足す（直接走らせる道具の決まり）

from bblist.transcript import TranscriptStore  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
LIMIT = 5.0
SID = "perf0000-0000-4000-8000-000000000000"
_UUID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def make_log(path: Path, mb: float) -> int:
    """手本を繰り返して mb MB 以上の会話ログを書き、その大きさを返す。k 回目は、各 uuid の末尾 12 桁に k を混ぜる
    （元の末尾を残したまま、k ごとに別の値にする。k が違えば、元の uuid が違うものどうしも、同じものどうしも別になる）。"""
    src = b"".join(p.read_bytes() for p in sorted(FIX.glob("*.jsonl")))
    target, size, k = int(mb * 1_000_000), 0, 0
    with open(path, "wb") as f:
        while size < target:
            chunk = _UUID_RE.sub(lambda m, k=k: m.group(0)[:24] + b"%012x" % ((int(m.group(0)[24:], 16) + (k << 20)) & 0xFFFFFFFFFFFF), src)
            f.write(chunk)
            size += len(chunk)
            k += 1
    return size


def measure(projects: Path, sid: str, size: int) -> int:
    store = TranscriptStore(projects)
    t0 = time.perf_counter()
    r = store.fetch(sid, None)
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    store.fetch(sid, r["cursor"])
    again = time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss      # macOS はバイト
    print(f"会話ログ {size / 1_000_000:.1f} MB")
    print(f"最初の読み {first:.2f} 秒（上限 {LIMIT} 秒）  items {len(r['items'])}")
    print(f"cursor での読み直し {again * 1000:.1f} ms")
    print(f"最大メモリ {rss / 1_000_000:.0f} MB")
    return 1 if first > LIMIT else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大きな会話ログを最初から読む秒数を測る")
    ap.add_argument("--log", type=Path, help="測る会話ログ（projects/<フォルダ>/<sessionId>.jsonl）。無ければ tmp に作る")
    ap.add_argument("--mb", type=float, default=140, help="作る会話ログの大きさ（MB）")
    args = ap.parse_args(argv)
    if args.log is not None:
        log = args.log.expanduser()
        projects, sid = log.parent.parent, log.stem
        found = TranscriptStore(projects).log_path(sid)
        if found is None or not os.path.samefile(found, log):
            print("その会話ログは sessionId で引けない（名前が ID の形でないか、同じ ID の新しいログが別にある）", file=sys.stderr)
            return 2
        return measure(projects, sid, log.stat().st_size)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "projects" / "-perf"
        folder.mkdir(parents=True)
        size = make_log(folder / f"{SID}.jsonl", args.mb)
        return measure(folder.parent, SID, size)


if __name__ == "__main__":
    sys.exit(main())
