#!/usr/bin/env python3
"""轻舟应用服务（AppService）配置读写的最小客户端。

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

UPDATABLE = {
    "name": str,
    "description": str,
    "techStack": str,
    "templateType": str,
    "baseBranch": str,
    "moduleCiId": int,
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


def fetch_detail(args):
    body = {"serviceId": args.service_id} if args.service_id else {"appCode": args.app_code}
    return post("/api/v1/app-services/detail", body)


def summarize(result):
    data = result.get("data") or {}
    service = data.get("service") or {}
    org = data.get("organization") or {}
    perms = data.get("permissions") or {}
    print(
        json.dumps(
            {
                "id": service.get("id"),
                "appCode": service.get("appCode"),
                "namespace": service.get("namespace"),
                "techStack": service.get("techStack"),
                "templateType": service.get("templateType"),
                "baseBranch": data.get("baseBranch"),
                "lifecycle": service.get("lifecycle"),
                "source": service.get("source"),
                "applicationCiId": service.get("applicationCiId"),
                "moduleId": org.get("moduleId"),
                "canEdit": perms.get("canEdit"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_detail(args):
    result = fetch_detail(args)
    if result.get("code") != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    summarize(result)
    return 0


def resolve_repository_id(service_id, explicit):
    if explicit:
        return explicit
    result = post("/api/v1/app-services/detail", {"serviceId": service_id})
    if result.get("code") != 0:
        sys.exit(f"detail failed for service {service_id}: {result.get('msg')}")
    repos = (result.get("data") or {}).get("repositories") or []
    if not repos:
        sys.exit(f"service {service_id} has no bound repository; fix the CMDB uses_code relation first")
    if len(repos) > 1:
        listed = ", ".join(f"{r.get('repoId')}({r.get('name')})" for r in repos)
        sys.exit(f"service {service_id} has multiple repositories, pass --repository-id: {listed}")
    return repos[0].get("repoId")


def cmd_list(args):
    payload = {"page": args.page, "pageSize": args.page_size, "lifecycle": args.lifecycle or ""}
    if args.keyword:
        payload["keyword"] = args.keyword
    if args.namespace:
        payload["namespace"] = args.namespace
    result = post("/api/v1/app-services/list", payload)
    if result.get("code") != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    data = result.get("data") or {}
    print(f"total={data.get('total')}")
    for item in data.get("list") or []:
        service = item.get("service") or {}
        print(
            "\t".join(
                str(value)
                for value in (
                    service.get("id"),
                    service.get("appCode"),
                    service.get("namespace"),
                    service.get("techStack") or "-",
                    service.get("lifecycle"),
                )
            )
        )
    return 0


def cmd_repo_perms(args):
    repository_id = resolve_repository_id(args.service_id, args.repository_id)
    body = {"serviceId": args.service_id, "repositoryId": repository_id}
    if args.action == "query":
        result = post("/api/v1/app-services/codeup/repo-permissions/query", body)
        if result.get("code") != 0:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        for member in (result.get("data") or {}).get("members") or []:
            print("\t".join((member.get("role", ""), member.get("name", ""), member.get("user_id", ""))))
        return 0

    if not args.user_id and not args.user_name:
        sys.exit("pass at least one --user-id or --user-name")
    body["roleType"] = args.role
    if args.user_id:
        body["userIds"] = args.user_id
    if args.user_name:
        body["userNames"] = args.user_name
    if not args.yes:
        print(f"dry-run ({args.action}), re-run with --yes to submit:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    result = post(f"/api/v1/app-services/codeup/repo-permissions/{args.action}", body)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("code") != 0:
        return 1
    for rule in (result.get("data") or {}).get("results") or []:
        if rule.get("failed_count"):
            return 1
    return 0


def cmd_stacks(args):
    result = post("/api/v1/ci-templates/tech-stacks", {})
    for option in result.get("data") or []:
        print(option.get("techStack"), "->", option.get("defaultTemplateName") or "(no default template)")
    return 0 if result.get("code") == 0 else 1


def cmd_update(args):
    payload = {"serviceId": args.service_id}
    for field, caster in UPDATABLE.items():
        value = getattr(args, _snake(field), None)
        if value is not None:
            payload[field] = caster(value)
    if len(payload) == 1:
        sys.exit("nothing to update: pass at least one field, e.g. --tech-stack node")
    if not args.yes:
        print("dry-run, re-run with --yes to submit:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    result = post("/api/v1/app-services/update", payload)
    print(f"code={result.get('code')} msg={result.get('msg')}")
    if result.get("code") != 0:
        return 1
    summarize(post("/api/v1/app-services/detail", {"serviceId": args.service_id}))
    return 0


def _snake(name):
    out = []
    for char in name:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_detail = sub.add_parser("detail", help="show config of one app service")
    target = p_detail.add_mutually_exclusive_group(required=True)
    target.add_argument("--service-id", type=int)
    target.add_argument("--app-code", help="ambiguous across namespaces; verify namespace in output")
    p_detail.set_defaults(func=cmd_detail)

    p_stacks = sub.add_parser("stacks", help="list tech stacks and their default CI templates")
    p_stacks.set_defaults(func=cmd_stacks)

    p_list = sub.add_parser("list", help="search app services")
    p_list.add_argument("--keyword", help="fuzzy match on app_code or name")
    p_list.add_argument("--namespace")
    p_list.add_argument("--lifecycle", default="")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_perms = sub.add_parser("repo-perms", help="query/add/remove members on the bound CodeUp repository")
    p_perms.add_argument("action", choices=["query", "add", "remove"])
    p_perms.add_argument("--service-id", type=int, required=True)
    p_perms.add_argument("--repository-id", help="omit when the service has exactly one bound repository")
    p_perms.add_argument("--user-id", action="append", help="repeatable; add=轻舟 sys_user id, remove=CodeUp user_id")
    p_perms.add_argument("--user-name", action="append", help="repeatable; CodeUp username or display name")
    p_perms.add_argument("--role", choices=["owner", "admin", "member", "viewer"], default="member")
    p_perms.add_argument("--yes", action="store_true")
    p_perms.set_defaults(func=cmd_repo_perms)

    p_update = sub.add_parser("update", help="partial update of app service config")
    p_update.add_argument("--service-id", type=int, required=True)
    p_update.add_argument("--tech-stack")
    p_update.add_argument("--template-type")
    p_update.add_argument("--base-branch")
    p_update.add_argument("--name")
    p_update.add_argument("--description")
    p_update.add_argument("--module-ci-id", type=int)
    p_update.add_argument("--yes", action="store_true")
    p_update.set_defaults(func=cmd_update)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
