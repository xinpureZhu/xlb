#!/bin/bash
# Weekly delivery report: read-only Efficiency MCP, optional Feishu bot message.
set -uo pipefail

export HOME="/Users/xinpure"
export PATH="/Users/xinpure/.local/share/fnm/aliases/default/bin:/Users/xinpure/.local/bin:/Users/xinpure/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
ROOT="/Users/xinpure/Project/xlb"
OUT_DIR="$HOME/.xlb/reports/weekly"
LOCK="$OUT_DIR/.lock-weekly"
USER_ID="ou_5d3dc8766fa70f73a8c0ace68cbbb565"

mkdir -p "$OUT_DIR" "$HOME/Library/Logs/xlb"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[skip] another weekly run is running"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$ROOT" || exit 1
args=(--days 7)
if [ "${XLB_NO_SEND:-0}" != "1" ]; then
  args+=(--send --user-id "$USER_ID")
fi
echo "[start] $(date '+%F %T')"
python3 skills/weekly-delivery-report/report.py "${args[@]}"
status=$?
echo "[done] exit=$status $(date '+%F %T')"
exit "$status"