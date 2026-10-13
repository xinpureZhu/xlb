#!/usr/bin/env python3
"""轻舟 code task 与分支流转的最小客户端。

覆盖 code task 创建/查询/删除、从 task 拉需求分支、迭代分支的门禁与 dev CD 跳过开关。
凭据从 cookie 文件读取，不打印、不入库。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://z.xlbsoft.com/api/efficiency"
DEFAULT_COOKIE_FILE = "/private/tmp/z_cookie.txt"

TASK_TYPES = ("story", "issue")
OWNER_SIDES = ("BACKEND", "PC", "APP", "MINIAPP", "POSQT", "DW")
SKIPPABLE_GATE = "requirement_completeness"


def load_cookie():
    path = os.environ.get("Z_COOKIE_FILE", DEFAULT_COOKIE_FILE)
    if os.path.exists(path):
        value = open(path, encoding="utf-8").read().strip()
        if value:
            return value
    value = os.environ.get("Z_COOKIE", "").strip()
    if value:
        return value
    sys.exit(f"missing cookie: write it to {path} or set Z_COOKIE")


def post(path, payload):
    base = os.environ.get("Z_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": load_cookie(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        sys.exit(f"HTTP {err.code} on {path}: {err.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as err:
        sys.exit(f"request to {path} failed: {err.reason}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit(f"non-JSON response from {path}: {body[:500]}")


def show(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("code") == 0 else 1


def cmd_list(args):
    return show(post("/feishu-tasks/by-workitem", {"work_item_id": args.work_item}))


def cmd_create(args):
    payload = [
        {
            "work_item_id": args.work_item,
            "type": args.type,
            "owner_side": args.owner_side,
            "application_ids": args.app_id,
        }
    ]
    if not args.yes:
        print("dry-run, re-run with --yes to submit:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    result = show(post("/feishu-tasks/batch-create", payload))
    created = (post("/feishu-tasks/by-workitem", {"work_item_id": args.work_item}) or {}).get("data", {})
    print(f"tasks now on work item {args.work_item}: {created.get('total')}")
    return result


def cmd_delete(args):
    if not args.yes:
        print(f"dry-run, would delete task {args.task_id}; re-run with --yes")
        return 0
    return show(post("/feishu-tasks/delete", {"id": args.task_id}))


def cmd_branch(args):
    payload = {"taskId": args.task_id, "branchFlow": args.flow}
    if not args.yes:
        print("dry-run, re-run with --yes to submit:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    result = post("/api/v1/branches/create-from-task", payload)
    show(result)
    data = result.get("data") or {}
    req_branch = data.get("requirementBranch") or {}
    iter_branch = data.get("iterationBranch") or {}
    if req_branch or iter_branch:
        print(
            f"requirement branch {req_branch.get('id')} {req_branch.get('branchName')} "
            f"-> iteration branch {iter_branch.get('id')} {iter_branch.get('branchName')}"
        )
    return 0 if result.get("code") == 0 else 1


def cmd_switches(args):
    body = {"iterationBranchId": args.iteration_branch}
    gates = post("/api/v1/iteration-branches/get-skip-promotion-gates", body).get("data") or {}
    dev_cd = post("/api/v1/iteration-branches/get-skip-dev-cd", body).get("data") or {}
    print(
        json.dumps(
            {
                "iterationBranchId": args.iteration_branch,
                "skipGates": gates.get("skipGates"),
                "skipDevCd": dev_cd.get("skipDevCd"),
                "releaseTestAutoPass": dev_cd.get("releaseTestAutoPass"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_skip(args):
    branch = args.iteration_branch
    calls = []
    if not args.no_gates:
        calls.append(
            (
                "/api/v1/iteration-branches/skip-promotion-gates",
                {"iterationBranchId": branch, "skipGates": [SKIPPABLE_GATE]},
            )
        )
    if not args.no_dev_cd:
        calls.append(
            ("/api/v1/iteration-branches/skip-dev-cd", {"iterationBranchId": branch, "skipDevCd": True})
        )
    if not args.no_auto_pass:
        calls.append(
            (
                "/api/v1/iteration-branches/release-test-auto-pass",
                {"iterationBranchId": branch, "releaseTestAutoPass": True},
            )
        )
    if not args.yes:
        print("dry-run, re-run with --yes to submit:")
        for path, body in calls:
            print(f"POST {path} {json.dumps(body, ensure_ascii=False)}")
        return 0
    failed = 0
    for path, body in calls:
        result = post(path, body)
        print(f"{path} -> code={result.get('code')} msg={result.get('msg')}")
        failed += 0 if result.get("code") == 0 else 1
    cmd_switches(args)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list code tasks on a work item")
    p_list.add_argument("--work-item", required=True)
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="create code tasks for CMDB application CI ids")
    p_create.add_argument("--work-item", required=True)
    p_create.add_argument("--app-id", type=int, action="append", required=True)
    p_create.add_argument("--type", choices=TASK_TYPES, default="story")
    p_create.add_argument("--owner-side", choices=OWNER_SIDES, default="BACKEND")
    p_create.add_argument("--yes", action="store_true")
    p_create.set_defaults(func=cmd_create)

    p_delete = sub.add_parser("delete", help="delete one code task by id (rollback)")
    p_delete.add_argument("--task-id", required=True)
    p_delete.add_argument("--yes", action="store_true")
    p_delete.set_defaults(func=cmd_delete)

    p_branch = sub.add_parser("branch", help="create a requirement branch from a code task")
    p_branch.add_argument("--task-id", required=True)
    p_branch.add_argument("--flow", choices=("iteration", "emergency_requirement"), default="iteration")
    p_branch.add_argument("--yes", action="store_true")
    p_branch.set_defaults(func=cmd_branch)

    p_switches = sub.add_parser("switches", help="show gate / dev CD switches of an iteration branch")
    p_switches.add_argument("--iteration-branch", type=int, required=True)
    p_switches.set_defaults(func=cmd_switches)

    p_skip = sub.add_parser("skip", help="skip promotion gate, dev CD and release_test on an iteration branch")
    p_skip.add_argument("--iteration-branch", type=int, required=True)
    p_skip.add_argument("--no-gates", action="store_true", help="leave promotion gates enforced")
    p_skip.add_argument("--no-dev-cd", action="store_true", help="keep dev CD deployment")
    p_skip.add_argument("--no-auto-pass", action="store_true", help="keep release_test manual")
    p_skip.add_argument("--yes", action="store_true")
    p_skip.set_defaults(func=cmd_skip)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
