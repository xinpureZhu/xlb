#!/usr/bin/env python3
"""Behavioral tests for the daily-inspection fixes: each test exercises the
failure path the fix targets, using stubs (no network/cloud calls)."""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, "/Users/xinpure/Project/xlb/.qoder/skills/daily-inspection/scripts")
import daily_inspect as di
import ns_lark_report as nr

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond), detail))
    print("%s %-46s %s" % ("PASS" if cond else "FAIL", name, detail))


class StubProm:
    """Records calls, returns canned values."""
    def __init__(self, q_fn=None, by_fn=None, num_fn=None):
        self.url = "http://stub/api/v1/query"
        self.token = None
        self.timeout = 5
        self.q_fn = q_fn or (lambda e, at="now": [])
        self.by_fn = by_fn or (lambda e, k, at="now": {})
        self.num_fn = num_fn or (lambda e, at="now", default=0.0: default)
    def q(self, expr, at="now"):
        return self.q_fn(expr, at)
    def by(self, expr, keys, at="now"):
        return self.by_fn(expr, keys, at)
    def num(self, expr, at="now", default=0.0):
        return self.num_fn(expr, at, default)


# ------------------------------------------------------------------ 1. redis pagination
def test_redis_pagination():
    calls = []
    def fake_aliyun(args, timeout=120):
        calls.append(args)
        if args[:2] != ["r-kvstore", "DescribeInstances"]:
            return {}
        page = int(args[args.index("--PageNumber") + 1])
        if page == 1:
            insts = [{"InstanceId": "r-%03d" % i, "InstanceName": "prod--ns%d--redis" % i}
                     for i in range(100)]
        else:
            insts = [{"InstanceId": "r-page2", "InstanceName": "prod--retail-bff--goods-redis"}]
        return {"TotalCount": 101, "Instances": {"KVStoreInstance": insts}}
    orig = nr.aliyun
    nr.aliyun = fake_aliyun
    try:
        got = nr._redis_instances()
    finally:
        nr.aliyun = orig
    names = [i["InstanceName"] for i in got]
    check("redis: paginates past page 1", len(got) == 101, "got=%d" % len(got))
    check("redis: page-2 instance included",
          "prod--retail-bff--goods-redis" in names)


# ------------------------------------------------------------------ 2. alert false-recovery
def test_alert_failure_protection():
    di._warn.clear()
    calls = {"n": 0}
    def fake_alert_instances(p, at):
        calls["n"] += 1
        if calls["n"] == 1:                     # `now` query fails
            di._warn.append("query timeout/failure: simulated SSL EOF")
            return {}
        return {("sig-%d" % i,): ("SomeAlert", "warning", "goods", "pod-x")
                for i in range(24)}             # 24 alerts yesterday
    orig_ai, orig_tp, orig_ss = di.alert_instances, nr.traffic_peak, nr.service_spikes
    di.alert_instances = fake_alert_instances
    nr.traffic_peak = lambda p, ns: {}
    nr.service_spikes = lambda p, ns, hours=24: []
    try:
        k = nr.k8s_summary(StubProm(), "goods")
    finally:
        di.alert_instances, nr.traffic_peak, nr.service_spikes = orig_ai, orig_tp, orig_ss
    check("alerts: failed query hides false recovery",
          k["alerts"] == 0 and k["new"] == [] and k["gone"] == [],
          "alerts=%d new=%d gone=%d" % (k["alerts"], len(k["new"]), len(k["gone"])))
    # healthy path: both queries succeed -> diff still works
    di._warn.clear()
    def ok_alert_instances(p, at):
        if at == "now":
            return {("a",): ("A", "warning", "goods", "x")}
        return {("a",): ("A", "warning", "goods", "x"),
                ("b",): ("B", "warning", "goods", "y")}
    di.alert_instances = ok_alert_instances
    nr.traffic_peak = lambda p, ns: {}
    nr.service_spikes = lambda p, ns, hours=24: []
    try:
        k2 = nr.k8s_summary(StubProm(), "goods")
    finally:
        di.alert_instances, nr.traffic_peak, nr.service_spikes = orig_ai, orig_tp, orig_ss
    check("alerts: healthy diff still computed",
          len(k2["gone"]) == 1 and k2["alerts"] == 1)


