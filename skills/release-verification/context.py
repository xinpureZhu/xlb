#!/usr/bin/env python3
"""Build the release-verification context: order, services and time windows.

Read-only. Emits JSON consumed by verify.sh and the release-verification skill.
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_shared"))
from efficiency_client import Client, McpError  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def iso(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_order(client, release_order_id=None, branch_name=None):
    if release_order_id:
        return release_order_id
    data = client.call_json("query_release_orders", {
        "branch_name": branch_name, "page_size": 20, "schema_version": 2,
    })
    items = ((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else []
    if not items:
        raise SystemExit("no V2 release order found for branch %s" % branch_name)
    return items[0]["release_order_id"]


def build(client, order_id):
    payload = client.call_json("get_release_order_detail", {"release_order_id": order_id})
    order = (payload.get("data") or {}).get("order") if isinstance(payload, dict) else None
    if not order:
        raise SystemExit("release order %s has no detail" % order_id)

    t0 = parse_iso(order.get("submitted_at")) or parse_iso(order.get("release_time"))
    if not t0:
        raise SystemExit("release order has no submitted_at/release_time")
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=CST)

    windows = {
        "baseline": {"start": iso(t0 - dt.timedelta(minutes=60)), "end": iso(t0)},
        "plus_15m": {"start": iso(t0), "end": iso(t0 + dt.timedelta(minutes=15))},
        "plus_60m": {"start": iso(t0), "end": iso(t0 + dt.timedelta(minutes=60))},
    }

    services = []
    branch_ids = set()
    for item in order.get("service_list") or []:
        bid = item.get("code_branch_id")
        if bid:
            branch_ids.add(bid)
        services.append({
            "target_service_name": item.get("target_service_name") or item.get("name"),
            "source_service_name": item.get("source_service_name"),
            "namespace": item.get("namespace"),
            "target_environment": item.get("target_environment"),
            "code_branch_id": bid,
            "deploy_status": (item.get("execution_view") or {}).get("status"),
            "actual_image": (item.get("execution_view") or {}).get("actual_image"),
            "target_image": (item.get("execution_view") or {}).get("target_image"),
            "actual_image_digest": (item.get("execution_view") or {}).get("actual_image_digest"),
            "target_image_digest": (item.get("execution_view") or {}).get("target_image_digest"),
        })

    workflows = {}
    for bid in sorted(branch_ids):
        try:
            workflows[str(bid)] = client.call_json("query_workflow_status", {"branch_id": str(bid)})
        except McpError as e:
            workflows[str(bid)] = {"error": str(e)}

    return {
        "order": {
            "id": order.get("id"),
            "name": order.get("name"),
            "submitted_at": order.get("submitted_at"),
            "status": order.get("status"),
            "approval_status": order.get("approval_status"),
            "environments": order.get("environments"),
            "work_item_ids": order.get("work_item_ids"),
            "release_time": order.get("release_time"),
        },
        "windows": windows,
        "services": services,
        "workflows": workflows,
        "notes": [
            "windows 为 UTC；baseline = 发布前 60 分钟，plus_15m/plus_60m = 发布后窗口。",
            "workflows 为当前状态快照，不是发布时状态。",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--release-order", help="V2 release order ObjectID")
    group.add_argument("--branch", help="exact iteration branch name")
    ap.add_argument("--out", help="write JSON to this path instead of stdout")
    a = ap.parse_args()

    client = Client(timeout=90)
    order_id = find_order(client, a.release_order, a.branch)
    data = build(client, order_id)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(a.out)
    else:
        print(text)


if __name__ == "__main__":
    try:
        main()
    except McpError as e:
        raise SystemExit("Efficiency MCP error: %s" % e)