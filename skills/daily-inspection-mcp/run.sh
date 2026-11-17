#!/bin/bash
# Daily goods inspection wrapper: one lock, chunked agent calls, optional Feishu card.
set -uo pipefail

export HOME="/Users/xinpure"
export PATH="/Users/xinpure/.local/share/fnm/aliases/default/bin:/Users/xinpure/.local/bin:/Users/xinpure/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
ROOT="/Users/xinpure/Project/xlb"
OUT_DIR="$HOME/.xlb/reports/daily"
LOCK="$OUT_DIR/.lock"
DATE="$(date +%Y%m%d)"

mkdir -p "$OUT_DIR" "$HOME/Library/Logs/xlb"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[skip] another inspection is running"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$ROOT" || exit 1
args=(--date "$DATE")
if [ "${XLB_NO_SEND:-0}" = "1" ]; then
  args+=(--no-send)
fi
echo "[start] $(date '+%F %T')"
python3 skills/daily-inspection-mcp/run_inspection.py "${args[@]}"
status=$?
echo "[done] exit=$status $(date '+%F %T')"
exit "$status"