# ------------------------------------------------------------------ 3. cronjob liveness
def test_cronjob_health():
    jobs = [{"name": "resident-job", "schedule": "* * * * *", "resident": True},
            {"name": "daily-job", "schedule": "0 3 * * *", "resident": False}]
    orig_jobs = nr.cronjobs_from_manifest
    nr.cronjobs_from_manifest = lambda ns: jobs
    try:
        # resident pod exists but is NOT Running (Failed phase residue)
        def by_no_running(expr, keys, at="now"):
            if "phase=\"Running\"" in expr:
                return {}
            if "kube_pod_start_time" in expr:
                return {("resident-job-1-abcde",): 1000.0}
            return {}
        p = StubProm(by_fn=by_no_running,
                     num_fn=lambda e, at="now", default=0.0: 0.0)
        out = nr.cronjob_health(p, "goods")
        r = next(j for j in out if j["name"] == "resident-job")
        check("cronjob: non-Running pod flagged dead",
              r["alive"] is False, r["detail"])
        # resident pod Running -> alive
        def by_running(expr, keys, at="now"):
            if "phase=\"Running\"" in expr:
                return {("resident-job-1-abcde",): 1}
            if "kube_pod_start_time" in expr:
                import time
                return {("resident-job-1-abcde",): time.time() - 5 * 86400}
            return {}
        p2 = StubProm(by_fn=by_running, num_fn=lambda e, at="now", default=0.0: 0.0)
        out2 = nr.cronjob_health(p2, "goods")
        r2 = next(j for j in out2 if j["name"] == "resident-job")
        check("cronjob: Running pod flagged alive",
              r2["alive"] is True and "5 天" in r2["detail"], r2["detail"])
        # periodic job with no new pod in 24h -> dead
        p3 = StubProm(by_fn=lambda e, k, at="now": {},
                      num_fn=lambda e, at="now", default=0.0: 0.0)
        out3 = nr.cronjob_health(p3, "goods")
        d = next(j for j in out3 if j["name"] == "daily-job")
        check("cronjob: periodic without recent Job flagged",
              d["alive"] is False, d["detail"])
        # periodic job with a new pod -> alive
        p4 = StubProm(by_fn=lambda e, k, at="now": {},
                      num_fn=lambda e, at="now", default=0.0: 1.0)
        out4 = nr.cronjob_health(p4, "goods")
        d4 = next(j for j in out4 if j["name"] == "daily-job")
        check("cronjob: periodic with recent Job alive", d4["alive"] is True)

        # REGRESSION (2026-09-07): a pod WAS created, so a kube_pod_created
        # check says alive — but it never started. CNI failure left 17 cron
        # pods wedged in ContainerCreating with no IP for hours while the
        # business endpoint silently stopped being called.
        def by_wedged(expr, keys, at="now"):
            if "waiting_reason" in expr:
                return {("daily-job-9-x", "ContainerCreating"): 1}
            if "kube_pod_created" in expr:
                return {("daily-job-9-x",): 4 * 3600}   # wedged 4h
            return {}
        p5 = StubProm(by_fn=by_wedged,
                      num_fn=lambda e, at="now", default=0.0: 1.0)
        w = next(j for j in nr.cronjob_health(p5, "goods")
                 if j["name"] == "daily-job")
        check("cronjob: created-but-wedged pod flagged dead",
              w["alive"] is False and "ContainerCreating" in w["detail"],
              w["detail"])

        # a freshly created pod still pulling must NOT be flagged
        def by_starting(expr, keys, at="now"):
            if "waiting_reason" in expr:
                return {("daily-job-9-y", "ContainerCreating"): 1}
            if "kube_pod_created" in expr:
                return {("daily-job-9-y",): 60}        # only 1 min old
            return {}
        p6 = StubProm(by_fn=by_starting,
                      num_fn=lambda e, at="now", default=0.0: 1.0)
        s = next(j for j in nr.cronjob_health(p6, "goods")
                 if j["name"] == "daily-job")
        check("cronjob: briefly-starting pod not flagged", s["alive"] is True,
              s["detail"])
    finally:
        nr.cronjobs_from_manifest = orig_jobs


