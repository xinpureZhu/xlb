#!/usr/bin/env bash
#
# 本地预览（带同一个 Basic Auth 门禁）：暂存 docs/ 后在 127.0.0.1 起 wrangler dev。
#   ./serve_local.sh                       # 口令默认 xlb-docs-dev
#   DOCS_DEV_PASSWORD=xxx ./serve_local.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(cd "$SCRIPT_DIR/../worker" && pwd)"

"$SCRIPT_DIR/stage_docs.sh" --quiet >/dev/null

cd "$WORKER_DIR"
exec wrangler dev \
  --var "DOCS_PASSWORD:${DOCS_DEV_PASSWORD:-xlb-docs-dev}" \
  --port "${DOCS_DEV_PORT:-8787}" \
  --ip 127.0.0.1