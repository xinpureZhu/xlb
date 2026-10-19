#!/usr/bin/env bash
#
# 一条命令同步部署：仓库根 docs/ -> Cloudflare Worker（Basic Auth 门禁）
#   ./deploy_docs.sh              # 暂存 docs -> 发布 -> 回读校验
#   ./deploy_docs.sh --dry-run    # 只本地打包预检，不发布、不需登录
#   ./deploy_docs.sh --skip-verify
#
# 需要的凭据：
#   wrangler login（或 CLOUDFLARE_API_TOKEN，需 Workers Scripts:Edit）
#   Worker secret DOCS_PASSWORD（访问口令，见 SKILL.md）
# 可选：环境里存在 DOCS_PASSWORD / DOCS_USER 时，额外做带凭据的 200、内容哈希与 index.html 字节校验。
#
# 首次部署的顺序问题：Worker 不存在时无法先设 secret（wrangler secret put 会报 Worker not found），
# 因此允许「先发布、后设 secret」。在 secret 就位前站点对所有请求返回 500（fail-closed，不泄露内容）；
# 而 Worker 已存在却缺 secret 属异常状态，直接阻断，避免把可公开访问的旧配置顶上去。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKER_DIR="$SKILL_DIR/worker"
STAGE_DIR="$WORKER_DIR/public"

DRY_RUN=0
SKIP_VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# 1) 暂存（stdout 只回哈希，进度走 stderr）
CONTENT_HASH="$("$SCRIPT_DIR/stage_docs.sh")"

cd "$WORKER_DIR"

# 2) 本地打包预检
if [ "$DRY_RUN" = "1" ]; then
  echo "==> dry-run：仅本地打包，不发布"
  wrangler deploy --dry-run
  echo "==> dry-run 通过（content_hash=${CONTENT_HASH}）"
  exit 0
fi

# 3) 前置条件：登录态
if ! wrangler whoami >/dev/null 2>&1; then
  cat >&2 <<'EOF'
错误：wrangler 未登录或 token 已过期。二选一：
  交互登录：  wrangler login
  API Token： export CLOUDFLARE_API_TOKEN=<具备 Workers Scripts:Edit 的 token>
EOF
  exit 1
fi

# 4) 口令 secret 状态：区分「首次部署（Worker 不存在）」与「已存在却缺 secret」
SECRETS_OUT="$(wrangler secret list 2>&1 || true)"
WORKER_EXISTS=1
HAVE_SECRET=0
printf '%s' "$SECRETS_OUT" | grep -qi 'not found' && WORKER_EXISTS=0
printf '%s' "$SECRETS_OUT" | grep -q '"DOCS_PASSWORD"' && HAVE_SECRET=1

SECRET_HINT="设置口令（口令不进仓库、不上屏）：
  cd $WORKER_DIR
  printf '%s' '<口令>' | wrangler secret put DOCS_PASSWORD"

if [ "$WORKER_EXISTS" = "1" ] && [ "$HAVE_SECRET" = "0" ]; then
  printf '错误：Worker xlb-docs 已存在但缺少 DOCS_PASSWORD secret，中止发布。\n%s\n' "$SECRET_HINT" >&2
  exit 1
fi
if [ "$WORKER_EXISTS" = "0" ]; then
  echo "==> 首次部署：Worker 还不存在。发布后必须立刻设置 DOCS_PASSWORD，否则站点对所有请求返回 500"
fi

# 5) 发布
echo "==> 发布 Worker xlb-docs（content_hash=${CONTENT_HASH}）"
set +e
DEPLOY_OUT="$(wrangler deploy 2>&1)"
DEPLOY_STATUS=$?
set -e
printf '%s\n' "$DEPLOY_OUT"
if [ "$DEPLOY_STATUS" -ne 0 ]; then
  if printf '%s' "$DEPLOY_OUT" | grep -qi 'workers\.dev subdomain'; then
    echo "提示：账号尚未注册 workers.dev 子域，需交互执行一次：wrangler deploy（或 wrangler subdomain）" >&2
  fi
  echo "错误：wrangler deploy 失败（exit=${DEPLOY_STATUS}）" >&2
  exit "$DEPLOY_STATUS"
fi