# ------------------------------------------------------------------ 4. dead cronjob -> red
def test_build_red_on_dead_cron():
    k = {"alerts": 0, "alerts_prev": 0, "new": [], "gone": [], "rules": {},
         "svc": [], "stab": {"restart": 0, "oom": 0}, "peak": {}, "spikes": []}
    cron = [{"name": "resident-job", "schedule": "* * * * *", "resident": True,
             "alive": False, "detail": "无 Running Pod"}]
    d = nr.build("goods", k, [], [], [], [], [], [], [], cron)
    check("build: dead CronJob => red + issue row",
          d["level"] == "red" and any(c == "CronJob" for c, _ in d["issues"]),
          "level=%s" % d["level"])


# ------------------------------------------------------------------ 5. recurrence marking
def test_mark_recurrence():
    tmp = tempfile.mkdtemp()
    orig_file = nr.__file__
    nr.__file__ = os.path.join(tmp, "fake.py")   # redirect state dir
    today = nr.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    try:
        issues = [("突刺", "`erp-store-sync` P99 峰值 29.55s"),
                  ("积压", "`prod_erp_stock_detail` 中位 71")]
        # day 1: no state -> all streak 1, no tag (authoritative run persists)
        d1 = nr.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True)
        check("recurrence: first day has no tag",
              not any("已连续" in t for _, t in d1["issues"]))
        # day 2 (simulate: state dated yesterday with same keys)
        spath = os.path.join(tmp, "state", "t.recurrence.json")
        prev = json.load(open(spath))
        json.dump({"date": yesterday, "keys": prev["keys"]}, open(spath, "w"))
        d2 = nr.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True)
        tagged = [t for _, t in d2["issues"]]
        check("recurrence: second day tagged 已连续 2 天",
              all("已连续 2 天" in t for t in tagged), str(tagged))
        # same-day re-run: streak must NOT inflate
        d3 = nr.mark_recurrence({"ns": "t", "issues": list(issues)}, persist=True)
        check("recurrence: same-day rerun keeps streak 2",
              all("已连续 2 天" in t for _, t in d3["issues"]))
        # an issue that disappears is not carried over
        d4 = nr.mark_recurrence({"ns": "t", "issues": [("突刺", "`other-svc` x")]},
                                persist=True)
        check("recurrence: new issue starts at 1 (no tag)",
              "已连续" not in d4["issues"][0][1])
    finally:
        nr.__file__ = orig_file


# ------------------------------------------------------------------ 6. rocketmq truncation warn
def test_mq_truncation_warning():
    di._warn.clear()
    insts = [{"instanceId": "rmq-%02d" % i, "instanceName": "prod--ns%02d--mq" % i}
             for i in range(25)]
    def fake_aliyun(args, timeout=120):
        if args[:2] == ["rocketmq", "ListInstances"]:
            return {"data": {"list": insts}}
        return {"data": {"list": []}}           # no consumer groups anywhere
    orig = nr.aliyun
    nr.aliyun = fake_aliyun
    try:
        nr.mq_lag("goods", frozenset(), scan_limit=20)
    finally:
        nr.aliyun = orig
    check("rocketmq: >scan_limit emits data-quality warning",
          any("RocketMQ" in w and "25" in w for w in di._warn), str(di._warn))


