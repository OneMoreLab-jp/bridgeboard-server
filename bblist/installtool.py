"""Helpers for install.sh. install.sh runs them with the Python it chose, from the repository folder:

    python3 -m bblist.installtool check --support-dir DIR [--own-service-loaded]
    python3 -m bblist.installtool health-url --support-dir DIR
    python3 -m bblist.installtool write-config --support-dir DIR --python PATH [--show-gitignored] [--launcher-root PATH:DEPTH]...
    python3 -m bblist.installtool render TEMPLATE OUT --format xml|sh --set NAME=VALUE...

Python standard library only. Everything is English because Claude Code reads it.

check        One line per item: "ok <item> [detail]", "warn <item> <reason>" or "fail <item> <reason>". Exit 3 if any item failed.
             Items here: tailscale, magicdns, ports, claude-sessions, remote-control (install.sh itself prints clt and python>=3.9
             first). magicdns: Tailscale reports MagicDNS as enabled for the tailnet and a DNS name for this Mac (the app refuses a
             pairing QR code without a host name); "warn ... not checked" when Tailscale itself is not usable.
             It only looks: it reads config.json and ~/.claude/sessions, runs `tailscale` (read-only subcommands), and tries to
             connect to the two ports on this Mac. It writes nothing.
health-url   Prints the URL install.sh polls to see that the new server answers (the server listens on the Tailscale IP only).
write-config Merges config.json: existing values (and unknown keys) are kept, missing keys get their defaults, `python` and
             `tailscale` are set to what install.sh found, and the flags given to install.sh are applied.
             Refuses (exit 1, the file is left as it is) if the existing file cannot be trusted: a link, owned by someone else,
             writable by group or others, too large, or not a JSON object. The server would ignore such a file anyway.
render       Fills @NAME@ placeholders in a template (LaunchAgent plist: XML-escaped; bbctl launcher: shell-quoted).

BRIDGEBOARD_FS_ROOT (default "/") is where the fixed search places (/opt/homebrew/bin, /Applications/Tailscale.app, ...) are
looked for. It exists so that tests can point the search at an empty folder instead of the real file system.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import config as _config
from .config import DEFAULT_API_PORT, DEFAULT_FILE_PORT, load_config
from .discover import backend_state, find_tailscale, ip_of, magicdns_of
from .fsutil import write_atomic
from .sessions import Catalog

FS_ROOT_ENV = "BRIDGEBOARD_FS_ROOT"
DEFAULTS = (("apiPort", DEFAULT_API_PORT), ("filePort", DEFAULT_FILE_PORT), ("claudeDir", "~/.claude"),
            ("showGitIgnored", False), ("previewExternal", True), ("launcherRoots", []), ("tailscale", None), ("python", None))
CONNECT_TIMEOUT = 1.0


def _root() -> str:
    return os.environ.get(FS_ROOT_ENV) or "/"


def _tilde(path, home: Path) -> str:
    text = str(path)
    h = str(home)
    return "~" + text[len(h):] if text == h or text.startswith(h + "/") else text


def _plain(text: str) -> str:
    """Output from other programs is printed with anything odd replaced (one line, no control characters)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "?", text)[:64]


def _in_use(port: int, ips) -> bool:
    for ip in ips:
        try:
            with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- check

def check(support_dir: Path, home: Path, own_service_loaded: bool, out) -> int:
    cfg = load_config(support_dir, home)
    failed = False

    def say(status: str, item: str, text: str = "") -> None:
        nonlocal failed
        failed = failed or status == "fail"
        out.write(f"{status} {item}{' ' + text if text else ''}\n")

    cmd = find_tailscale(cfg.tailscale, home=home, root=_root())
    running = False
    if cmd is None:
        say("fail", "tailscale", 'not found. Install Tailscale (https://tailscale.com/download) and sign in, '
                                 'or set "tailscale" in config.json')
    else:
        state = backend_state(cmd)
        if state == "Running":
            running = True
            say("ok", "tailscale", _tilde(cmd, home))
        elif state is None:
            say("fail", "tailscale", f"{_tilde(cmd, home)} did not answer `status --json`. Is Tailscale running?")
        else:
            say("fail", "tailscale", f"BackendState={_plain(state)} (expected Running). Open Tailscale and sign in")

    if not running:                                        # 失敗を重ねない：Tailscale が使えないと名前も読めない
        say("warn", "magicdns", "not checked: Tailscale is not usable (see the tailscale line)")
    else:
        enabled, dns_name = magicdns_of(cmd)
        fix = "Turn MagicDNS on at https://login.tailscale.com/admin/dns and run this check again"
        if enabled is True and dns_name:
            say("ok", "magicdns", "enabled, and this Mac has a DNS name")
        elif enabled is False:
            say("fail", "magicdns", f"MagicDNS is turned off for this tailnet, so the iPad has no name to reach this Mac by. {fix}")
        elif enabled is None:
            say("fail", "magicdns", "could not read whether MagicDNS is turned on (CurrentTailnet.MagicDNSEnabled is missing "
                                    f"from `tailscale status --json`). {fix}, and update Tailscale if it still fails")
        else:
            say("fail", "magicdns", f"Tailscale reports no DNS name for this Mac (Self.DNSName), so the iPad has no name to reach "
                                    f"it by. {fix}")

    ips = ["127.0.0.1"]
    ts_ip = ip_of(cmd) if cmd else None
    if ts_ip:
        ips.append(ts_ip)
    busy = [p for p in (cfg.api_port, cfg.file_port) if _in_use(p, ips)]
    ports = f"{cfg.api_port} {cfg.file_port}"
    if not busy:
        say("ok", "ports", ports)
    elif own_service_loaded:
        say("ok", "ports", f"{ports} (in use; the installed BridgeBoard LaunchAgent is loaded and will be restarted)")
    else:
        say("fail", "ports", f"{' and '.join(str(p) for p in busy)} already in use by another program. "
                             "Stop it, or set apiPort/filePort in config.json")

    sessions_dir = cfg.claude_dir / "sessions"
    if sessions_dir.is_dir():
        say("ok", "claude-sessions", _tilde(sessions_dir, home))
    else:
        say("fail", "claude-sessions", f"{_tilde(sessions_dir, home)} not found. Run Claude Code once on this Mac")

    count, without = _remote_control_sessions(cfg.claude_dir, home)
    if count is None:
        say("warn", "remote-control", "could not read the sessions folder")
    elif count:
        say("ok", "remote-control", f"{count} live session(s) with a Remote Control id")
    else:
        say("warn", "remote-control", f"0 live sessions with a Remote Control id ({without} live without one). "
                                      "Only sessions started with Remote Control show up on the iPad; start one")
    return 3 if failed else 0


