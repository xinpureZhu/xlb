#!/usr/bin/env bash
#
# 把仓库根 docs/ 同步到 worker/public 暂存目录，并写入部署元数据 version.json。
#
# 用法：
#   ./stage_docs.sh            # 暂存 + 打印 docs 内容哈希（stdout 只有哈希）
#   ./stage_docs.sh --quiet    # 不打印进度，只输出哈希
#
# 约定：stdout 仅输出 content_hash，人类可读信息一律走 stderr，便于被其他脚本捕获。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
DOCS_DIR="${DOCS_DIR:-$REPO_ROOT/docs}"
STAGE_DIR="${STAGE_DIR:-$SKILL_DIR/worker/public}"

QUIET=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    -h | --help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

log() { [ "$QUIET" = "1" ] || echo "$@" >&2; }

[ -d "$DOCS_DIR" ] || { echo "错误：找不到文档目录 $DOCS_DIR" >&2; exit 1; }
[ -f "$DOCS_DIR/index.html" ] || { echo "错误：$DOCS_DIR/index.html 不存在，docs/ 不是可部署站点" >&2; exit 1; }

mkdir -p "$STAGE_DIR"
# --delete：docs/ 里删掉的文件必须在站点上消失，否则「同步部署」不成立。
# version.json 不在 docs/ 中，会被自动清掉，随后重新生成为当前值。
rsync -a --delete --exclude '.DS_Store' --exclude '._*' "$DOCS_DIR/" "$STAGE_DIR/"

FILE_COUNT="$(cd "$STAGE_DIR" && find . -type f ! -name '.DS_Store' | wc -l | tr -d ' ')"
CONTENT_HASH="$(
  cd "$STAGE_DIR" &&
    find . -type f ! -name '.DS_Store' -print0 |
    LC_ALL=C sort -z |
    xargs -0 shasum -a 256 |
    shasum -a 256 | cut -c1-16
)"
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- docs 2>/dev/null)" ]; then
  GIT_DIRTY=true
else
  GIT_DIRTY=false
fi
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat >"$STAGE_DIR/version.json" <<EOF
{"project":"xlb-docs","content_hash":"$CONTENT_HASH","files":$FILE_COUNT,"git_commit":"$GIT_COMMIT","docs_dirty":$GIT_DIRTY,"built_at":"$BUILT_AT"}
EOF

log "暂存：$DOCS_DIR -> $STAGE_DIR"
log "  文件数=$FILE_COUNT  git=$GIT_COMMIT  docs_dirty=$GIT_DIRTY  built_at=$BUILT_AT"
log "  content_hash=$CONTENT_HASH"

echo "$CONTENT_HASH"