# ------------------------------------------------------------------ 7. retry under injected failure
def test_retry_on_transient_failure():
    import urllib.request
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
    urllib.request.urlopen = flaky_urlopen
    di._warn.clear()
    try:
        p = di.Prom("http://stub", "prometheus", None, 5)
        got = p.q("up")                                    # instant query
        r_got = nr.range_q(p, "up", "now-1h", "now", "300s")
    finally:
        urllib.request.urlopen = orig_urlopen
    # each call fails once then recovers: 2 attempts x 2 calls = 4… but the
    # shared counter sees attempt 1 fail, then attempts 2-3 succeed.
    check("retry: q() recovers after 1 transient failure",
          got and not di._warn,
          "attempts=%d warns=%d" % (attempts["n"], len(di._warn)))
    check("retry: range_q() recovers after 1 transient failure",
          bool(r_got), "attempts=%d" % attempts["n"])
    # both attempts fail -> warn recorded, empty result (never a false number)
    attempts["n"] = 0
    def always_fail(req, timeout=None):
        attempts["n"] += 1
        raise OSError("simulated SSL EOF")
    urllib.request.urlopen = always_fail
    di._warn.clear()
    try:
        got2 = p.q("up")
    finally:
        urllib.request.urlopen = orig_urlopen
    check("retry: exhausted retries warn + return empty",
          got2 == [] and attempts["n"] == 2 and len(di._warn) == 1,
          "attempts=%d" % attempts["n"])


def test_recurrence_persist_isolation():
    """Regression: previews used to write the state file. Because a preview runs
    at a different time than the 09:00 scheduled job, it sees a different issue
    set and silently erased the production streaks (observed 2026-09-07: a 4-day
    backlog streak was dropped to nothing)."""
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "state"))
    orig_file = nr.__file__
    nr.__file__ = os.path.join(tmp, "fake.py")
    spath = os.path.join(tmp, "state", "t.recurrence.json")
    today = nr.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    try:
        # authoritative run yesterday recorded two streaks
        json.dump({"date": yesterday,
                   "keys": {"突刺|svcA": 3, "积压|groupB": 3}},
                  open(spath, "w"), ensure_ascii=False)

        # a preview today, where only svcA is currently visible
        nr.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                           persist=False)
        saved = json.load(open(spath))
        check("recurrence: preview does not write state",
              saved["date"] == yesterday and saved["keys"] == {"突刺|svcA": 3,
                                                               "积压|groupB": 3},
              json.dumps(saved["keys"], ensure_ascii=False))

        # authoritative run today persists
        out = nr.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                                 persist=True)
        saved2 = json.load(open(spath))
        check("recurrence: authoritative run advances streak",
              saved2["keys"].get("突刺|svcA") == 4, str(saved2["keys"]))
        check("recurrence: cross-day resolved issue is dropped (not stale-kept)",
              "积压|groupB" not in saved2["keys"], str(saved2["keys"]))
        check("recurrence: tagged 已连续 4 天",
              "已连续 4 天" in out["issues"][0][1], out["issues"][0][1])

        # same-day authoritative re-run must merge, not erase what it recorded
        nr.mark_recurrence({"ns": "t", "issues": [("积压", "`groupB` y")]},
                           persist=True)
        saved3 = json.load(open(spath))
        check("recurrence: same-day persist merges both keys",
              saved3["keys"].get("突刺|svcA") == 4
              and saved3["keys"].get("积压|groupB") == 1,
              json.dumps(saved3["keys"], ensure_ascii=False))
    finally:
        nr.__file__ = orig_file


def test_unverified_categories_carry_over():
    """When cloud credentials are broken the 积压/事件 checks never run, so those
    issues are absent from today's report. Absent must mean "unknown", not
    "resolved" — otherwise a credential outage silently clears real streaks."""
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "state"))
    orig_file = nr.__file__
    nr.__file__ = os.path.join(tmp, "fake.py")
    spath = os.path.join(tmp, "state", "t.recurrence.json")
    today = nr.now_cst().date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    try:
        json.dump({"date": yesterday,
                   "keys": {"突刺|svcA": 3, "积压|groupB": 5}},
                  open(spath, "w"), ensure_ascii=False)

        # cloud broken: only the spike check ran
        nr.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                           persist=True, unverified=("积压",))
        saved = json.load(open(spath))
        check("unverified: skipped category keeps its streak",
              saved["keys"].get("积压|groupB") == 5, str(saved["keys"]))
        check("unverified: verified category still advances",
              saved["keys"].get("突刺|svcA") == 4, str(saved["keys"]))

        # cloud healthy: an absent category really is resolved -> dropped
        json.dump({"date": yesterday,
                   "keys": {"突刺|svcA": 3, "积压|groupB": 5}},
                  open(spath, "w"), ensure_ascii=False)
        nr.mark_recurrence({"ns": "t", "issues": [("突刺", "`svcA` x")]},
                           persist=True, unverified=())
        saved2 = json.load(open(spath))
        check("verified: genuinely resolved category is dropped",
              "积压|groupB" not in saved2["keys"], str(saved2["keys"]))
    finally:
        nr.__file__ = orig_file


