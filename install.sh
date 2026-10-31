#!/bin/bash
# Installs the BridgeBoard server as a per-user LaunchAgent (it starts at login and is restarted if it dies).
#
#   ./install.sh --check
#       Looks at this Mac and prints one line per item, "ok <item>", "warn <item> <reason>" or "fail <item> <reason>".
#       Items: clt, python>=3.9, tailscale, magicdns, ports, claude-sessions, remote-control. Changes nothing.
#       Exit 3 if any item is "fail"; "warn" does not fail.
#   ./install.sh [--show-gitignored] [--launcher-root <path>:<depth>]...
#       Copies this version to "~/Library/Application Support/BridgeBoard/versions/<version>/", points "current" at it,
#       writes config.json (values that are already there are kept), writes and loads the LaunchAgent, and waits up to
#       15 seconds for the new server to answer /v1/health. If it does not, the last lines of its log are printed.
#       --show-gitignored        also show files that git ignores (config.json: showGitIgnored)
#       --launcher-root P:D      a folder whose sub-folders (D=1) or sub-sub-folders (D=2) are listed as projects
#                                (config.json: launcherRoots). Repeatable. Without it, the projects come from the
#                                folders Claude Code has been used in.
#   Run it again to upgrade or repair: it replaces the loaded server. The cloned folder is not needed afterwards.
#   To remove everything: "~/Library/Application Support/BridgeBoard/current/uninstall.sh" (add --purge to delete the data too).
#
# This script creates no secret and prints none. The server creates its own secret when it starts, and the keys of paired
# devices are made later by `bbctl pair`.
#
# Exit codes: 0 done · 1 something failed · 3 a prerequisite is missing (--check lists them) · 4 the new server did not
# answer in time · 64 wrong arguments
#
# Only tools that ship with macOS are used (bash 3.2, sed, curl, plutil, launchctl, mktemp). The Python helper
# (bblist/installtool.py) runs with the Python found below. Environment variables for tests: BRIDGEBOARD_FS_ROOT (where the
# fixed search places such as /opt/homebrew are looked for), BRIDGEBOARD_HEALTH_WAIT (seconds to wait for the new server,
# default 15) and BRIDGEBOARD_STOP_WAIT (seconds to wait for the old server to stop, default 15).
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1                # Python must not write .pyc caches (Apple's Python would put them in ~/Library/Caches)

LABEL="jp.onemorelab.bridgeboard.server"
REPO="$(cd "$(dirname "$0")" && pwd -P)"
FS_ROOT="${BRIDGEBOARD_FS_ROOT:-}"
HEALTH_WAIT="${BRIDGEBOARD_HEALTH_WAIT:-15}"
STOP_WAIT="${BRIDGEBOARD_STOP_WAIT:-15}"
SUPPORT="$HOME/Library/Application Support/BridgeBoard"
LOGS="$HOME/Library/Logs/BridgeBoard"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
CONFIG="$SUPPORT/config.json"
DOMAIN="gui/$(id -u)"

STAGE=""        # temporary files and folders (made with mktemp; removed when this script ends, whatever the reason)
LINK_TMP=""
PLIST_TMP=""
cleanup() {
  local f
  for f in "$STAGE" "$LINK_TMP" "$PLIST_TMP"; do
    if [ -n "$f" ]; then rm -rf "$f"; fi
  done
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  echo "usage: $0 --check" >&2
  echo "       $0 [--show-gitignored] [--launcher-root <path>:<depth>]..." >&2
  exit "${1:-64}"
}

mode=install
show_gitignored=0
roots=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --show-gitignored) show_gitignored=1 ;;
    --launcher-root)
      shift
      [ "$#" -gt 0 ] || usage
      roots+=("$1")
      ;;
    -h | --help) usage 0 ;;
    *) usage ;;
  esac
  shift
done
if [ "$mode" = check ] && { [ "$show_gitignored" = 1 ] || [ "${#roots[@]}" -gt 0 ]; }; then
  usage