def _remote_control_sessions(claude_dir: Path, home: Path):
    """(live sessions the server would list, live sessions without a Remote Control id). (None, 0) if unreadable.
    Uses the server's own Catalog, so the number is what the iPad would show. Read-only."""
    try:
        catalog = Catalog(claude_dir, (), home, [], secret=bytes(32))
        listed = len(catalog.overview()["sessions"])
        return listed, catalog.diagnostics()["sessions"]["noBridge"]
    except Exception:                                      # noqa: BLE001 a check must report, not crash
        return None, 0


# ---------------------------------------------------------------- health-url

def health_url(support_dir: Path, home: Path) -> str | None:
    cfg = load_config(support_dir, home)
    cmd = find_tailscale(cfg.tailscale, home=home, root=_root())
    ip = ip_of(cmd) if cmd else None
    return None if ip is None else f"http://{ip}:{cfg.api_port}/v1/health"


# ---------------------------------------------------------------- write-config

class ConfigRefused(Exception):
    """config.json exists but cannot be used (the message says why; it never contains the file's content)."""


def _existing(path: Path) -> dict:
    raw, problems = _config._read(path)
    if raw is _config._UNUSABLE:
        raise ConfigRefused(problems[-1] if problems else "cannot be read")
    if not isinstance(raw, dict):
        raise ConfigRefused("not a JSON object")
    return raw


def _roots(specs) -> list:
    roots = []
    for spec in specs:
        path, sep, depth = spec.rpartition(":")
        if not sep or not path or depth not in ("1", "2") or not (path.startswith("/") or path.startswith("~/")):
            raise ValueError(f"--launcher-root must look like /absolute/path:2 or ~/path:1 (depth 1 or 2), got {spec!r}")
        roots.append({"path": path, "depth": int(depth)})
    return roots


def write_config(support_dir: Path, home: Path, python: str, show_git_ignored: bool, launcher_roots) -> Path:
    path = Path(support_dir) / _config.NAME
    merged = _existing(path)
    for key, value in DEFAULTS:
        merged.setdefault(key, value)
    if show_git_ignored:
        merged["showGitIgnored"] = True
    roots = _roots(launcher_roots)
    if roots:
        merged["launcherRoots"] = roots
    merged["python"] = python
    found = find_tailscale(load_config(support_dir, home).tailscale, home=home, root=_root())
    if found:
        merged["tailscale"] = found
    write_atomic(path, (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


# ---------------------------------------------------------------- render

def render(template: Path, out: Path, fmt: str, values: dict) -> None:
    quote = escape if fmt == "xml" else shlex.quote
    text = Path(template).read_text(encoding="utf-8")
    for name, value in values.items():
        text = text.replace(f"@{name}@", quote(value))
    left = sorted(set(re.findall(r"@[A-Z][A-Z_]*@", text)))
    if left:
        raise ValueError(f"placeholders not filled: {', '.join(left)}")
    Path(out).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- command line

def _default_support() -> Path:
    return Path.home() / "Library" / "Application Support" / "BridgeBoard"


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="python3 -m bblist.installtool", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--support-dir", type=Path, default=_default_support())
    c.add_argument("--own-service-loaded", action="store_true")
    h = sub.add_parser("health-url")
    h.add_argument("--support-dir", type=Path, default=_default_support())
    w = sub.add_parser("write-config")
    w.add_argument("--support-dir", type=Path, default=_default_support())
    w.add_argument("--python", required=True)
    w.add_argument("--show-gitignored", action="store_true")
    w.add_argument("--launcher-root", action="append", default=[])
    r = sub.add_parser("render")
    r.add_argument("template", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--format", choices=("xml", "sh"), required=True)
    r.add_argument("--set", dest="values", action="append", default=[], metavar="NAME=VALUE")
    a = p.parse_args(argv)
    home = Path.home()
    if a.cmd == "check":
        return check(a.support_dir, home, a.own_service_loaded, out)
    if a.cmd == "health-url":
        url = health_url(a.support_dir, home)
        if url is None:
            print("error: could not determine the Tailscale IPv4 address. Is Tailscale running?", file=sys.stderr)
            return 1
        print(url, file=out)
        return 0
    try:
        if a.cmd == "write-config":
            write_config(a.support_dir, home, a.python, a.show_gitignored, a.launcher_root)
        else:
            render(a.template, a.out, a.format, dict(v.split("=", 1) for v in a.values))
    except ConfigRefused as e:
        print(f"error: config.json cannot be used ({e}). Fix or remove it. The server files, config.json and the "
              "LaunchAgent were not changed.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {type(e).__name__}: {e}" if isinstance(e, ValueError) else f"error: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