def test_card_never_false_green():
    """Regression: a failed query returned no issues, and the card said
    无需处置 ✅ with a green header (observed 2026-08-16/17). The card the user
    reads must carry the data-quality warnings and refuse to claim all-clear."""
    k = {"alerts": 0, "alerts_prev": 0, "new": [], "gone": [], "rules": {},
         "svc": [], "stab": {"restart": 0, "oom": 0}, "peak": {}, "spikes": []}
    d = nr.build("goods", k, [], [], [], [], [], [], [], [])
    assert d["level"] == "green"

    di._warn.clear()
    card = nr.render_card(d)
    check("card: clean run stays green with ✅",
          card["header"]["template"] == "green"
          and any("无需处置" in json.dumps(e, ensure_ascii=False)
                  for e in card["elements"]))

    di._warn.clear()
    di._warn.append("range query failed: SSL UNEXPECTED_EOF")
    card2 = nr.render_card(d)
    blob = json.dumps(card2, ensure_ascii=False)
    check("card: green downgraded when checks failed",
          card2["header"]["template"] == "orange",
          card2["header"]["template"])
    check("card: no longer claims 无需处置", "无需处置" not in blob)
    check("card: surfaces the data-quality warning",
          "数据质量" in blob and "SSL UNEXPECTED_EOF" in blob)

    # the text preview must follow the same rule (shared effective_level)
    txt = nr.render_text(d)
    check("text: downgraded marker + no all-clear claim",
          txt.startswith("[橙]") and "无需处置" not in txt
          and "部分检查未完成" in txt, txt.splitlines()[0])
    di._warn.clear()

    # an already-orange report must not be downgraded by warnings
    di._warn.append("some failure")
    k2 = dict(k)
    d2 = nr.build("goods", k2, [], [], [], [], [], [], [],
                  [{"name": "dead-cron", "schedule": "* * * * *",
                    "resident": True, "alive": False, "detail": "无 Running Pod"}])
    check("card: red/orange severity preserved with warnings",
          nr.render_card(d2)["header"]["template"] == d2["level"],
          d2["level"])
    di._warn.clear()


