/**
 * XLB 文档中心 · Cloudflare Worker 入口
 *
 * 职责：给 docs/ 静态站点加一道 HTTP Basic Auth 门禁，其余请求透传给静态资源绑定。
 * - 口令只来自 Worker secret `DOCS_PASSWORD`，源码/配置里不出现明文。
 * - 用户名来自 wrangler.jsonc 的 vars.DOCS_USER（非敏感，默认 xlb）。
 * - wrangler.jsonc 用 assets.run_worker_first=true，保证每个请求先过这里，无法被绕过。
 */

const REALM = 'Basic realm="XLB Docs", charset="UTF-8"';
const NO_STORE = { 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow, noarchive' };

/** 逐字符定长比较，避免用 === 造成的前缀时序差。 */
function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** UTF-8 安全的 base64（btoa 只接受 latin1，不能直接喂中文口令）。 */
function toBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/**
 * 直接比较 base64 文本而非解码后的口令：字节级精确，且天然避免 atob 的
 * latin1 还原导致非 ASCII 口令比对失败。
 */
function isAuthorized(header, user, password) {
  if (typeof header !== 'string') return false;
  const m = /^Basic\s+(.+)$/i.exec(header.trim());
  if (!m) return false;
  return constantTimeEqual(m[1].trim(), toBase64(`${user}:${password}`));
}

function unauthorized(message) {
  return new Response(message, {
    status: 401,
    headers: { ...NO_STORE, 'WWW-Authenticate': REALM, 'Content-Type': 'text/plain; charset=UTF-8' },
  });
}

export default {
  async fetch(request, env) {
    const password = env.DOCS_PASSWORD;
    if (!password) {
      return new Response('部署未完成：缺少 DOCS_PASSWORD secret。', {
        status: 500,
        headers: { ...NO_STORE, 'Content-Type': 'text/plain; charset=UTF-8' },
      });
    }

    if (!isAuthorized(request.headers.get('Authorization'), env.DOCS_USER || 'xlb', password)) {
      return unauthorized('需要访问密码。\n');
    }

    const res = await env.ASSETS.fetch(request);
    const headers = new Headers(res.headers);
    headers.set('X-Robots-Tag', 'noindex, nofollow, noarchive');
    // 页面要能随部署即时更新，但 ETag/Last-Modified 仍由资源层给出，浏览器会走条件请求。
    if ((headers.get('Content-Type') || '').includes('text/html')) {
      headers.set('Cache-Control', 'no-cache');
    }
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  },
};