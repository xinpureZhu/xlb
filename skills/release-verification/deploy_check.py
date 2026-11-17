#!/usr/bin/env python3
"""Deployment check for a V2 release order (efficiency MCP only).

Verifies order status, per-service target vs actual image digest, and each
branch's workflow position. Runtime RED needs observability-prod separately.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_LABEL = {"prod": "生产", "prod-gray": "灰度", "staging": "预发", "test": "测试", "dev": "开发"}


def context(args):
    cmd = [sys.executable, os.path.join(HERE, "context.py")]
    if args.release_order:
        cmd += ["--release-order", args.release_order]
    else:
        cmd += ["--branch", args.branch]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(r.stderr or r.stdout)
    return json.loads(r.stdout)


def wf_summary(workflows, branch_id):
    w = workflows.get(str(branch_id)) or {}
    b = w.get("branch") or {}
    flow = w.get("workflow") or {}
    return {
        "env": b.get("current_environment") or "-",
        "node": b.get("current_node") or "-",
        "status": b.get("node_status") or "-",
        "updated": b.get("updated_at") or "-",
        "phase": flow.get("phase") or "-",
    }


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--release-order")
    g.add_argument("--branch")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    data = context(a)
    order = data["order"]
    services = []
    for s in data["services"]:
        wf = wf_summary(data["workflows"], s.get("code_branch_id"))
        target_digest = s.get("target_image_digest")
        actual_digest = s.get("actual_image_digest")
        matched = bool(target_digest and actual_digest and target_digest == actual_digest)
        ok = s.get("deploy_status") == "satisfied_by_current_order" and matched
        services.append({
            "service": s.get("target_service_name"),
            "env": s.get("target_environment"),
            "deploy_status": s.get("deploy_status"),
            "digest_match": matched,
            "workflow": wf,
            "ok": ok,
        })

    failed = [s for s in services if not s["ok"]]
    verdict = "PASS" if not failed else "FAIL"
    result = {"order": order, "verdict": verdict, "services": services,
              "note": "运行态 RED 由 runtime_check.py 使用 Grafana 指标验证。"}

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if verdict == "PASS" else 2

    print("## 发布落地核对：%s" % order.get("name"))
    print("")
    print("- 发布单：`%s`" % order.get("id"))
    print("- 状态：%s / 审批 %s" % (order.get("status"), order.get("approval_status")))
    print("- 环境：%s" % ", ".join(order.get("environments") or []))
    print("- 结论：**%s**" % verdict)
    print("")
    print("| 服务 | 环境 | 部署状态 | 镜像一致 | 当前节点 | 节点状态 | 更新 |")
    print("|---|---|---|---|---|---|---|")
    for s in services:
        print("| %s | %s | %s | %s | %s | %s | %s |" % (
            s["service"], s["env"], s["deploy_status"], "是" if s["digest_match"] else "否",
            s["workflow"]["node"], s["workflow"]["status"], s["workflow"]["updated"]))
    print("")
    print("> " + result["note"])
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())