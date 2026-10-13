#!/usr/bin/env python3
"""精确复制 ACR 镜像 tag：<src_repo>:<tag> -> <dst_repo>:<tag>（逐字节一致）。

为什么要专用工具：
  `docker buildx imagetools create -t dst src` 会把单个 manifest 包装成
  manifest list，目标 digest 与 media type 都会变；本工具改为「先把 blob 落到
  目标仓库，再把源 manifest 原字节按原 media type PUT」，因此
  目标 tag 的 sha256 == 源 tag 的 sha256。

用法示例：
  export ACR_USER='<acr 访问凭证用户名>' ACR_PW='<口令>'   # 或 --password-file
  ./acr_exact_copy.py --plan plan.json --preflight      # 只读预检
  ./acr_exact_copy.py --plan plan.json --dry-run        # 只打印计划，不触网
  ./acr_exact_copy.py --plan plan.json                  # 执行复制并校验
  ./acr_exact_copy.py --plan plan.json --verify-only    # 只读校验 digest 与 blob

plan.json 结构（两种都支持）：
  [{"src": "zc_xlb/a", "dst": "zc_xlb/mart-a", "tags": ["dev-1", "prod-1"]}, ...]
  [{"src": "zc_xlb/a", "dst": "zc_xlb/mart-a", "tag": "dev-1"}, ...]

限制：
  - 源必须是单 manifest（`manifest.v2` / OCI `image.manifest.v1`）。源是 manifest
    list/index 时本工具只报告不支持，不猜测平台，也不会动目标仓库。
  - 源与目标必须同属 `--registry` 指向的同一个 registry 实例（跨实例请分两次跑）。

退出码：0 全部成功；1 有 tag 失败（会逐条列出，其余条目继续处理）；2 参数错误/凭据缺失。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REGISTRY = os.environ.get('ACR_REGISTRY', 'xlb-acr-registry.cn-hangzhou.cr.aliyuncs.com')
DEFAULT_REALM = os.environ.get('ACR_REALM', 'https://dockerauth.cn-hangzhou.aliyuncs.com/auth')
DEFAULT_SERVICE = os.environ.get(
    'ACR_SERVICE', 'registry.aliyuncs.com:cn-hangzhou:china:cri-9sd5of4lfqkvmx3v')
ACCEPT = ', '.join([
    'application/vnd.docker.distribution.manifest.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.index.v1+json'])


class Registry:
    def __init__(self, registry: str, realm: str, service: str, user: str, password: str):
        self.registry = registry
        self.realm = realm
        self.service = service
        self.basic = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        self._tokens: dict[tuple[str, str], str] = {}

    def token(self, repo: str, actions: str = 'pull') -> str:
        key = (repo, actions)
        if key not in self._tokens:
            query = urllib.parse.urlencode([
                ('service', self.service), ('scope', f'repository:{repo}:{actions}')])
            req = urllib.request.Request(f'{self.realm}?{query}', headers={'Authorization': self.basic})
            self._tokens[key] = json.load(urllib.request.urlopen(req, timeout=30))['token']
        return self._tokens[key]

    def request(self, method: str, url: str, token: str, data=None, headers=None, timeout=300):
        merged = {'Authorization': 'Bearer ' + token}
        if headers:
            merged.update(headers)
        return urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=merged, method=method), timeout=timeout)

    def get_manifest(self, repo: str, ref: str):
        resp = self.request('GET', f'https://{self.registry}/v2/{repo}/manifests/{ref}',
                            self.token(repo, 'pull'), headers={'Accept': ACCEPT})
        return resp.read(), resp.headers['Content-Type']

    def tags(self, repo: str):
        """返回 tag 列表；仓库不存在时返回 None。"""
        try:
            resp = self.request('GET', f'https://{self.registry}/v2/{repo}/tags/list',
                                self.token(repo, 'pull'))
            return json.load(resp).get('tags') or []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def has_blob(self, repo: str, digest: str) -> bool:
        try:
            self.request('HEAD', f'https://{self.registry}/v2/{repo}/blobs/{digest}',
                         self.token(repo, 'push,pull'), timeout=30)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def put_manifest(self, repo: str, ref: str, body: bytes, media_type: str) -> int:
        resp = self.request('PUT', f'https://{self.registry}/v2/{repo}/manifests/{ref}',
                            self.token(repo, 'push,pull'), data=body,
                            headers={'Content-Type': media_type})
        return resp.status

    def copy_blob(self, src: str, dst: str, digest: str) -> tuple[str, int]:
        """返回 (方式, 上传字节数)。"""
        if self.has_blob(dst, digest):
            return 'present', 0
        try:
            resp = self.request(
                'POST', f'https://{self.registry}/v2/{dst}/blobs/uploads/?mount={digest}&from={src}',
                self.token(dst, 'push,pull'), data=b'', timeout=60)
            if resp.status == 201:
                return 'mounted', 0
        except urllib.error.HTTPError as exc:
            if exc.code not in (202, 400, 401, 404):
                raise
        # 跨仓库 mount 不被接受时，经本地临时文件中转。
        tmp = tempfile.NamedTemporaryFile(delete=False).name
        try:
            with self.request('GET', f'https://{self.registry}/v2/{src}/blobs/{digest}',
                              self.token(src, 'pull'), timeout=3600) as resp:
                size, written = int(resp.headers['Content-Length']), 0
                with open(tmp, 'wb') as handle:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
            if written != size:
                raise IOError(f'blob 长度不符 {written} != {size} ({digest})')
            upload = self.request('POST', f'https://{self.registry}/v2/{dst}/blobs/uploads/',
                                  self.token(dst, 'push,pull'), data=b'', timeout=60)
            location = upload.headers['Location']
            if location.startswith('/'):
                location = f'https://{self.registry}{location}'
            separator = '&' if '?' in location else '?'
            with open(tmp, 'rb') as handle:
                self.request('PUT', f'{location}{separator}digest={digest}',
                             self.token(dst, 'push,pull'), data=handle.read(),
                             headers={'Content-Type': 'application/octet-stream',
                                      'Content-Length': str(size)}, timeout=3600)
        finally:
            os.unlink(tmp)
        return 'uploaded', size

    def manifest_blobs(self, body: bytes) -> list[str] | None:
        """返回 config+layers 的 digest；源是 manifest list/index 时返回 None。"""
        manifest = json.loads(body)
        if 'manifests' in manifest:
            return None
        return [manifest['config']['digest']] + [layer['digest'] for layer in manifest.get('layers', [])]


def load_plan(path: str) -> list[dict]:
    raw = json.load(open(path))
    items = []
    for entry in raw:
        for tag in entry.get('tags', [entry['tag']] if entry.get('tag') else []):
            items.append({'src': entry['src'], 'dst': entry['dst'], 'tag': tag})
    return items


def describe(reg: Registry, item: dict) -> tuple[str, dict]:
    """只读：源 manifest 事实 + 目标仓库现状。"""
    body, media_type = reg.get_manifest(item['src'], item['tag'])
    digest = hashlib.sha256(body).hexdigest()
    blobs = reg.manifest_blobs(body)
    dst_tags = reg.tags(item['dst'])
    same_tag_same_digest = False
    if dst_tags and item['tag'] in dst_tags:
        dst_body, _ = reg.get_manifest(item['dst'], item['tag'])
        same_tag_same_digest = hashlib.sha256(dst_body).hexdigest() == digest
    facts = {'src_digest': digest, 'media_type': media_type,
             'blobs': None if blobs is None else len(blobs),
             'src_bytes': sum(layer['size'] for layer in json.loads(body).get('layers', []))
                          + json.loads(body).get('config', {}).get('size', 0) if blobs else 0,
             'dst_repo_exists': dst_tags is not None,
             'dst_tag_exists': bool(dst_tags and item['tag'] in dst_tags),
             'dst_tag_same_digest': same_tag_same_digest}
    if blobs is None:
        return f"[SKIP] {item['src']}:{item['tag']} 源是 manifest list/index（本工具不支持）", facts
    over = ''
    if facts['dst_tag_exists'] and not same_tag_same_digest:
        over = '  ⚠ 目标同名 tag 已存在且 digest 不同：执行将覆盖，需要额外授权'
    return (f"[PRE ] {item['src']}:{item['tag']} -> {item['dst']}:{item['tag']} "
            f"sha256={digest[:16]} type={media_type.split('.')[-2]} blobs={len(blobs)} "
            f"{facts['src_bytes'] / 1048576:.0f}MiB dst_exists={facts['dst_repo_exists']} "
            f"dst_tag_exists={facts['dst_tag_exists']}{over}"), facts


def main() -> int:
    parser = argparse.ArgumentParser(description='ACR 镜像 tag 逐字节精确复制')
    parser.add_argument('--plan', help='JSON 计划文件')
    parser.add_argument('--src', help='源仓库，如 zc_xlb/sync-worker')
    parser.add_argument('--dst', help='目标仓库，如 zc_xlb/mart-sync-worker')
    parser.add_argument('--tag', action='append', default=[], help='tag，可重复')
    parser.add_argument('--registry', default=DEFAULT_REGISTRY)
    parser.add_argument('--realm', default=DEFAULT_REALM)
    parser.add_argument('--service', default=DEFAULT_SERVICE)
    parser.add_argument('--password-file', help='含 `ACR_USER=…` 与 `ACR_PW=…` 两行的文件（600 权限）')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='只打印计划，不触网、不写')
    mode.add_argument('--preflight', action='store_true', help='只读预检：源 digest/形态 + 目标仓库现状')
    mode.add_argument('--verify-only', action='store_true', help='只读校验：digest、media type、blob 在位')
    args = parser.parse_args()

    if args.plan:
        items = load_plan(args.plan)
    elif args.src and args.dst and args.tag:
        items = [{'src': args.src, 'dst': args.dst, 'tag': t} for t in args.tag]
    else:
        parser.error('需要 --plan，或 --src/--dst/--tag 组合')
    if not items:
        parser.error('计划为空')

    if args.dry_run:
        for item in items:
            print(f"DRY-RUN {item['src']}:{item['tag']} -> {item['dst']}:{item['tag']}")
        print(f'共 {len(items)} 个 tag（未触网、未写入）')
        return 0

    if args.password_file:
        for line in open(args.password_file):
            if '=' in line:
                key, value = line.strip().split('=', 1)
                os.environ[key] = value
    user, password = os.environ.get('ACR_USER'), os.environ.get('ACR_PW')
    if not user or not password:
        parser.error('缺少 ACR_USER / ACR_PW（环境变量或 --password-file）')

    reg = Registry(args.registry, args.realm, args.service, user, password)
    failures, processed, uploaded_bytes, skipped = [], 0, 0, 0
    for item in items:
        label = f"{item['src']}:{item['tag']} -> {item['dst']}:{item['tag']}"
        try:
            if args.preflight:
                line, _ = describe(reg, item)
                print(line)
                continue
            src_body, src_type = reg.get_manifest(item['src'], item['tag'])
            src_digest = hashlib.sha256(src_body).hexdigest()
            blobs = reg.manifest_blobs(src_body)
            if blobs is None:
                print(f'[SKIP] {label} 源是 manifest list/index（本工具不支持，未改动目标）')
                skipped += 1
                continue
            stats = {}
            if not args.verify_only:
                for digest in blobs:
                    how, size = reg.copy_blob(item['src'], item['dst'], digest)
                    stats[how] = stats.get(how, 0) + 1
                    uploaded_bytes += size
                reg.put_manifest(item['dst'], item['tag'], src_body, src_type)
            dst_body, dst_type = reg.get_manifest(item['dst'], item['tag'])
            dst_digest = hashlib.sha256(dst_body).hexdigest()
            missing = [] if not args.verify_only else [d for d in blobs if not reg.has_blob(item['dst'], d)]
            ok = (src_digest == dst_digest and src_type == dst_type and not missing)
            processed += 1
            if not ok:
                failures.append(f'{label} digest {src_digest[:16]}/{src_type} != {dst_digest[:16]}/{dst_type}'
                                + (f' 缺 blob {len(missing)} 个' if missing else ''))
            print(f"[{'OK ' if ok else 'BAD'}] {label} sha256={dst_digest[:16]} "
                  f"type={dst_type.split('.')[-2]} blobs={stats if not args.verify_only else '-'}")
        except Exception as exc:  # 单条失败不影响其余条目
            failures.append(f'{label} 异常：{exc}')
            print(f'[BAD] {label} 异常：{exc}')
    print(f'\n共处理 {processed} 个 tag，跳过 {skipped}，失败 {len(failures)}，'
          f'本机中转上传 {uploaded_bytes / 1073741824:.2f} GiB')
    for failure in failures:
        print('  !!', failure)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())