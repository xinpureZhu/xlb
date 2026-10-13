#!/usr/bin/env python3
"""CodeUp 代码仓成员权限增删查（CMDB 维度接口，不经 app-service 归属校验）。

对应后端路由 /api/v1/codeup/repo-permissions/{query,add,remove}，只需登录态，
按 repo_id / cmdb_id 直接操作，适用于未绑定应用服务的仓库。凭据从 cookie 文件读取，
不打印、不入库。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://z.xlbsoft.com/api/efficiency"
DEFAULT_COOKIE_FILE = "/private/tmp/z_cookie.txt"

# role_type -> CodeUp accessLevel / 角色，仅用于展示确认
ROLE_HINT = {
    "owner": "40 Maintainer",
    "admin": "40 Maintainer",
    "member": "30 Developer",
    "viewer": "20 Reporter",
}


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


def _targets(args):
    """返回 (repo_ids, cmdb_ids)，至少一个非空。"""
    repo_ids = args.repo_id or []
    cmdb_ids = [int(x) for x in (args.cmdb_id or [])]
    if not repo_ids and not cmdb_ids:
        sys.exit("pass at least one --repo-id or --cmdb-id")
    return repo_ids, cmdb_ids


def cmd_query(args):
    repo_ids, cmdb_ids = _targets(args)
    body = {}
    if repo_ids:
        body["repo_ids"] = repo_ids
    if cmdb_ids:
        body["cmdb_ids"] = cmdb_ids
    result = post("/api/v1/codeup/repo-permissions/query", body)
    if result.get("code") != 200:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    members = (result.get("data") or {}).get("members") or []
    print(f"total={len(members)}")
    for m in members:
        # role \t name \t codeup_user_id \t username
        print("\t".join((m.get("role", ""), m.get("name", ""),
                          m.get("user_id", ""), m.get("username", ""))))
    return 0


def _build_rule(args):
    repo_ids, cmdb_ids = _targets(args)
    rule = {"role_type": args.role}
    if repo_ids:
        rule["repo_ids"] = repo_ids
    if cmdb_ids:
        rule["cmdb_ids"] = cmdb_ids
    if args.user_id:
        rule["user_ids"] = args.user_id
    if args.user_name:
        rule["user_names"] = args.user_name
    if args.user_email:
        rule["user_emails"] = args.user_email
    if not (args.user_id or args.user_name or args.user_email):
        sys.exit("pass at least one --user-id / --user-name / --user-email")
    return rule


def _run_mutation(action, args):
    rule = _build_rule(args)
    payload = {"rules": [rule]}
    if not args.yes:
        print(f"dry-run ({action}), re-run with --yes to submit:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if action == "add":
            print(f"# role_type={args.role} -> accessLevel {ROLE_HINT.get(args.role, '?')}")
            print("# NOTE: add 对已存在成员会改角色（可能提权/降权），先 query 核对")
        return 0
    result = post(f"/api/v1/codeup/repo-permissions/{action}", payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("code") != 200:
        return 1
    failed = 0
    for r in (result.get("data") or {}).get("results") or []:
        failed += r.get("failed_count") or 0
    return 1 if failed else 0


def cmd_add(args):
    return _run_mutation("add", args)


def cmd_remove(args):
    return _run_mutation("remove", args)


def _add_target_args(p):
    p.add_argument("--repo-id", action="append", help="repeatable; CodeUp 数字仓库 ID")
    p.add_argument("--cmdb-id", action="append", help="repeatable; CMDB Repository CI ID（后端按关系解析 repo_id）")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_query = sub.add_parser("query", help="列出仓库现有成员（role/name/codeup_user_id/username）")
    _add_target_args(p_query)
    p_query.set_defaults(func=cmd_query)

    p_add = sub.add_parser("add", help="添加成员或改角色（幂等：已存在则改角色）")
    _add_target_args(p_add)
    p_add.add_argument("--user-id", action="append",
                       help="repeatable；轻舟 sys_user ObjectID（按 email/name 匹配 CodeUp）或直接 CodeUp userId")
    p_add.add_argument("--user-name", action="append", help="repeatable；CodeUp 用户名或显示名")
    p_add.add_argument("--user-email", action="append", help="repeatable；用户邮箱")
    p_add.add_argument("--role", choices=["owner", "admin", "member", "viewer"], default="member")
    p_add.add_argument("--yes", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help="移除成员（--user-id 必须是 query 输出里的 CodeUp userId）")
    _add_target_args(p_rm)
    p_rm.add_argument("--user-id", action="append", help="repeatable；CodeUp userId（不做 sys_users 解析）")
    p_rm.add_argument("--user-name", action="append", help="repeatable；CodeUp 用户名或显示名")
    p_rm.add_argument("--user-email", action="append", help="repeatable；用户邮箱")
    p_rm.add_argument("--role", default="member", help="占位，remove 时后端忽略角色")
    p_rm.add_argument("--yes", action="store_true")
    p_rm.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
