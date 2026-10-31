"""使用率（GET /v1/usage の中身）。

Python 3.9 の標準ライブラリだけで動かす。

決まった場所のファイルを読むだけ。書くのは Claude Code のステータスラインのスクリプト（このリポジトリの外）。
書きかけ・壊れたファイルで落ちない（次に読むときには直っている）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def _num(x) -> bool:
    """有限の数か。bool は数に数えない。NaN・Infinity は JSON に出せないので除く。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _window(o) -> dict | None:
    """rate_limits の1つの窓（five_hour / seven_day）。used_percentage が無ければ窓ごと None。"""
    if not isinstance(o, dict) or not _num(o.get("used_percentage")):
        return None
    resets = o.get("resets_at")
    return {"usedPercentage": o["used_percentage"], "resetsAt": resets if _num(resets) else None}


def read_usage(path: Path) -> dict:
    """usage.json（{"capturedAt": Unix 秒, "rate_limits": {...}}）を GET /v1/usage の形にする。

    無い・読めない・壊れている・capturedAt が無ければ {"available": False}。窓が無ければその窓だけ None。
    """
    try:
        o = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):                            # UnicodeDecodeError は ValueError
        return {"available": False}
    if not isinstance(o, dict) or not _num(o.get("capturedAt")):
        return {"available": False}
    limits = o.get("rate_limits")
    if not isinstance(limits, dict):
        limits = {}
    return {"available": True, "capturedAt": o["capturedAt"],
            "fiveHour": _window(limits.get("five_hour")), "sevenDay": _window(limits.get("seven_day"))}
