#!/usr/bin/env python3
"""Behavioral tests for the merged inspection: each test exercises the failure
path the fix targets, using stubs (no network/cloud calls).

Run: python3 skills/daily-inspection-mcp/test_inspection.py
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collect as c
import prom
import report as rp

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond), detail))
    print("%s %-52s %s" % ("PASS" if cond else "FAIL", name, detail))


class StubProm:
    """Records nothing, answers by pattern; the collectors only use these four."""

    def __init__(self, q_fn=None, by_fn=None, num_fn=None, range_fn=None, sum_fn=None):
        self.q_fn = q_fn or (lambda e, at="now": [])
        self.by_fn = by_fn or (lambda e, k, at="now": {})
        self.num_fn = num_fn or (lambda e, at="now", default=0.0: default)
        self.range_fn = range_fn or (lambda e, s, en, step=300: [])
        self.sum_fn = sum_fn or (lambda e, at="now": 0.0)

    def q(self, expr, at="now"):
        return self.q_fn(expr, at)

    def by(self, expr, keys, at="now"):
        return self.by_fn(expr, keys, at)

    def num(self, expr, at="now", default=0.0):
        return self.num_fn(expr, at, default)

    def range(self, expr, start, end, step=300):
        return self.range_fn(expr, start, end, step)

    def sum(self, expr, at="now"):
        return self.sum_fn(expr, at)


def blank_k(**over):
    k = {"alerts": 0, "alerts_prev": 0, "new": [], "gone": [], "rules": {},
         "svc": [], "stab": {"restart": 0, "oom": 0}, "peak": {}, "spikes": [],
         "notready": [], "red": {"bad": 0, "all": 0, "ratio": None, "sampled": False,
                                 "p99": []},
         "series": 100, "cron": [], "no_ip": []}
    k.update(over)
    return k


# ------------------------------------------------------------------ 1. redis pagination
def test_redis_pagination():
    def fake_aliyun(args, timeout=120):
        if args[:2] != ["r-kvstore", "DescribeInstances"]:
            return {}
        page = int(args[args.index("--PageNumber") + 1])
        if page == 1:
            insts = [{"InstanceId": "r-%03d" % i, "InstanceName": "prod--ns%d--redis" % i}
                     for i in range(100)]
        else:
            insts = [{"InstanceId": "r-page2", "InstanceName": "prod--retail-bff--goods-redis"}]
        return {"TotalCount": 101, "Instances": {"KVStoreInstance": insts}}

    orig = c.aliyun
    c.aliyun = fake_aliyun
    try:
        got = c._redis_instances()
    finally:
        c.aliyun = orig
    names = [i["InstanceName"] for i in got]
    check("redis: paginates past page 1", len(got) == 101, "got=%d" % len(got))
    check("redis: page-2 instance included", "prod--retail-bff--goods-redis" in names)


# ------------------------------------------------------------------ 2. alert false-recovery
def test_alert_failure_protection():
    prom.WARN.clear()
    calls = {"n": 0}

    def fake_alert_instances(p, ns, at):
        calls["n"] += 1
        if calls["n"] == 1:                     # `now` query fails
            prom.WARN.append("query failed: simulated SSL EOF")
            return {}
        return {("sig-%d" % i,): ("SomeAlert", "warning", "goods", "pod-x")
                for i in range(24)}             # 24 alerts yesterday

    orig_ai, orig_tp, orig_ss = c.alert_instances, c.traffic_peak, c.service_spikes
    c.alert_instances = fake_alert_instances
    c.traffic_peak = lambda p, ns: {}
    c.service_spikes = lambda p, ns, hours=24: []
    try:
        k = c.k8s_summary(StubProm(), "goods")
    finally:
        c.alert_instances, c.traffic_peak, c.service_spikes = orig_ai, orig_tp, orig_ss
    check("alerts: failed query hides false recovery",
          k["alerts"] == 0 and k["new"] == [] and k["gone"] == [],
          "alerts=%d new=%d gone=%d" % (k["alerts"], len(k["new"]), len(k["gone"])))

    prom.WARN.clear()

    def ok_alert_instances(p, ns, at):
        if at == "now":
            return {("a",): ("A", "warning", "goods", "x")}
        return {("a",): ("A", "warning", "goods", "x"),
                ("b",): ("B", "warning", "goods", "y")}

    c.alert_instances = ok_alert_instances
    c.traffic_peak = lambda p, ns: {}
    c.service_spikes = lambda p, ns, hours=24: []
    try:
        k2 = c.k8s_summary(StubProm(), "goods")
    finally:
        c.alert_instances, c.traffic_peak, c.service_spikes = orig_ai, orig_tp, orig_ss
    check("alerts: healthy diff still computed",
          len(k2["gone"]) == 1 and k2["alerts"] == 1)


# ------------------------------------------------------------------ 3. cronjob liveness
def test_cronjob_health():
    jobs = [{"name": "resident-job", "schedule": "* * * * *", "resident": True},
            {"name": "daily-job", "schedule": "0 3 * * *", "resident": False}]
    orig_jobs = c.cronjobs_from_manifest
    c.cronjobs_from_manifest = lambda ns, root="repos/gitops/apps": jobs
    try:
        def by_no_running(expr, keys, at="now"):
            if 'phase="Running"' in expr:
                return {}
            if "kube_pod_start_time" in expr:
                return {("resident-job-1-abcde",): 1000.0}
            return {}

        out = c.cronjob_health(StubProm(by_fn=by_no_running), "goods")
        r = next(j for j in out if j["name"] == "resident-job")
        check("cronjob: non-Running pod flagged dead", r["alive"] is False, r["detail"])

        def by_running(expr, keys, at="now"):
            if 'phase="Running"' in expr:
                return {("resident-job-1-abcde",): 1}
            if "kube_pod_start_time" in expr:
                return {("resident-job-1-abcde",): time.time() - 5 * 86400}
            return {}

        r2 = next(j for j in c.cronjob_health(StubProm(by_fn=by_running), "goods")
                  if j["name"] == "resident-job")
        check("cronjob: Running pod flagged alive",
              r2["alive"] is True and "5 天" in r2["detail"], r2["detail"])

        d = next(j for j in c.cronjob_health(StubProm(), "goods")
                 if j["name"] == "daily-job")
        check("cronjob: periodic without recent Job flagged",
              d["alive"] is False, d["detail"])

        d4 = next(j for j in c.cronjob_health(
            StubProm(num_fn=lambda e, at="now", default=0.0: 1.0), "goods")
            if j["name"] == "daily-job")
        check("cronjob: periodic with recent Job alive", d4["alive"] is True)

        # REGRESSION (2026-09-07): a pod WAS created, so a kube_pod_created
        # check says alive — but it never started. CNI failure left 17 cron
        # pods wedged in ContainerCreating with no IP for hours while the
        # business endpoint silently stopped being called.
        def by_wedged(expr, keys, at="now"):
            if "waiting_reason" in expr:
                return {("daily-job-9-x", "ContainerCreating"): 1}
            if "kube_pod_created" in expr:
                return {("daily-job-9-x",): 4 * 3600}
            return {}

        w = next(j for j in c.cronjob_health(
            StubProm(by_fn=by_wedged, num_fn=lambda e, at="now", default=0.0: 1.0),
            "goods") if j["name"] == "daily-job")
        check("cronjob: created-but-wedged pod flagged dead",
              w["alive"] is False and "ContainerCreating" in w["detail"], w["detail"])

        def by_starting(expr, keys, at="now"):
            if "waiting_reason" in expr:
                return {("daily-job-9-y", "ContainerCreating"): 1}
            if "kube_pod_created" in expr:
                return {("daily-job-9-y",): 60}        # only 1 min old
            return {}

        s = next(j for j in c.cronjob_health(
            StubProm(by_fn=by_starting, num_fn=lambda e, at="now", default=0.0: 1.0),
            "goods") if j["name"] == "daily-job")
        check("cronjob: briefly-starting pod not flagged", s["alive"] is True, s["detail"])
    finally:
        c.cronjobs_from_manifest = orig_jobs


# ------------------------------------------------------------------ 4. severity
def test_build_red_on_dead_cron():
    k = blank_k(cron=[{"name": "resident-job", "schedule": "* * * * *",
                       "resident": True, "alive": False, "detail": "无 Running Pod"}])
    d = rp.build("goods", k, [], [], [], [], [], [])
    check("build: dead CronJob => red + issue row",
          d["level"] == "red" and any(cat == "CronJob" for cat, _ in d["issues"]),
          "level=%s" % d["level"])

    k2 = blank_k(notready=["goods-6cf4648b9-htrks"])
    d2 = rp.build("goods", k2, [], [], [], [], [], [])
    check("build: NotReady names the pod, stays orange",
          d2["level"] == "orange"
          and any(cat == "稳定性" and "goods-6cf4648b9-htrks" in t
                  for cat, t in d2["issues"]), d2["level"])


# ------------------------------------------------------------------ 5. recurrence
def test_mark_recurrence():
    state = tempfile.mkdtemp()
    today = c.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    issues = [("突刺", "`erp-store-sync` P99 峰值 29.55s"),
              ("积压", "`prod_erp_stock_detail` 中位 71")]

    d1 = rp.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True,
                            state_dir=state)
    check("recurrence: first day has no tag",
          not any("已连续" in t for _, t in d1["issues"]))

    spath = os.path.join(state, "t.recurrence.json")
    prev = json.load(open(spath))
    json.dump({"date": yesterday, "keys": prev["keys"]}, open(spath, "w"))
    d2 = rp.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True,
                            state_dir=state)
    check("recurrence: second day tagged 已连续 2 天",
          all("已连续 2 天" in t for _, t in d2["issues"]), str(d2["issues"]))

    d3 = rp.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True,
                            state_dir=state)
    check("recurrence: same-day rerun keeps streak 2",
          all("已连续 2 天" in t for _, t in d3["issues"]))

    d4 = rp.mark_recurrence({"ns": "t", "issues": [("突刺", "`other-svc` x")]},
                            persist=True, state_dir=state)
    check("recurrence: new issue starts at 1 (no tag)",
          "已连续" not in d4["issues"][0][1])


def test_recurrence_persist_isolation():
    """Regression: previews used to write the state file. Because a preview runs
    at a different time than the 09:00 scheduled job, it sees a different issue
    set and silently erased the production streaks."""
    state = tempfile.mkdtemp()
    spath = os.path.join(state, "t.recurrence.json")
    today = c.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    json.dump({"date": yesterday, "keys": {"突刺|svcA": 3, "积压|groupB": 3}},
              open(spath, "w"), ensure_ascii=False)

    rp.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                       persist=False, state_dir=state)
    saved = json.load(open(spath))
    check("recurrence: preview does not write state",
          saved["date"] == yesterday and saved["keys"] == {"突刺|svcA": 3,
                                                           "积压|groupB": 3},
          json.dumps(saved["keys"], ensure_ascii=False))

    out = rp.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                             persist=True, state_dir=state)
    saved2 = json.load(open(spath))
    check("recurrence: authoritative run advances streak",
          saved2["keys"].get("突刺|svcA") == 4, str(saved2["keys"]))
    check("recurrence: cross-day resolved issue is dropped (not stale-kept)",
          "积压|groupB" not in saved2["keys"], str(saved2["keys"]))
    check("recurrence: tagged 已连续 4 天",
          "已连续 4 天" in out["issues"][0][1], out["issues"][0][1])

    rp.mark_recurrence({"ns": "t", "issues": [("积压", "`groupB` y")]},
                       persist=True, state_dir=state)
    saved3 = json.load(open(spath))
    check("recurrence: same-day persist merges both keys",
          saved3["keys"].get("突刺|svcA") == 4 and saved3["keys"].get("积压|groupB") == 1,
          json.dumps(saved3["keys"], ensure_ascii=False))


def test_unverified_categories_carry_over():
    """When cloud credentials are broken the 积压/事件 checks never run, so those
    issues are absent from today's report. Absent must mean "unknown", not
    "resolved" — otherwise a credential outage silently clears real streaks."""
    state = tempfile.mkdtemp()
    spath = os.path.join(state, "t.recurrence.json")
    today = c.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    json.dump({"date": yesterday, "keys": {"突刺|svcA": 3, "积压|groupB": 5}},
              open(spath, "w"), ensure_ascii=False)

    rp.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                       persist=True, unverified=("积压",), state_dir=state)
    saved = json.load(open(spath))
    check("unverified: skipped category keeps its streak",
          saved["keys"].get("积压|groupB") == 5, str(saved["keys"]))
    check("unverified: verified category still advances",
          saved["keys"].get("突刺|svcA") == 4, str(saved["keys"]))

    json.dump({"date": yesterday, "keys": {"突刺|svcA": 3, "积压|groupB": 5}},
              open(spath, "w"), ensure_ascii=False)
    rp.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                       persist=True, unverified=(), state_dir=state)
    saved2 = json.load(open(spath))
    check("verified: genuinely resolved category is dropped",
          "积压|groupB" not in saved2["keys"], str(saved2["keys"]))


# ------------------------------------------------------------------ 6. card correctness
def test_card_never_false_green():
    """Regression: a failed query returned no issues, and the card said
    无需处置 ✅ with a green header. The card the user reads must carry the
    data-quality warnings and refuse to claim all-clear."""
    prom.WARN.clear()
    d = rp.build("goods", blank_k(), [], [], [], [], [], [])
    assert d["level"] == "green"

    card = rp.render_card(d)
    check("card: clean run stays green with ✅",
          card["header"]["template"] == "green"
          and any("无需处置" in json.dumps(e, ensure_ascii=False)
                  for e in card["elements"]))

    prom.WARN.append("range query failed: SSL UNEXPECTED_EOF")
    card2 = rp.render_card(d)
    blob = json.dumps(card2, ensure_ascii=False)
    check("card: green downgraded when checks failed",
          card2["header"]["template"] == "orange", card2["header"]["template"])
    check("card: no longer claims 无需处置", "无需处置" not in blob)
    check("card: surfaces the data-quality warning",
          "数据质量" in blob and "SSL UNEXPECTED_EOF" in blob)

    txt = rp.render_text(d)
    check("text: downgraded marker + no all-clear claim",
          txt.startswith("[橙]") and "无需处置" not in txt and "部分检查未完成" in txt,
          txt.splitlines()[0])
    prom.WARN.clear()

    prom.WARN.append("some failure")
    k2 = blank_k(cron=[{"name": "dead-cron", "schedule": "* * * * *",
                        "resident": True, "alive": False, "detail": "无 Running Pod"}])
    d2 = rp.build("goods", k2, [], [], [], [], [], [])
    check("card: red/orange severity preserved with warnings",
          rp.render_card(d2)["header"]["template"] == d2["level"], d2["level"])
    prom.WARN.clear()


def test_merged_card_content():
    """The merged card must carry both halves: the health snapshot (5xx / P99 /
    restarts / alert rules) and the capacity block (peak QPS / middleware / MQ),
    with a sample-size guard instead of a false 0.000% on low traffic."""
    prom.WARN.clear()
    k = blank_k(
        alerts=37, alerts_prev=38, rules={("HpaMaxedOut", "warning"): 37},
        gone=[("Gone", "warning", 1, "x")],
        red={"bad": 0, "all": 2409, "ratio": 0.0, "sampled": True,
             "p99": [("goods", 146.0), ("tenant-goods-c", 23.0)]},
        peak={"today": {"peak": 651.0, "peak_at": 1763000000, "trough": 3.0,
                        "ratio": 187.0},
              "prev": {"peak": 800.0, "peak_at": 1762900000, "trough": 4.0,
                       "ratio": 200.0}},
        spikes=[])
    mongo = [{"name": "m", "shards": [{"cpu": 0.0, "mem": 2.0},
                                      {"cpu": 13.0, "mem": 59.0}], "note": []}]
    redis = [{"name": "r", "cpu": 3.57, "mem": 34.0, "note": []}]
    backlog = [{"group": "self_takeaway_stock_sync_group", "topic": "prod_dfp_sync",
                "peak": 4356, "peak_at": 1763000000, "median": 0, "last": 0}]
    d = rp.build("goods", k, mongo, redis, [], [], [], backlog)
    blob = json.dumps(rp.render_card(d), ensure_ascii=False)
    for label in ("5xx（1h）", "重启 / OOM（24h）", "NotReady", "告警规则",
                  "P99 Top3（1h）", "峰值 QPS（24h）", "中间件峰值（24h）", "MQ 积压"):
        check("merged card: field %s" % label, label in blob)
    check("merged card: 5xx ratio rendered", "0.000%" in blob, blob[:120])
    check("merged card: alert rule breakdown rendered", "HpaMaxedOut" in blob)
    check("merged card: middleware peak rendered", "分片2 CPU 13%" in blob)
    check("merged card: 24h backlog peak rendered", "4356" in blob)

    # low-traffic hour: a ratio over a handful of requests is noise, not 0.000%
    k2 = dict(k, red={"bad": 0, "all": 42, "ratio": 0.0, "sampled": False, "p99": []})
    d2 = rp.build("goods", k2, [], [], [], [], [], [])
    blob2 = json.dumps(rp.render_card(d2), ensure_ascii=False)
    check("merged card: low sample => UNKNOWN not 0.000%",
          "UNKNOWN" in blob2 and "样本不足" in blob2)


def test_sls_unreachable_is_not_healthy():
    """SLS is blocked from this host (TLS reset), so K8s event reasons never
    arrive. That is a blind spot, not silence: the note must name it and the
    report must not claim a clean green."""
    prom.WARN.clear()
    orig = c.aliyun
    c.aliyun = lambda args, timeout=120, quiet=False: None
    try:
        check("sls: unreachable returns None, not empty list",
              c.k8s_events("goods") is None)
    finally:
        c.aliyun = orig
    check("sls: warning recorded", any("SLS" in w for w in prom.WARN), str(prom.WARN))
    d = rp.build("goods", blank_k(), [], [], [], [], None, [])
    check("sls: blind spot named in the note",
          "SLS 不可达" in rp._note(d), rp._note(d))
    check("sls: events_ok=False is exposed for recurrence carry-over",
          d["events_ok"] is False)
    prom.WARN.clear()


# ------------------------------------------------------------------ 7. no-ip pods
def test_pods_without_ip():
    """REGRESSION (2026-09-07): node 172.16.128.18's CNI stopped allocating IPs.
    17 cron pods wedged in ContainerCreating with no IP for 4h+, and the node
    still reported Ready with no resource pressure — nothing else caught it."""
    def by_fn(expr, keys, at="now"):
        if "kube_pod_info" in expr and 'pod_ip=""' in expr:
            return {("job-a-1-x", "node-1"): 1,
                    ("job-b-1-y", "node-1"): 1,
                    ("job-c-1-z", "node-2"): 1}
        if "kube_pod_created" in expr:
            return {("job-a-1-x",): 5 * 3600, ("job-b-1-y",): 2 * 3600,
                    ("job-c-1-z",): 45 * 60}
        return {}

    out = c.pods_without_ip(StubProm(by_fn=by_fn), "goods")
    check("no-ip: finds wedged pods on both nodes", len(out) == 3, str(len(out)))
    check("no-ip: sorted by longest stuck first",
          out[0]["pod"] == "job-a-1-x" and out[0]["mins"] == 300,
          "%s %smin" % (out[0]["pod"], out[0]["mins"]))

    d = rp.build("goods", blank_k(no_ip=out), [], [], [], [], [], [])
    nets = [t for cat, t in d["issues"] if cat == "网络"]
    check("no-ip: aggregated per node, not one row per pod", len(nets) == 2, str(len(nets)))
    check("no-ip: row names the node and pod count",
          any("node-1" in t and "2 个 Pod" in t for t in nets), str(nets))
    check("no-ip: escalates report to red", d["level"] == "red", d["level"])

    def by_fresh(expr, keys, at="now"):
        if "kube_pod_info" in expr and 'pod_ip=""' in expr:
            return {("job-d-1-n", "node-3"): 1}
        return {}

    check("no-ip: fresh pod not flagged",
          c.pods_without_ip(StubProm(by_fn=by_fresh), "goods") == [])


# ------------------------------------------------------------------ 8. mq truncation
def test_mq_truncation_warning():
    prom.WARN.clear()
    insts = [{"instanceId": "rmq-%02d" % i, "instanceName": "prod--ns%02d--mq" % i}
             for i in range(25)]

    def fake_aliyun(args, timeout=120):
        if args[:2] == ["rocketmq", "ListInstances"]:
            return {"data": {"list": insts}}
        return {"data": {"list": []}}

    orig = c.aliyun
    c.aliyun = fake_aliyun
    try:
        c.mq_lag("goods", frozenset(), scan_limit=20)
    finally:
        c.aliyun = orig
    check("rocketmq: >scan_limit emits data-quality warning",
          any("RocketMQ" in w and "25" in w for w in prom.WARN), str(prom.WARN))
    prom.WARN.clear()


# ------------------------------------------------------------------ 9. retry
def test_retry_on_transient_failure():
    body = json.dumps({"status": "success", "data": {"result": [
        {"metric": {"pod": "x"}, "value": [0, "1"]}]}}).encode()

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    attempts = {"n": 0}

    def flaky_urlopen(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("simulated SSL: UNEXPECTED_EOF_WHILE_READING")
        return FakeResp(body)

    orig_urlopen = urllib.request.urlopen
    prom.WARN.clear()
    p = prom.Prom.__new__(prom.Prom)          # bypass cookie loading
    from grafana_prom import Prom as GrafanaProm
    p.client = GrafanaProm(cookie="dddl=x")
    try:
        urllib.request.urlopen = flaky_urlopen
        got = p.q("up")
        got_range = p.range("up", "now-1h", "now", 300)
    finally:
        urllib.request.urlopen = orig_urlopen
    check("retry: q() recovers after 1 transient failure",
          len(got) == 1 and not prom.WARN, "attempts=%d" % attempts["n"])
    check("retry: range() recovers after 1 transient failure",
          len(got_range) == 1, "attempts=%d" % attempts["n"])

    def always_fail(req, timeout=None):
        attempts["n"] += 1
        raise OSError("simulated SSL EOF")

    attempts["n"] = 0
    prom.WARN.clear()
    try:
        urllib.request.urlopen = always_fail
        got2 = p.q("up")
    finally:
        urllib.request.urlopen = orig_urlopen
    check("retry: exhausted retries warn + return empty",
          got2 == [] and attempts["n"] == 2 and len(prom.WARN) == 1,
          "attempts=%d" % attempts["n"])


# ------------------------------------------------------------------ 10. credentials
def test_credential_guard():
    """An expired aliyun token makes every cloud call return None, which used to
    render as a healthy-looking 无数据. It must now surface as a failure."""
    prom.WARN.clear()
    c.CRED_BROKEN.clear()

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        class R:
            returncode = 1
            stdout = ""
            stderr = ("ERROR: init openapi client failed, failed to refresh "
                      "token, status code: 400")
        return R()

    orig_run = subprocess.run
    subprocess.run = fake_run
    try:
        ok = c.preflight_cloud()
    finally:
        subprocess.run = orig_run
    check("cred: preflight detects expired token", ok is False and c.CRED_BROKEN)
    check("cred: failure recorded as data-quality warning",
          any("凭据失效" in w for w in prom.WARN), str(prom.WARN)[:70])

    d = {"backlog": [], "mongo": [], "redis": []}
    check("cred: cloud fields say 凭据失效未检查, not 无数据",
          rp._mw_field(d) == "⚠️ 凭据失效未检查"
          and rp._backlog_field(d) == "⚠️ 凭据失效未检查",
          "%r / %r" % (rp._mw_field(d), rp._backlog_field(d)))

    c.CRED_BROKEN.clear()
    check("cred: healthy credentials keep 无数据 label", rp._mw_field(d) == "无数据")
    prom.WARN.clear()


def test_sts_endpoint_fallback():
    """REGRESSION (2026-10-27): the global sts.aliyuncs.com gateway reset the TLS
    handshake while the regional endpoint and every product endpoint worked.
    That was reported as 凭据失效 and disabled all cloud checks."""
    c.CRED_BROKEN.clear()
    c.CLOUD_UNREACHABLE.clear()
    prom.WARN.clear()

    def stub(global_err=None, regional_ok=True):
        seen = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            seen.append(list(cmd))
            regional = any("--endpoint" in x for x in cmd)

            class R:
                returncode, stdout, stderr = 0, "{}", ""
            if not regional and global_err:
                R.returncode, R.stdout, R.stderr = 1, "", global_err
            elif regional and not regional_ok:
                R.returncode, R.stdout, R.stderr = 1, "", global_err
            return R()
        return fake_run, seen

    orig_run = c.subprocess.run
    tls_reset = ('ERROR: Post "https://sts.aliyuncs.com/?Action=GetCallerIdentity": EOF')
    # the CLI echoes the signed URL, token included; it must never reach a card
    leaky = ('ERROR: Post "https://sts.aliyuncs.com/?AccessKeyId=STS.9j2CUNsg8eH9'
             'KLzkeX7nKgEbdoUd33NPNp&Action=GetCallerIdentity&SecurityToken=CAIS9AJ1'
             'q6Ft5DqyfSjI2KmHCO%2F6nrgZ0orSSWrLj2UNO": EOF')
    try:
        c.subprocess.run, seen = stub(tls_reset)
        ok = c.preflight_cloud()
        check("net: global gateway down falls back to regional STS", ok is True)
        check("net: fallback really used --endpoint",
              any("--endpoint" in x for x in seen[1]), str(seen[-1])[:80])
        check("net: reachable credential raises no warning", not prom.WARN,
              str(prom.WARN)[:60])
        check("net: identity still considered healthy",
              not c.CRED_BROKEN and not c.CLOUD_UNREACHABLE)

        c.subprocess.run, _ = stub(tls_reset, regional_ok=False)
        ok = c.preflight_cloud()
        check("net: both endpoints down skips cloud", ok is False)
        check("net: network failure is not a credential failure",
              not c.CRED_BROKEN and c.CLOUD_UNREACHABLE)
        check("net: warning blames the endpoint, not the credential",
              any("不可达" in w and "非凭据" in w for w in prom.WARN),
              str(prom.WARN)[:80])
        d = {"backlog": [], "mongo": [], "redis": []}
        check("net: cloud fields say 云端未检查",
              rp._mw_field(d) == "⚠️ 云端未检查"
              and rp._backlog_field(d) == "⚠️ 云端未检查", "%r" % rp._mw_field(d))

        c.CRED_BROKEN.clear()
        c.CLOUD_UNREACHABLE.clear()
        prom.WARN.clear()
        c.subprocess.run, _ = stub(tls_reset, regional_ok=False)
        check("net: unparseable product call returns None",
              c.aliyun(["dds", "DescribeDBInstances"]) is None)
        check("net: product-call network failure is classified",
              c.CLOUD_UNREACHABLE and not c.CRED_BROKEN,
              str(c.CLOUD_UNREACHABLE)[:60])
        check("net: product-call failure downgrades the 无数据 label",
              rp._mw_field(d) == "⚠️ 云端未检查")

        c.CRED_BROKEN.clear()
        c.CLOUD_UNREACHABLE.clear()
        prom.WARN.clear()
        c.subprocess.run, _ = stub(leaky, regional_ok=False)
        ok, err = c.sts_identity(None)
        check("secret: STS error keeps its shape", ok is False and "EOF" in err)
        check("secret: AccessKeyId/SecurityToken redacted from the error",
              "STS.9j2CUNsg8eH9" not in err and "CAIS9AJ1" not in err
              and "AccessKeyId=***" in err, err[:80])
        c.preflight_cloud()
        check("secret: warnings carry no token",
              prom.WARN and not any("STS.9j2CUN" in w or "CAIS9AJ1" in w
                                    for w in prom.WARN), str(prom.WARN)[:80])
        check("secret: JSON-style profile dumps are redacted too",
              c._redact('"access_key_secret":"8CBkGM"') == '"access_key_secret=***"'
              and c._redact("RegionId=cn-hangzhou") == "RegionId=cn-hangzhou",
              c._redact('"access_key_secret":"8CBkGM"'))
    finally:
        c.subprocess.run = orig_run
    c.CRED_BROKEN.clear()
    c.CLOUD_UNREACHABLE.clear()
    c.CLOUD_SKIPPED.clear()
    prom.WARN.clear()


def test_skip_cloud_is_labelled():
    c.CLOUD_SKIPPED.append("--skip-cloud")
    try:
        d = {"backlog": [], "mongo": [], "redis": []}
        check("skip: cloud fields say 云检查已跳过",
              rp._mw_field(d) == "⚠️ 云检查已跳过")
    finally:
        c.CLOUD_SKIPPED.clear()


for fn in (test_redis_pagination, test_alert_failure_protection,
           test_cronjob_health, test_build_red_on_dead_cron,
           test_mark_recurrence, test_recurrence_persist_isolation,
           test_unverified_categories_carry_over, test_card_never_false_green,
           test_merged_card_content, test_sls_unreachable_is_not_healthy,
           test_pods_without_ip, test_mq_truncation_warning,
           test_retry_on_transient_failure, test_credential_guard,
           test_sts_endpoint_fallback, test_skip_cloud_is_labelled):
    fn()

fails = [n for n, ok, _ in PASS if not ok]
print("\n%d/%d checks passed" % (len(PASS) - len(fails), len(PASS)))
sys.exit(1 if fails else 0)