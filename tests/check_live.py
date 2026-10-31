#!/usr/bin/env python3
"""動いている一覧サーバーの一覧の数と、sessions/*.json から数えた数を、同じ時刻に比べる道具。

使い方: BRIDGEBOARD_TOKEN=<端末の鍵> python3 tests/check_live.py [--host <IP>] [--port 8772] [--claude-dir ~/.claude]
        （端末の鍵は、bbctl pair で組んだ端末のもの。共通のトークンのファイルはもう無い。--token-file <ファイル> でも渡せる）
Python 3.9 の標準ライブラリだけで動かす。

- 一覧サーバーの GET /v1/sessions の sessions の数を取る。端末の鍵は環境変数 BRIDGEBOARD_TOKEN か --token-file の
  ファイルから読んで Authorization: Bearer で送る（画面には出さない）
- sessions/*.json を、一覧サーバー（bblist.sessions.Catalog）と同じ決まりで、Catalog を使わずに数える。
  glob は *.json だけ（隣の *.key は開かない）。pid が整数・kill -0 が通る・procStart が `TZ=UTC ps -o lstart=` と一致・
  bridgeSessionId が空でない・cwd が空でない文字列。同じ bridgeSessionId を持つものは1つに数える（一覧は新しい方だけ出す）
- 「同じ時刻」：ファイルを数える → 一覧を取る → ファイルを数える。前後の数が違えば（その間に増減した）やり直す
- 出すのは数だけ。本文・パス・名前・トークンは出さない。一致すれば終了コード 0、違えば 1、取れなければ 2
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # リポジトリの直下を足す（直接走らせる道具の決まり）

HOME = Path.home()
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TRIES = 5


def tailscale_ip() -> str | None:
    """`tailscale ip -4` の最初の語。PATH に無ければアプリの中の実体を使う。"""
    try:
        r = subprocess.run([shutil.which("tailscale") or TAILSCALE_APP, "ip", "-4"], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.split()
    return out[0] if r.returncode == 0 and out else None


def ps_start(pid: int) -> str:
    """`TZ=UTC ps -o lstart= -p <pid>` の空白を詰めたもの。無ければ ""。月・曜日の綴りは英語（LC_ALL=C）。"""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(r.stdout.split()) if r.returncode == 0 else ""


def alive(pid: int) -> bool:
    """kill -0 が通るか。別のユーザーのプロセス（EPERM）も生きている（procStart の照合で外れる）。"""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def count_files(claude_dir: Path) -> dict:
    """sessions/*.json のうち、起動中で RC のあるものを数える。
    files は当てはまるファイルの数、bridges はそれを bridgeSessionId でまとめた数（一覧に出る数）、
    dupBridges は同じ bridgeSessionId が2本以上ある bridge の数、dupSessions は同じ sessionId が2本以上ある sessionId の数。"""
    bridges: dict[str, int] = {}
    sids: dict[str, int] = {}
    files = 0
    for f in sorted((claude_dir / "sessions").glob("*.json")):
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):                       # 書きかけ・読めない（一覧サーバーも飛ばす）
            continue
        if not isinstance(o, dict):
            continue
        pid, bridge, cwd = o.get("pid"), o.get("bridgeSessionId"), o.get("cwd")
        if (not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 2 ** 31 - 1
                or not isinstance(bridge, str) or not bridge or not isinstance(cwd, str) or not cwd):
            continue
        proc = " ".join(o["procStart"].split()) if isinstance(o.get("procStart"), str) else ""
        if not proc or not alive(pid) or ps_start(pid) != proc:
            continue
        files += 1
        bridges[bridge] = bridges.get(bridge, 0) + 1
        if isinstance(o.get("sessionId"), str):
            sids[o["sessionId"]] = sids.get(o["sessionId"], 0) + 1
    return {"files": files, "bridges": len(bridges),
            "dupBridges": sum(1 for n in bridges.values() if n > 1),
            "dupSessions": sum(1 for n in sids.values() if n > 1)}


def api_count(host: str, port: int, token: str) -> int:
    req = urllib.request.Request(f"http://{host}:{port}/v1/sessions", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return len(json.load(r)["sessions"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一覧サーバーの一覧の数と sessions/*.json の数を比べる")
    ap.add_argument("--host", help="一覧サーバーの IP（既定は tailscale ip -4）")
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--claude-dir", type=Path, default=HOME / ".claude")
    ap.add_argument("--token-file", type=Path, default=None, help="device key file (default: $BRIDGEBOARD_TOKEN)")
    a = ap.parse_args(argv)
    host = a.host or tailscale_ip()
    try:
        token = a.token_file.read_text(encoding="utf-8").strip() if a.token_file else os.environ.get("BRIDGEBOARD_TOKEN", "").strip()
    except OSError:
        token = ""
    if not host or not token:
        print("Tailscale の IP か端末の鍵（BRIDGEBOARD_TOKEN か --token-file）が取れない", file=sys.stderr)
        return 2
    for _ in range(TRIES):
        before = count_files(a.claude_dir)
        try:
            n = api_count(host, a.port, token)
        except (OSError, ValueError, KeyError) as e:        # 本文は出さない。型の名前だけ
            print(f"一覧を取れない: {type(e).__name__}", file=sys.stderr)
            return 2
        after = count_files(a.claude_dir)
        if before == after:
            break
    else:
        print(f"{TRIES} 回とも数える間に sessions/ が変わった", file=sys.stderr)
        return 2
    print(f"一覧サーバー sessions {n}")
    print(f"sessions/*.json 起動中で RC あり {after['files']} 本・bridgeSessionId でまとめて {after['bridges']} 本")
    print(f"同じ bridgeSessionId が2本以上 {after['dupBridges']}・同じ sessionId が2本以上 {after['dupSessions']}")
    ok = n == after["bridges"]
    print("一致" if ok else "不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