URL="${DOCS_URL:-$(printf '%s' "$DEPLOY_OUT" | grep -oE 'https://[A-Za-z0-9.-]+\.workers\.dev' | head -1)}"
[ -n "$URL" ] || { echo "错误：未能从 deploy 输出解析站点 URL，可用 DOCS_URL 环境变量指定" >&2; exit 1; }
echo "==> 站点：$URL"

if [ "$HAVE_SECRET" = "0" ]; then
  printf '\n==> 下一步（必做）：\n%s\n  在设置口令前，站点对任何请求返回 500（不会泄露文档内容）。\n' "$SECRET_HINT"
  echo "  设好后重跑本脚本即可得到完整的 401/200 与内容校验。"
  exit 1
fi

if [ "$SKIP_VERIFY" = "1" ]; then
  echo "==> 已跳过回读校验（--skip-verify）"
  exit 0
fi

# 6) 回读校验（任一硬校验失败 -> 退出码 1，避免「看起来部署成功但线上是旧版」）
echo "==> 回读校验"
VERIFY_FAILED=0
fail() {
  echo "  警告：$1" >&2
  VERIFY_FAILED=1
}

# 超时兜底：本机网络若无法直连 *.workers.dev（DNS/SNI 被污染），curl 会一直挂着。
# curl 自带识别 https_proxy/HTTPS_PROXY/ALL_PROXY，需要走代理时先 export 再跑本脚本。
CURL=(curl -sS --connect-timeout 10 --max-time 60)

ANON_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$URL/")"
echo "  未带凭据 GET /            -> HTTP ${ANON_CODE}（期望 401）"
if [ "$ANON_CODE" = "000" ]; then
  echo "错误：连不上 $URL —— 本机网络无法直连 *.workers.dev（DNS/SNI 被污染），部署本身已完成。" >&2
  echo "      走代理复验：export https_proxy=http://127.0.0.1:7890 && $0 ${*:-}" >&2
  exit 1
fi
[ "$ANON_CODE" = "401" ] || fail "未带凭据返回 ${ANON_CODE}，门禁可能未生效"

if [ -n "${DOCS_PASSWORD:-}" ]; then
  DOCS_USER="${DOCS_USER:-xlb}"
  AUTH_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' -u "$DOCS_USER:$DOCS_PASSWORD" "$URL/")"
  echo "  带凭据 GET /              -> HTTP ${AUTH_CODE}（期望 200）"
  [ "$AUTH_CODE" = "200" ] || fail "带凭据访问返回 ${AUTH_CODE}，口令或 DOCS_USER 不匹配"

  REMOTE_HASH="$("${CURL[@]}" -u "$DOCS_USER:$DOCS_PASSWORD" "$URL/version.json" | sed -n 's/.*"content_hash":"\([^"]*\)".*/\1/p')"
  echo "  远端 version.json hash    -> ${REMOTE_HASH:-<未取到>}（本地 ${CONTENT_HASH}）"
  if [ "$REMOTE_HASH" = "$CONTENT_HASH" ]; then
    echo "  内容哈希一致：远端与本次暂存的 docs/ 完全相同"
  else
    fail "内容哈希不一致，远端可能仍是旧版本"
  fi

  # 用站点根路径而非 /index.html：auto-trailing-slash 下 /index.html 会 307 到 /，不带 -L 只能拿到空的重定向体。
  LOCAL_INDEX="$(shasum -a 256 "$STAGE_DIR/index.html" | cut -d' ' -f1)"
  REMOTE_INDEX="$("${CURL[@]}" -L -u "$DOCS_USER:$DOCS_PASSWORD" "$URL/" | shasum -a 256 | cut -d' ' -f1)"
  if [ "$LOCAL_INDEX" = "$REMOTE_INDEX" ]; then
    echo "  index.html 字节校验一致"
  else
    fail "index.html 字节不一致（本地 $LOCAL_INDEX / 远端 ${REMOTE_INDEX}）"
  fi
else
  echo "  提示：未设置环境变量 DOCS_PASSWORD，跳过带凭据的 200、内容哈希与字节比对"
  echo "        自验命令：curl -sS -u <用户名>:<口令> $URL/version.json"
fi

if [ "$VERIFY_FAILED" = "1" ]; then
  echo "错误：回读校验未通过（Worker 已发布，但线上内容/门禁不符合预期）" >&2
  exit 1
fi

echo "==> 完成"