def test_pods_without_ip():
    """REGRESSION (2026-09-07): node 172.16.128.18's CNI stopped allocating IPs.
    17 cron pods wedged in ContainerCreating with no IP for 4h+, and the node
    still reported Ready with no resource pressure — nothing else caught it.
    goods' hourly cron silently missed 5 executions."""
    def by_fn(expr, keys, at="now"):
        if "kube_pod_info" in expr and 'pod_ip=""' in expr:
            return {("job-a-1-x", "node-1"): 1,
                    ("job-b-1-y", "node-1"): 1,
                    ("job-c-1-z", "node-2"): 1}
        if "kube_pod_created" in expr:
            # only pods older than STUCK_MIN survive the `> 1800` filter
            return {("job-a-1-x",): 5 * 3600, ("job-b-1-y",): 2 * 3600,
                    ("job-c-1-z",): 45 * 60}
        return {}
    p = StubProm(by_fn=by_fn)
    out = nr.pods_without_ip(p, "goods")
    check("no-ip: finds wedged pods on both nodes", len(out) == 3, str(len(out)))
    check("no-ip: sorted by longest stuck first",
          out[0]["pod"] == "job-a-1-x" and out[0]["mins"] == 300,
          "%s %smin" % (out[0]["pod"], out[0]["mins"]))

    k = {"alerts": 0, "alerts_prev": 0, "new": [], "gone": [], "rules": {},
         "svc": [], "stab": {"restart": 0, "oom": 0}, "peak": {}, "spikes": []}
    d = nr.build("goods", k, [], [], [], [], [], [], [], [], out)
    nets = [t for c, t in d["issues"] if c == "网络"]
    check("no-ip: aggregated per node, not one row per pod",
          len(nets) == 2, str(len(nets)))
    check("no-ip: row names the node and pod count",
          any("node-1" in t and "2 个 Pod" in t for t in nets), str(nets))
    check("no-ip: escalates report to red", d["level"] == "red", d["level"])

    # a pod younger than STUCK_MIN must not be flagged (normal image pull)
    def by_fresh(expr, keys, at="now"):
        if "kube_pod_info" in expr and 'pod_ip=""' in expr:
            return {("job-d-1-n", "node-3"): 1}
        if "kube_pod_created" in expr:
            return {}            # filtered out by the age threshold
        return {}
    check("no-ip: fresh pod not flagged",
          nr.pods_without_ip(StubProm(by_fn=by_fresh), "goods") == [])


# ------------------------------------------------------------------ 8. credential guard
def test_credential_guard():
    """An expired aliyun token makes every cloud call return None, which used to
    render as a healthy-looking 无数据. It must now surface as a failure."""
    import subprocess
    di._warn.clear()
    nr.CRED_BROKEN.clear()

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
        ok = nr.preflight_cloud()
    finally:
        subprocess.run = orig_run
    check("cred: preflight detects expired token", ok is False and nr.CRED_BROKEN)
    check("cred: failure recorded as data-quality warning",
          any("凭据失效" in w for w in di._warn), str(di._warn)[:70])

    d = {"backlog": [], "mongo": [], "redis": []}
    check("cred: cloud fields say 凭据失效未检查, not 无数据",
          nr._mw_field(d) == "⚠️ 凭据失效未检查"
          and nr._backlog_field(d) == "⚠️ 凭据失效未检查",
          "%r / %r" % (nr._mw_field(d), nr._backlog_field(d)))

    # with credentials healthy the label must stay 无数据 (no false alarm)
    nr.CRED_BROKEN.clear()
    check("cred: healthy credentials keep 无数据 label",
          nr._mw_field(d) == "无数据")
    di._warn.clear()