fi
for root in ${roots[@]+"${roots[@]}"}; do
  case "$root" in
    /*:1 | /*:2 | "~/"*:1 | "~/"*:2) ;;
    *)
      echo "error: --launcher-root must look like /absolute/path:2 or ~/path:1 (depth 1 or 2): $root" >&2
      exit 64
      ;;
  esac
done

# ---------------------------------------------------------------- finding things

clt_ok() { xcode-select -p > /dev/null 2>&1; }

# $1: a path that is Python 3.9 or newer AND will run under launchd. launchd gives the server only PATH=/usr/bin:/bin:/usr/sbin:/sbin
# (see the plist), so the candidate is run with an empty environment plus that PATH: a shim that needs the shell's PATH (pyenv
# and the like) fails here. A virtual environment (sys.prefix differs from sys.base_prefix) is skipped too: it belongs to the
# shell that activated it, and it can be deleted later.
py_ok() {
  [ -n "$1" ] && [ -x "$1" ] \
    && /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin "$1" \
      -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) and sys.prefix == sys.base_prefix else 1)' \
      < /dev/null > /dev/null 2>&1
}

config_trusted() {   # the same rule as the server: a regular file that is mine and not writable by group or others
  [ -f "$CONFIG" ] && [ ! -L "$CONFIG" ] && [ "$(stat -f %u "$CONFIG")" = "$(id -u)" ] \
    && [ $((0$(stat -f %Lp "$CONFIG") & 022)) -eq 0 ]
}

# Prints the first usable Python 3.9+ (see py_ok): "python" in config.json, PATH, /opt/homebrew/bin, /usr/local/bin, /usr/bin.
# A "python" in config.json that fails py_ok is not used (install.sh then writes the one it found back into config.json).
# /usr/bin/python3 on a Mac without the Command Line Tools is only a stub that opens an installer window, so it is not run then.
find_python() {
  local configured="" cand
  if config_trusted; then
    configured="$(/usr/bin/plutil -extract python raw -o - "$CONFIG" 2> /dev/null || true)"
    case "$configured" in "~/"*) configured="$HOME/${configured#\~/}" ;; esac
  fi
  for cand in "$configured" "$(command -v python3 2> /dev/null || true)" "$FS_ROOT/opt/homebrew/bin/python3" \
    "$FS_ROOT/usr/local/bin/python3" "$FS_ROOT/usr/bin/python3"; do
    [ -n "$cand" ] || continue
    case "$cand" in
      /usr/bin/python3 | "$FS_ROOT"/usr/bin/python3) clt_ok || continue ;;
    esac
    if py_ok "$cand"; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

# Runs bblist/installtool.py from the repository folder with the Python found above ($PY).
run_tool() {
  (cd "$REPO" && env -u PYTHONPATH -u PYTHONHOME "$PY" -m bblist.installtool "$@")
}

# ---------------------------------------------------------------- --check

if [ "$mode" = check ]; then
  failed=0
  if clt_ok; then
    echo "ok clt"
  else
    echo "fail clt the Command Line Tools are not installed. Run: xcode-select --install (and wait for it to finish)"
    failed=1
  fi
  PY=""
  if PY="$(find_python)"; then
    echo "ok python>=3.9 $PY"
  else
    echo "fail python>=3.9 no Python 3.9 or newer that runs under launchd found (looked at config.json, PATH, /opt/homebrew/bin, /usr/local/bin and /usr/bin/python3; virtual environments and programs that need more than PATH=/usr/bin:/bin:/usr/sbin:/sbin are skipped)"
    failed=1
  fi
  if [ -n "$PY" ]; then
    own=()
    if launchctl print "$DOMAIN/$LABEL" > /dev/null 2>&1; then own=(--own-service-loaded); fi
    run_tool check --support-dir "$SUPPORT" ${own[@]+"${own[@]}"} || failed=1
  else
    for item in tailscale magicdns ports claude-sessions remote-control; do
      echo "fail $item not checked: it needs Python 3.9 or newer"
    done
  fi
  if [ "$failed" != 0 ]; then exit 3; fi
  exit 0
fi

# ---------------------------------------------------------------- install

if [ ! -f "$REPO/listserver.py" ] || [ ! -f "$REPO/bbctl.in" ] || [ ! -f "$REPO/$LABEL.plist.in" ] \
  || [ ! -d "$REPO/bblist" ] || [ ! -d "$REPO/vendor" ]; then
  echo "error: this does not look like a BridgeBoard server folder: $REPO" >&2
  exit 1
fi
VERSION="$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$REPO/bblist/__init__.py")"
case "$VERSION" in
  '' | .* | -* | *[!0-9A-Za-z._+-]*)
    echo "error: cannot read the version from bblist/__init__.py" >&2
    exit 1
    ;;
esac

PY=""
if ! PY="$(find_python)"; then
  echo "error: no Python 3.9 or newer that runs under launchd found. Run: $0 --check" >&2
  exit 3
fi
if [ -e "$SUPPORT/current" ] && [ ! -L "$SUPPORT/current" ]; then
  echo "error: $SUPPORT/current exists and is not a link. Move it away and run this again." >&2
  exit 1
fi

umask 077                                       # everything made here is readable by me only
mkdir -p "$SUPPORT/versions" "$LOGS" "$AGENTS"  # the log folder must exist before launchd opens launchd.out and launchd.err
chmod 700 "$SUPPORT" "$LOGS"

# 1. Stage the new version next to its final place (same disk, so the final move is one rename).
STAGE="$(mktemp -d "$SUPPORT/.stage.XXXXXX")"
cp "$REPO/listserver.py" "$REPO/bbctl.py" "$REPO/uninstall.sh" "$REPO/LICENSE" "$STAGE/"
cp -R "$REPO/bblist" "$REPO/vendor" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
run_tool render "$REPO/bbctl.in" "$STAGE/bbctl" --format sh --set PYTHON="$PY"
chmod -R go-rwx "$STAGE"
chmod 700 "$STAGE/bbctl" "$STAGE/uninstall.sh" "$STAGE/listserver.py" "$STAGE/bbctl.py"

# 2. config.json: keeps what is there; refuses (and stops) if the existing file cannot be trusted.
args=(--support-dir "$SUPPORT" --python "$PY")
if [ "$show_gitignored" = 1 ]; then args+=(--show-gitignored); fi
for root in ${roots[@]+"${roots[@]}"}; do args+=(--launcher-root "$root"); done
run_tool write-config "${args[@]}"

# 3. The LaunchAgent file (placeholders are filled XML-escaped, so a "&" in a folder name cannot break it).
PLIST_TMP="$(mktemp "$AGENTS/.$LABEL.XXXXXX")"
run_tool render "$REPO/$LABEL.plist.in" "$PLIST_TMP" --format xml --set PYTHON="$PY" --set SUPPORT="$SUPPORT" --set HOME="$HOME"
plutil -lint "$PLIST_TMP" > /dev/null
chmod 644 "$PLIST_TMP"                          # nothing secret in it; the same mode as other LaunchAgents

# 4. Stop the old server, swap the files in, and point "current" at them.
LOG="$LOGS/listserver.log"
# Remember which log file this is and how long it is now: only what is written after this point counts (the log is rotated
# at 1 MB, so counting lines before and after would break when the file is replaced).
log_inode="$(stat -f %i "$LOG" 2> /dev/null || echo 0)"
log_size="$(stat -f %z "$LOG" 2> /dev/null || echo 0)"
launchctl bootout "$DOMAIN/$LABEL" 2> /dev/null || true
stop_deadline=$((SECONDS + STOP_WAIT))          # let the old server finish stopping, so that it cannot hold the ports
while pgrep -f -- "$SUPPORT/current/listserver.py" > /dev/null 2>&1 && [ "$SECONDS" -lt "$stop_deadline" ]; do
  sleep 1
done
if pgrep -f -- "$SUPPORT/current/listserver.py" > /dev/null 2>&1; then
  echo "error: the old server did not stop within ${STOP_WAIT} seconds, so its files were not replaced." >&2
  echo "Stop it (for example: launchctl bootout $DOMAIN/$LABEL, or quit the process) and run this script again." >&2
  exit 1
fi
rm -rf "$SUPPORT/versions/$VERSION"
mv "$STAGE" "$SUPPORT/versions/$VERSION"
STAGE=""
LINK_TMP="$SUPPORT/.current.$$"
ln -s "versions/$VERSION" "$LINK_TMP"
mv -f -h "$LINK_TMP" "$SUPPORT/current"         # -h: replace the link itself, not the folder it points at
LINK_TMP=""
mv -f "$PLIST_TMP" "$PLIST"
PLIST_TMP=""

# 5. Load it. Right after bootout, bootstrap is sometimes refused while launchd is still tidying up, so try a few times.
loaded=0
for _ in 1 2 3 4 5; do
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2> /dev/null; then
    loaded=1
    break
  fi
  sleep 1
done
if [ "$loaded" != 1 ]; then
  if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then   # the last try shows the error
    echo "error: launchctl could not load $PLIST (see the message above)." >&2
    exit 1
  fi
fi

# 6. Wait until the new server answers. Answering is not enough: an older server on the same port would answer too,
#    so the new server must also have logged that it is listening.
# What was written to the log since the old server was stopped. If the file was replaced (rotated), the new file is all new,
# and the old one (now listserver.log.1) is new from the remembered size on.
new_log_text() {
  if [ "$(stat -f %i "$LOG" 2> /dev/null || echo 0)" = "$log_inode" ]; then
    tail -c +$((log_size + 1)) "$LOG" 2> /dev/null || true
  else
    cat "$LOG" 2> /dev/null || true
    if [ "$(stat -f %i "$LOG.1" 2> /dev/null || echo 0)" = "$log_inode" ]; then
      tail -c +$((log_size + 1)) "$LOG.1" 2> /dev/null || true
    fi
  fi
}
url=""
answered=0
deadline=$((SECONDS + HEALTH_WAIT))             # a deadline in seconds: one check can take a while (tailscale, curl)
while :; do
  if [ -z "$url" ]; then url="$(run_tool health-url --support-dir "$SUPPORT" 2> /dev/null || true)"; fi
  if [ -n "$url" ]; then
    case "$(new_log_text)" in                   # (no pipe into grep -q: with pipefail, an early exit of grep would fail the pipe)
      *"Listening on"*)
        case "$(curl -fsS --noproxy '*' --max-time 2 "$url" 2> /dev/null || true)" in
          *'"apiVersion"'*) answered=1 ;;
        esac
        ;;
    esac
  fi
  if [ "$answered" = 1 ]; then break; fi
  if [ "$SECONDS" -ge "$deadline" ]; then break; fi
  sleep 1
done
if [ "$answered" != 1 ]; then
  {
    echo "error: the new server did not answer /v1/health within ${HEALTH_WAIT} seconds."
    echo "--- last 10 lines of $LOG"
    tail -n 10 "$LOG" 2> /dev/null || echo "(no log yet)"
    if [ -s "$LOGS/launchd.err" ]; then
      echo "--- last 10 lines of $LOGS/launchd.err"
      tail -n 10 "$LOGS/launchd.err"
    fi
    echo "---"
    echo "Fix the cause and run this script again. To remove the install: $SUPPORT/current/uninstall.sh"
  } >&2
  exit 4
fi

echo "installed BridgeBoard server $VERSION: ${DOMAIN}/${LABEL}"
echo "files: $SUPPORT/current -> versions/$VERSION"
echo "config: $CONFIG"
echo "log: $LOG"
echo "answers: $url"
echo "to remove: \"$SUPPORT/current/uninstall.sh\" (add --purge to delete the data and logs too)"
echo "next: run \"$SUPPORT/current/bbctl\" pair"
