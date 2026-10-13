#!/usr/bin/env python3
"""把镜像 tag 写入 GitOps 的 <group>/artifacts.libsonnet（通过 artifacts-webhook）。

服务端语义（见 devops-utils/artifacts-webhook/src/main.rs）：
  读 <group>/artifacts.libsonnet（不存在则用 {} 建文件并加固定头注释）
  -> artifacts[docker][branch] = image_tag
  -> commit 并 push 到 artifacts-<group> 分支
  -> 再 merge 进该实例的合并目标分支（按集群配置，实测 ali-test = test）
  -> 异步 refresh ArgoCD 应用 <namespace>

用法示例：
  ./publish_artifact.py --endpoint https://artifacts-webhook.argocd.ali-test.xlbsoft.com \
      --group mart-retail-review --docker mart-review-b --branch prod-gray \
      --tag prod-gray-a0cd2d2f...-2026-08-07-11-21-28
  ./publish_artifact.py --endpoint <url> --plan plan.json --dry-run

plan.json: [{"group": "...", "docker": "...", "branch": "prod", "tag": "prod-..."}]

注意：队列 status=done 不等于写入成功（push 重试耗尽也可能显示 done）。
必须回读远端文件；脚本结尾会打印回读命令。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

TERMINAL = {'done', 'failed', 'error'}


def call(url: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method='POST' if data else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return resp.status, (json.loads(body) if body.strip().startswith(('{', '[')) else body)


def main() -> int:
    parser = argparse.ArgumentParser(description='通过 artifacts-webhook 写入 artifacts 条目')
    parser.add_argument('--endpoint', required=True, help='如 https://artifacts-webhook.argocd.<env>.xlbsoft.com')
    parser.add_argument('--group', help='GitOps 组目录名，也是 ArgoCD 应用名')
    parser.add_argument('--namespace', help='默认等于 --group')
    parser.add_argument('--docker', help='ACR 仓库名（即 artifacts 的 key）')
    parser.add_argument('--branch', help='环境键：dev/test/staging/prod-gray/prod')
    parser.add_argument('--tag', help='完整镜像 tag')
    parser.add_argument('--plan', help='JSON 计划文件（替代上面的单个参数）')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-wait', action='store_true', help='投递后不等队列终态')
    parser.add_argument('--timeout', type=int, default=1800, help='等待队列的秒数')
    args = parser.parse_args()

    endpoint = args.endpoint.rstrip('/')
    if args.plan:
        items = json.load(open(args.plan))
    elif args.group and args.docker and args.branch and args.tag:
        items = [{'group': args.group, 'namespace': args.namespace or args.group,
                  'docker': args.docker, 'branch': args.branch, 'tag': args.tag}]
    else:
        parser.error('需要 --plan，或 --group/--docker/--branch/--tag 组合')
    for item in items:
        item.setdefault('namespace', item['group'])

    if args.dry_run:
        for item in items:
            print(f"DRY-RUN group={item['group']} docker={item['docker']} "
                  f"branch={item['branch']} tag={item['tag']}")
        print(f'共 {len(items)} 条（未写入）')
        return 0

    try:
        _, baseline = call(f'{endpoint}/api/queues')
    except urllib.error.URLError as exc:
        print(f'端点不可达或不是 artifacts-webhook: {exc}', file=sys.stderr)
        return 2
    start_id = max([entry['id'] for entry in baseline.get('history', [])] or [0])
    print(f'队列基线 max_id={start_id}，待写入 {len(items)} 条')

    groups = {item['group'] for item in items}
    wanted = {(item['docker'], item['branch']) for item in items}
    rejected = []
    for item in items:
        payload = {'namespace': item['namespace'], 'group': item['group'],
                   'docker': item['docker'], 'branch': item['branch'],
                   'image_tag': item['tag']}
        try:
            status, body = call(endpoint, payload)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode(errors='replace')
        ok = 200 <= status < 300
        if not ok:
            rejected.append(f"{item['docker']}:{item['branch']} HTTP {status} {body}")
        print(f"POST {item['docker']:30} {item['branch']:10} -> {status} {body}")
    if rejected:
        print(f'\n{len(rejected)} 条未被接受，终止等待：', file=sys.stderr)
        for entry in rejected:
            print('  !!', entry, file=sys.stderr)
        return 1
    if args.no_wait:
        return 0

    deadline, settled = time.time() + args.timeout, {}
    while time.time() < deadline:
        time.sleep(10)
        _, queues = call(f'{endpoint}/api/queues')
        for entry in queues.get('history', []):
            if entry['id'] > start_id and entry.get('group') in groups \
                    and (entry.get('docker'), entry.get('branch')) in wanted:
                settled[entry['id']] = entry
        pending = len(queues.get('normal', [])) + len(queues.get('prod', []))
        print(f"  settled={len(settled)}/{len(items)} pending={pending}")
        if len(settled) >= len(items) and all(e['status'] in TERMINAL for e in settled.values()):
            break

    bad = [e for e in settled.values() if e['status'] != 'done']
    for entry in sorted(settled.values(), key=lambda e: e['id']):
        print(f"  id={entry['id']} {entry['docker']:30} {entry['branch']:10} "
              f"{entry['status']:10} {entry['image_tag']}")
    if bad or len(settled) < len(items):
        print(f'未全部成功：settled={len(settled)}/{len(items)} bad={len(bad)}', file=sys.stderr)
        return 1
    print('\n队列全部 done —— 仍需回读远端文件确认（done != 写入成功）：')
    for group in sorted(groups):
        print(f"  git clone -b artifacts-{group} <gitops-repo> /tmp/verify-{group} && "
              f"cat /tmp/verify-{group}/{group}/artifacts.libsonnet")
    return 0


if __name__ == '__main__':
    sys.exit(main())