def test_sts_endpoint_fallback():
    """REGRESSION (2026-10-27): the global sts.aliyuncs.com gateway reset the TLS
    handshake while the regional endpoint and every product endpoint worked.
    That was reported as 凭据失效 and disabled all cloud checks, sending people
    to re-run `aliyun configure` for nothing."""
    nr.CRED_BROKEN.clear()
    nr.CLOUD_UNREACHABLE.clear()
    di._warn.clear()

    def stub(global_err=None, regional_ok=True):
        seen = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            seen.append(list(cmd))
            regional = any("--endpoint" in c for c in cmd)
            class R:
                returncode, stdout, stderr = 0, "{}", ""
            if not regional and global_err:
                R.returncode, R.stdout, R.stderr = 1, "", global_err
            elif regional and not regional_ok:
                R.returncode, R.stdout, R.stderr = 1, "", global_err
            return R()
        return fake_run, seen

    orig_run = nr.subprocess.run
    tls_reset = ("ERROR: Post \"https://sts.aliyuncs.com/?Action=GetCallerIdentity\": "
                 "EOF")
    # the CLI echoes the signed URL, token included; it must never reach a card
    leaky = ('ERROR: Post "https://sts.aliyuncs.com/?AccessKeyId=STS.9j2CUNsg8eH9'
             'KLzkeX7nKgEbdoUd33NPNp&Action=GetCallerIdentity&SecurityToken=CAIS9AJ1'
             'q6Ft5DqyfSjI2KmHCO%2F6nrgZ0orSSWrLj2UNO": EOF')
    try:
        nr.subprocess.run, seen = stub(tls_reset)
        ok = nr.preflight_cloud()
        check("net: global gateway down falls back to regional STS", ok is True)
        check("net: fallback really used --endpoint",
              any("--endpoint" in c for c in seen[1]), str(seen[-1])[:80])
        check("net: reachable credential raises no warning", not di._warn,
              str(di._warn)[:60])
        check("net: identity still considered healthy",
              not nr.CRED_BROKEN and not nr.CLOUD_UNREACHABLE)

        # both endpoints blocked -> skip cloud, but never call it 凭据失效
        nr.subprocess.run, _ = stub(tls_reset, regional_ok=False)
        ok = nr.preflight_cloud()
        check("net: both endpoints down skips cloud", ok is False)
        check("net: network failure is not a credential failure",
              not nr.CRED_BROKEN and nr.CLOUD_UNREACHABLE)
        check("net: warning blames the endpoint, not the credential",
              any("不可达" in w and "非凭据" in w for w in di._warn),
              str(di._warn)[:80])
        d = {"backlog": [], "mongo": [], "redis": []}
        check("net: cloud fields say 云端未检查",
              nr._mw_field(d) == "⚠️ 云端未检查"
              and nr._backlog_field(d) == "⚠️ 云端未检查",
              "%r" % nr._mw_field(d))

        # a product call that never reaches the service must not read as 无数据
        nr.CRED_BROKEN.clear()
        nr.CLOUD_UNREACHABLE.clear()
        di._warn.clear()
        nr.subprocess.run, _ = stub(tls_reset, regional_ok=False)
        check("net: unparseable product call returns None",
              nr.aliyun(["dds", "DescribeDBInstances"]) is None)
        check("net: product-call network failure is classified",
              nr.CLOUD_UNREACHABLE and not nr.CRED_BROKEN, str(nr.CLOUD_UNREACHABLE)[:60])
        check("net: product-call failure downgrades the 无数据 label",
              nr._mw_field(d) == "⚠️ 云端未检查")

        # credentials echoed by the CLI must not be surfaced or logged
        nr.CRED_BROKEN.clear()
        nr.CLOUD_UNREACHABLE.clear()
        di._warn.clear()
        nr.subprocess.run, _ = stub(leaky, regional_ok=False)
        ok, err = nr.sts_identity(None)
        check("secret: STS error keeps its shape", ok is False and "EOF" in err)
        check("secret: AccessKeyId/SecurityToken redacted from the error",
              "STS.9j2CUNsg8eH9" not in err and "CAIS9AJ1" not in err
              and "AccessKeyId=***" in err, err[:80])
        nr.preflight_cloud()
        check("secret: warnings carry no token",
              di._warn and not any("STS.9j2CUN" in w or "CAIS9AJ1" in w
                                   for w in di._warn), str(di._warn)[:80])
        check("secret: JSON-style profile dumps are redacted too",
              nr._redact('"access_key_secret":"8CBkGM"') == '"access_key_secret=***"'
              and nr._redact("RegionId=cn-hangzhou") == "RegionId=cn-hangzhou",
              nr._redact('"access_key_secret":"8CBkGM"'))
    finally:
        nr.subprocess.run = orig_run
    nr.CRED_BROKEN.clear()
    nr.CLOUD_UNREACHABLE.clear()
    di._warn.clear()


for fn in (test_redis_pagination, test_alert_failure_protection,
           test_cronjob_health, test_build_red_on_dead_cron,
           test_mark_recurrence, test_recurrence_persist_isolation,
           test_unverified_categories_carry_over,
           test_card_never_false_green,
           test_pods_without_ip,
           test_mq_truncation_warning,
           test_retry_on_transient_failure, test_credential_guard,
           test_sts_endpoint_fallback):
    fn()

fails = [n for n, ok, _ in PASS if not ok]
print("\n%d/%d checks passed" % (len(PASS) - len(fails), len(PASS)))
sys.exit(1 if fails else 0)
