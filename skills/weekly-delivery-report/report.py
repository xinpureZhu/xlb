#!/usr/bin/env python3
"""Weekly delivery report (read-only).

Pulls the authenticated user's requirements, code tasks and release orders from
the Efficiency MCP server, then writes Markdown + JSON. Optionally sends the
Markdown as a Feishu bot message.

Never writes to Efficiency, git, or the cluster.
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_shared"))
from efficiency_client import Client, McpError  # noqa: E402
import feishu_card as fc  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))
DEFAULT_OUT = os.path.expanduser("~/.xlb/reports/weekly")
DEFAULT_USER = "ou_5d3dc8766fa70f73a8c0ace68cbbb565"
TASK_HEADER = re.compile(r"^--- Task \d+ ---$", re.M)
SHOWING = re.compile(r"showing \d+ of (\d+) total")


def now():
    return dt.datetime.now(CST)


def parse_tasks(text):
    total = 0
    m = SHOWING.search(text or "")
    if m:
        total = int(m.group(1))
    tasks = []
    blocks = TASK_HEADER.split(text or "")
    for block in blocks[1:]:
        task = {}
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            key, _, value = line[2:].partition(": ")
            if key in ("iterationBranches",):
                continue
            task[key.strip()] = value.strip()
        if task.get("TaskID"):
            tasks.append(task)
    return total, tasks


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def requirement_rows(items):
    rows = []
    for item in items:
        status = item.get("status") or ""
        if status.startswith("{"):
            try:
                status = json.loads(status).get("state_key", "unknown")
            except Exception:
                status = "unknown"
        if status == "closed":
            status = "已关闭"
        rows.append({
            "product_line": item.get("product_line") or "-",
            "system": item.get("system") or "-",
            "title": item.get("title") or "-",
            "status": status or "unknown",
            "work_item_id": item.get("work_item_id") or "",
            "url": item.get("feishu_url") or "",
        })
    return rows


def enrich_branch_updates(client, tasks):
    """Attach the last workflow update so old failures are not read as new."""
    cache = {}
    for task in tasks:
        bid = task.get("BranchID")
        if not bid:
            continue
        if bid not in cache:
            try:
                data = client.call_json("query_workflow_status", {"branch_id": str(bid)})
                cache[bid] = ((data.get("branch") or {}).get("updated_at") or "") if isinstance(data, dict) else ""
            except Exception:
                cache[bid] = ""
        task["BranchUpdatedAt"] = cache[bid]


def build_report(days):
    client = Client(timeout=90)
    end = now()
    start = end - dt.timedelta(days=days)

    req = client.call_json("query_my_requirements", {"page_size": 100})
    req_items = req.get("items", []) if isinstance(req, dict) else []
    req_total = req.get("total", len(req_items)) if isinstance(req, dict) else len(req_items)

    task_text = client.call("query_my_code_tasks", {"page_size": 100, "archive_status": "all"})
    task_total, tasks = parse_tasks(task_text)

    orders = client.call_json("query_release_orders", {
        "created_by_me": "true",
        "page_size": 100,
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
    })
    order_items = []
    if isinstance(orders, dict):
        order_items = (orders.get("data") or {}).get("items") or []

    rows = requirement_rows(req_items)
    by_line = collections.Counter(r["product_line"] for r in rows)
    by_status = collections.Counter(r["status"] for r in rows)

    failed = [t for t in tasks if (t.get("NodeStatus") or "").lower() == "failed"]
    pending_merge = [t for t in tasks if (t.get("MergeStatus") or "").lower() == "pending"]
    enrich_branch_updates(client, failed)
    active_orders = []
    for o in order_items:
        created = parse_iso(o.get("created_at"))
        if created and created < start:
            continue
        active_orders.append(o)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "requirements": {"total": req_total, "sample": len(rows), "by_product_line": dict(by_line),
                         "by_status": dict(by_status), "rows": rows},
        "code_tasks": {"total": task_total, "sample": len(tasks), "failed": failed,
                       "pending_merge": pending_merge, "rows": tasks},
        "release_orders": {"total": len(active_orders), "rows": active_orders},
        "gaps": [
            "需求状态为平台 state_key，未映射为中文状态名；按原值统计。",
            "待审批接口是全局数据（约 984 条），无法按人归属，本报告不统计。",
            "代码任务是当前状态快照，不是本周新增；失败节点带最后更新时间，可据此判断是否历史遗留。",
        ],
    }


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def render_md(data):
    p = data["period"]
    req = data["requirements"]
    ct = data["code_tasks"]
    ro = data["release_orders"]
    lines = []
    lines.append("# 交付周报 %s ~ %s" % (p["start"][:10], p["end"][:10]))
    lines.append("")
    lines.append("生成时间：%s（只读数据：Efficiency MCP）" % now().strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append(md_table(["指标", "数值"], [
        ["名下需求", req["total"]],
        ["代码任务", ct["total"]],
        ["失败节点（当前快照）", len(ct["failed"])],
        ["待合并", len(ct["pending_merge"])],
        ["周期内发布单", ro["total"]],
    ]))
    lines.append("")
    lines.append("## 需求分布")
    lines.append("")
    if req["by_product_line"]:
        lines.append(md_table(["产品线", "数量"],
                              sorted(req["by_product_line"].items(), key=lambda x: -x[1])))
    else:
        lines.append("无数据。")
    lines.append("")
    lines.append("## 失败节点（按 branch 当前状态）")
    lines.append("")
    if ct["failed"]:
        lines.append(md_table(
            ["服务", "分支", "环境", "节点", "最后更新", "标题"],
            [[t.get("Application", "-"), t.get("BranchName", "-"), t.get("Environment", "-"),
              t.get("CurrentNode", "-"), t.get("BranchUpdatedAt", "-") or "-",
              (t.get("WorkItemTitle") or "-")[:40]]
             for t in ct["failed"][:15]]))
    else:
        lines.append("无。")
    lines.append("")
    lines.append("## 周期内发布单")
    lines.append("")
    if ro["rows"]:
        lines.append(md_table(
            ["名称", "状态", "环境", "服务数", "创建时间"],
            [[o.get("name", "-"), o.get("status_text", o.get("status", "-")),
              ",".join(o.get("target_environments") or []), o.get("service_count", "-"),
              (o.get("created_at") or "")[:16]]
             for o in ro["rows"][:15]]))
    else:
        lines.append("无。")
    lines.append("")
    lines.append("## 数据口径与缺口")
    lines.append("")
    for gap in data["gaps"]:
        lines.append("- " + gap)
    lines.append("")
    return "\n".join(lines)


def _short(value, n):
    value = str(value or "-")
    return value if len(value) <= n else value[:n - 1] + "…"


def build_card(data):
    p = data["period"]
    req = data["requirements"]
    ct = data["code_tasks"]
    ro = data["release_orders"]
    elements = [fc.fields([
        ("需求", str(req["total"])),
        ("代码任务", str(ct["total"])),
        ("失败节点", ("🔴 %d" % len(ct["failed"])) if ct["failed"] else "✅ 0"),
        ("发布单", str(ro["total"])),
    ]), fc.hr()]

    lines = ["**需求分布**"]
    for name, n in sorted(req["by_product_line"].items(), key=lambda x: -x[1])[:6]:
        lines.append("• %s · %d" % (name, n))
    elements.append(fc.md("\n".join(lines)))

    elements.append(fc.hr())
    if ct["failed"]:
        groups = collections.defaultdict(list)
        for t in ct["failed"]:
            groups[t.get("WorkItemTitle") or "-"].append(t)
        lines = ["**失败节点（当前快照）**"]
        for title, items in list(groups.items())[:2]:
            envs = sorted({i.get("Environment") or "-" for i in items})
            nodes = sorted({i.get("CurrentNode") or "-" for i in items})
            last = max((i.get("BranchUpdatedAt") or "") for i in items)
            lines.append("🔴 **%s**（%d 个服务）" % (_short(title, 30), len(items)))
            names = [i.get("Application") or "-" for i in items]
            for k in range(0, min(len(names), 6), 2):
                lines.append("　" + "、".join(names[k:k + 2]))
            if len(names) > 6:
                lines.append("　…另 %d 个服务" % (len(names) - 6))
            lines.append("　%s · %s · %s" % (",".join(envs), "/".join(nodes), last[:16] or "-"))
        if len(groups) > 2:
            lines.append("…另 %d 个工单" % (len(groups) - 2))
    else:
        lines = ["**失败节点**", "✅ 无失败节点"]
    elements.append(fc.md("\n".join(lines)))

    elements.append(fc.hr())
    lines = ["**周期内发布单**"]
    if ro["rows"]:
        for o in ro["rows"][:5]:
            status = o.get("status_text") or str(o.get("status"))
            mark = "🟢" if status == "成功" else ("🔴" if status == "失败" else "⚪")
            lines.append("%s %s · %s · %s · %s 服务 · %s" % (
                mark, _short(o.get("name"), 34), status,
                ",".join(o.get("target_environments") or []),
                o.get("service_count", "-"), (o.get("created_at") or "")[:10]))
    else:
        lines.append("无。")
    elements.append(fc.md("\n".join(lines)))

    elements.append(fc.hr())
    elements.append(fc.note("口径：需求状态用平台 state_key；待审批为全局数据不统计；代码任务为当前快照。"))
    elements.append(fc.note("生成 %s · 只读 Efficiency MCP" % now().strftime("%Y-%m-%d %H:%M")))
    return fc.card("交付周报 · %s ~ %s" % (p["start"][:10], p["end"][:10]), "blue", elements)


def send_lark(card, user_id, key):
    try:
        return fc.send(card, user_id, key)
    except RuntimeError as e:
        raise SystemExit("发送失败: %s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--send", action="store_true", help="发送飞书机器人消息")
    ap.add_argument("--user-id", default=DEFAULT_USER)
    ap.add_argument("--idempotency-key")
    a = ap.parse_args()

    data = build_report(a.days)
    md = render_md(data)
    os.makedirs(a.out, exist_ok=True)
    stamp = now().strftime("%Y%m%d")
    base = os.path.join(a.out, "weekly-%s" % stamp)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(md)
    print("\n[saved] %s.md / %s.json" % (base, base))

    if a.send:
        key = a.idempotency_key or "xlb-weekly-%s" % now().strftime("%Y-W%V")
        info = send_lark(build_card(data), a.user_id, key)
        print("[sent] message_id=%s key=%s" % (info.get("message_id"), key))


if __name__ == "__main__":
    try:
        main()
    except McpError as e:
        raise SystemExit("Efficiency MCP error: %s" % e)