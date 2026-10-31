#!/bin/bash
# Stops the BridgeBoard server and removes its LaunchAgent.
#
#   uninstall.sh            Unloads the LaunchAgent and deletes its plist. The data stays: "~/Library/Application Support/BridgeBoard/"
#                           (server files, config.json, the secret, the hashes of the paired devices' keys) and the logs in
#                           "~/Library/Logs/BridgeBoard/". Installing again brings back the same paired iPads.
#   uninstall.sh --purge    Also deletes those two folders. The paired iPads stop working and have to be paired again.
#                           This script lives inside the first folder; that is fine, the shell keeps the file open while it runs.
#
# Nothing else is touched: not Tailscale, not Claude Code, not your projects.
# Exit codes: 0 done · 1 something failed (--purge: the server did not stop, so the data folder and the logs were kept) · 64 wrong arguments
set -euo pipefail

main() {
  local label="jp.onemorelab.bridgeboard.server"
  local support="$HOME/Library/Application Support/BridgeBoard"
  local logs="$HOME/Library/Logs/BridgeBoard"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  local domain="gui/$(id -u)"
  local purge=0

  case "$#:${1:-}" in
    0:) ;;
    1:--purge) purge=1 ;;
    *)
      echo "usage: $0 [--purge]" >&2
      exit 64
      ;;
  esac
  case "$HOME" in
    /?*) ;;
    *)
      echo "error: HOME is not set to an absolute path" >&2
      exit 1
      ;;
  esac

  launchctl bootout "$domain/$label" 2> /dev/null || true    # does nothing if it is not loaded
  rm -f "$plist"
  echo "removed: ${domain}/${label} (deleted ${plist})"

  if [ "$purge" = 0 ]; then
    echo "kept: $support (config, secret, hashes of the paired devices' keys) and $logs (logs)"
    echo "to delete them too: run this again with --purge"
    return 0
  fi
  # Let the server finish stopping first, so that it cannot write into the folders while they are deleted.
  local stop_wait="${BRIDGEBOARD_STOP_WAIT:-15}" deadline=$((SECONDS + ${BRIDGEBOARD_STOP_WAIT:-15}))
  while pgrep -f -- "$support/current/listserver.py" > /dev/null 2>&1 && [ "$SECONDS" -lt "$deadline" ]; do
    sleep 1
  done
  if pgrep -f -- "$support/current/listserver.py" > /dev/null 2>&1; then
    echo "error: the server did not stop within ${stop_wait} seconds, so the data folder and the logs were not deleted. Stop it and run this again." >&2
    exit 1
  fi
  rm -rf "$support" "$logs"
  echo "deleted: $support and $logs (every paired iPad has to be paired again)"
}

main "$@"
exit 0
