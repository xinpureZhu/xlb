#!/usr/bin/env python3
"""Merged daily inspection for a prod namespace.

One card, one report: actionable issues (alerts, CronJob liveness, CNI, K8s
events, spikes, backlogs, middleware peaks) plus the health snapshot (alerts
diff, 5xx, P99, restarts) — replacing the two earlier reports, one of which lost
its data source when Grafana moved to Feishu SSO.
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "_shared"))

import collect as c  # noqa: E402
import gitops_deps  # noqa: E402
import report as rp  # noqa: E402
import feishu_card as fc  # noqa: E402
from prom import GrafanaAuthError, GrafanaError, Prom  # noqa: E402

OUT_DIR = os.path.expanduser("~/.xlb/reports/daily")
USER_ID = "ou_5d3dc8766fa70f73a8c0ace68cbbb565"


def build_report(ns, skip_cloud=False):
    p = Prom()
    if not p.num("count(up)"):
        raise GrafanaError("Prometheus returned no series")
    k = c.k8s_summary(p, ns)
    mongo = redis = mq = kafka = events = backlog = []
    cloud_ok = False
    if skip_cloud:
        c.CLOUD_SKIPPED.append("--skip-cloud")
        c.WARN.append("云侧检查已跳过（--skip-cloud）：MongoDB/Redis/MQ/K8s 事件未检查")
    else:
        cloud_ok = c.preflight_cloud()
        if cloud_ok:
            sys.stderr.write("[cloud] MongoDB / Redis / RocketMQ / Kafka / SLS events\n")
            mongo = c.mongo_health(ns)
            redis = c.redis_health(ns)
            deps = gitops_deps.collect(ns)
            mq = c.mq_lag(ns, deps["groups"])
            kafka = c.kafka_lag(p, ns, deps)
            backlog = sorted((c.kafka_backlog_peak(deps["kafka"]) or {}).values(),
                             key=lambda x: -x["peak"])
            events = c.k8s_events(ns)
    d = rp.build(ns, k, mongo, redis, mq, kafka, events, backlog)
    # Categories whose check never ran are "unknown", not "resolved": carrying
    # them over stops a broken credential from reading as an overnight recovery.
    unverified = set()
    if not cloud_ok:
        unverified |= {"积压", "MongoDB", "Redis", "事件"}
    if not d["events_ok"]:
        unverified.add("事件")
    return d, tuple(unverified)


def send_card(d, date, ns, key=None):
    info = fc.send(rp.render_card(d), USER_ID,
                   key or "xlb-daily-%s-%s" % (ns, date))
    print("[sent] message_id=%s" % info.get("message_id"))


def send_failure(ns, err, date):
    """A cron that dies silently looks exactly like a healthy morning."""
    payload = fc.card("%s 巡检未执行" % ns, "red", [
        fc.md("**巡检脚本未能生成报告**\n%s" % str(err)[:300]),
        fc.note("幂等键 %s-fail；修复后手动重跑 run.sh 即可补发当日卡片"
                % ("xlb-daily-%s-%s" % (ns, date))),
    ])
    try:
        info = fc.send(payload, USER_ID, "xlb-daily-%s-%s-fail" % (ns, date))
        print("[sent] failure notice message_id=%s" % info.get("message_id"))
    except RuntimeError as e:
        print("[fail] notify: %s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="goods")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--skip-cloud", action="store_true")
    ap.add_argument("--print-card", action="store_true")
    ap.add_argument("--date", default=dt.datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--idempotency-key",
                    help="覆盖默认幂等键；当天已用默认键发过卡片时补发用")
    a = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        d, unverified = build_report(a.namespace, a.skip_cloud)
    except GrafanaAuthError as e:
        print("[preflight] %s" % e)
        if not a.no_send:
            send_failure(a.namespace, e, a.date)
        return 2
    except GrafanaError as e:
        print("[preflight] %s" % e)
        if not a.no_send:
            send_failure(a.namespace, e, a.date)
        return 1

    # Only the authoritative daily run persists streaks; a preview works off a
    # different data window and would corrupt the recorded history.
    d = rp.mark_recurrence(d, persist=not a.no_send, unverified=unverified)
    text = rp.render_text(d)
    # A preview must not overwrite the report the daily run actually sent.
    path = os.path.join(OUT_DIR, "%s-%s%s.md"
                        % (a.namespace, a.date, ".preview" if a.no_send else ""))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("\n[saved] %s" % path)
    if a.print_card:
        print(json.dumps(rp.render_card(d), ensure_ascii=False, indent=2))
    if a.no_send:
        print("[skip] send disabled")
        return 0
    send_card(d, a.date, a.namespace, a.idempotency_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())