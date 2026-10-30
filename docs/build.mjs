#!/usr/bin/env node
/**
 * 生成 docs/*.html 的公共 header / footer。
 *
 * 单一事实源：
 *   - docs/_partials/header.html  header 与导航模板
 *   - docs/_partials/footer.html  footer 模板
 *   - 每个文档顶部的 <!--DOC_META ...--> 元数据
 *
 * 新增文档只需：
 *   1. 复制 docs/_template.html 为 NN-topic.html
 *   2. 修改 DOC_META 里的 title / navLabel / subtitle / meta
 *   3. 运行 node docs/build.mjs
 *
 * 导航由所有文档的 DOC_META.navLabel 自动汇总，按文件名序号排序；
 * 不需要维护单独的导航清单，也不需要改任何旧文档。
 *
 * 用法：
 *   node docs/build.mjs          # 生成/更新所有文档
 *   node docs/build.mjs --check  # 只校验，不写文件；有差异退出码 1
 *   node docs/build.mjs --quiet  # 只输出错误
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DOCS_DIR = path.dirname(fileURLToPath(import.meta.url));
const PARTIALS_DIR = path.join(DOCS_DIR, '_partials');
const HEADER_START = '<!-- DOC_HEADER_START -->';
const HEADER_END = '<!-- DOC_HEADER_END -->';
const FOOTER_START = '<!-- DOC_FOOTER_START -->';
const FOOTER_END = '<!-- DOC_FOOTER_END -->';
const DEFAULT_EYEBROW = 'XLB 零售平台 · 文档中心';
const DEFAULT_FOOTER = 'XLB 零售平台文档中心 · 本文档由源码静态分析生成，仅供架构参考。';

const args = new Set(process.argv.slice(2));
const checkOnly = args.has('--check');
const quiet = args.has('--quiet');

const read = (file) => fs.readFileSync(file, 'utf8');
const headerTemplate = read(path.join(PARTIALS_DIR, 'header.html')).trimEnd();
const footerTemplate = read(path.join(PARTIALS_DIR, 'footer.html')).trimEnd();

function fail(message) {
  console.error(`docs shell: ${message}`);
  process.exit(1);
}

function parseMeta(file, source) {
  const match = source.match(/<!--DOC_META([\s\S]*?)-->/);
  if (!match) {
    fail(`${file} 缺少 <!--DOC_META ...--> 元数据；请参考 docs/_template.html`);
  }
  try {
    return JSON.parse(match[1].trim());
  } catch (error) {
    fail(`${file} 的 DOC_META 不是合法 JSON：${error.message}`);
  }
}

function orderOf(file) {
  if (file === 'index.html') return -1;
  const match = file.match(/^(\d+)-/);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}

function navLabelOf(file, meta) {
  if (meta.navLabel) return meta.navLabel;
  const title = String(meta.title || file)
    .replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return title || file;
}

const documents = fs
  .readdirSync(DOCS_DIR)
  .filter((file) => file.endsWith('.html') && !file.startsWith('_'))
  .sort((a, b) => orderOf(a) - orderOf(b) || a.localeCompare(b))
  .map((file) => {
    const source = read(path.join(DOCS_DIR, file));
    return { file, source, meta: parseMeta(file, source) };
  });

const navItems = documents
  .filter(({ meta }) => meta.nav !== false)
  .map(({ file, meta }) => ({ file, label: navLabelOf(file, meta) }));

let changed = 0;

for (const { file, source, meta } of documents) {
  if (!source.includes(HEADER_START) || !source.includes(FOOTER_START)) {
    fail(`${file} 缺少 ${HEADER_START} / ${FOOTER_START} 标记；请参考 docs/_template.html`);
  }

  const nav = navItems
    .map((item) => {
      const active = item.file === file ? ' class="active"' : '';
      return `    <a href="${item.file}"${active}>${item.label}</a>`;
    })
    .join('\n');

  const subtitle = meta.subtitle ? `    <p class="subtitle">${meta.subtitle}</p>` : '';
  const header = headerTemplate
    .replaceAll('{{EYEBROW}}', meta.eyebrow || DEFAULT_EYEBROW)
    .replaceAll('{{TITLE}}', meta.title || '')
    .replaceAll('{{SUBTITLE_BLOCK}}', subtitle)
    .replaceAll('{{META}}', meta.meta || '')
    .replaceAll('{{NAV}}', nav);

  const footer = footerTemplate.replaceAll('{{FOOTER}}', meta.footer || DEFAULT_FOOTER);
  const indentBlock = (text) => text.split('\n').map((line) => (line ? `  ${line}` : line)).join('\n');
  const headerRegion = `  ${HEADER_START}\n${indentBlock(header)}\n  ${HEADER_END}`;
  const footerRegion = `  ${FOOTER_START}\n${indentBlock(footer)}\n  ${FOOTER_END}`;

  const next = source
    .replace(/[ \t]*<!-- DOC_HEADER_START -->[\s\S]*?<!-- DOC_HEADER_END -->[ \t\r\n]*(?=<main\b)/, () => `${headerRegion}\n\n  `)
    .replace(/[ \t]*<!-- DOC_FOOTER_START -->[\s\S]*?<!-- DOC_FOOTER_END -->/, () => footerRegion);

  if (next !== source) {
    changed += 1;
    if (!checkOnly) fs.writeFileSync(path.join(DOCS_DIR, file), next);
  }
}

if (checkOnly) {
  if (changed > 0) {
    console.error(`docs shell: ${changed} 个文档未生成，请运行 node docs/build.mjs`);
    process.exit(1);
  }
  if (!quiet) console.log(`docs shell: ${documents.length} 个文档已是最新`);
} else if (!quiet) {
  console.log(`docs shell: ${documents.length} 个文档，更新 ${changed} 个`);
}