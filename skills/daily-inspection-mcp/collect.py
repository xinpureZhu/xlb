#!/usr/bin/env python3
"""Collectors for the goods daily inspection: K8s metrics (Prometheus via the
Grafana cookie) plus the namespace's owned cloud resources (aliyun CLI).

Every cloud call is a read-only Describe/List/Get. Credential failures and
network failures are recorded separately: a broken credential asks the reader to
re-login, a blocked endpoint does not, and neither may render as "healthy".
"""

import collections
import datetime
import json
import os
import re
import statistics
import subprocess
import time

from prom import WARN, Prom  # noqa: F401  (Prom re-exported for callers)

TZ = 8 * 3600
REGION = "cn-hangzhou"

# Report thresholds. Anything below these is silence.
TH = {
    "qps_pct": 30.0,      # traffic change worth mentioning
    "p99_pct": 50.0,      # latency regression
    "p99_abs": 2.0,       # chronic slowness, seconds
    "err_pct": 0.1,       # 5xx share of traffic
    "res_pct": 50.0,      # cpu/mem change
}

# A cron pod that stays in a pre-start waiting state this long has not run.
# Normal pulls take seconds; 30 min leaves room for cold image pulls while
# still catching the CNI-wedge case (observed: stuck 4h+ with no IP).
STUCK_MIN = 30

# K8s events land in SLS, not Prometheus. Project is k8s-log-<ACK clusterId>.
SLS_PROJECT = "k8s-log-cb5b365bf5d49464d981e2b4cfc44dc55"
SLS_LOGSTORE = "k8s-event"

# Only hard failures. TooManyMissedTimes / Unhealthy are excluded on purpose:
# they fire in the thousands and drown the reasons that need action.
BAD_REASONS = {
    "PodOOMKilling": "容器被 OOM 杀掉",
    "Evicted": "Pod 被驱逐",
    "EvictionThresholdMet": "节点触及驱逐水位",
    "BackOff": "容器反复启动失败",
    "Failed": "容器启动失败",
    "BackoffLimitExceeded": "Job 重试次数耗尽",
    "FailedScheduling": "Pod 无法调度",
    "FailedMount": "存储挂载失败",
    "FailedGetScale": "HPA 拿不到伸缩目标",
    "FailedGetResourceMetric": "HPA 拿不到指标",
    "FailedComputeMetricsReplicas": "HPA 无法计算副本数",
    "FreeDiskSpaceFailed": "磁盘空间回收失败",
    "ImageGCFailed": "镜像 GC 失败",
    "AllocIPFailed": "分配 Pod IP 失败",
    "NodeNotReady": "节点异常",
}

REGIONAL_STS_ENDPOINT = "sts.%s.aliyuncs.com" % REGION

# Set when the aliyun CLI cannot authenticate at all (expired OAuth token).
CRED_BROKEN = []
# Set when cloud calls fail for network/TLS reasons rather than credentials.
CLOUD_UNREACHABLE = []
# Set when the caller asked to skip cloud checks (--skip-cloud) or the aliyun
# CLI is absent: "not checked" must not read as "healthy".
CLOUD_SKIPPED = []

# The CLI echoes the signed request URL in some errors, and that URL carries the
# live STS token; warnings end up in cards and log files.
_SECRET_PARAM_RE = re.compile(
    r'(?i)\b(access_key_id|accesskeyid|access_key_secret|accesskeysecret|'
    r'sts_token|securitytoken|signature|oauth_access_token|'
    r'oauth_refresh_token|access_token|refresh_token)"?\s*[=:]\s*"?[^&"\s,}]*')

_CRED_ERR_HINTS = ("refresh token", "invalidcredential", "invalidaccesskeyid",
                   "invalidsecuritytoken", "securitytokenexpired",
                   "unauthorized", "forbidden", "no credential",
                   "init client failed", "init openapi client failed")

_NET_ERR_HINTS = ("ssl", "eof", "connection reset", "connection refused",
                  "connection aborted", "timeout", "timed out",
                  "no such host", "dial tcp")


def now_cst():
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=TZ)


def cst(ts):
    return datetime.datetime.fromtimestamp(ts + TZ, datetime.UTC).strftime("%m-%d %H:%M")


def pct(new, old):
    if old == 0:
        return float("inf") if new > 0 else 0.0
    return (new - old) / old * 100


def arrow(p):
    if p == float("inf"):
        return "新增"
    return ("+%.0f%%" % p) if p >= 0 else ("%.0f%%" % p)


def _redact(text):
    """Remove credentials from CLI output before it is logged or surfaced."""
    return _SECRET_PARAM_RE.sub(r"\1=***", text or "")


# ----------------------------------------------------------------- cloud side
def aliyun(args, timeout=120, quiet=False):
    """Run one read-only aliyun CLI call.

    `quiet` skips the global credential/network classification: endpoints are
    per product, so a blocked SLS must not disable (or relabel) MongoDB/Redis.
    """
    try:
        r = subprocess.run(["aliyun"] + args + ["--region", REGION],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    err = _redact(r.stderr or "")
    if r.returncode != 0 and not quiet and not CRED_BROKEN and not CLOUD_UNREACHABLE:
        low = err.lower()
        if any(h in low for h in _CRED_ERR_HINTS):
            CRED_BROKEN.append(err.strip()[:120])
            WARN.append("阿里云凭据失效，云侧检查全部跳过（MongoDB/Redis/MQ 显示「无数据」"
                        "不代表健康）：%s" % err.strip()[:90])
        elif any(h in low for h in _NET_ERR_HINTS):
            CLOUD_UNREACHABLE.append(err.strip()[:120])
            WARN.append("阿里云接口不可达（网络/TLS，非凭据问题；相应云检查未完成，"
                        "「无数据」不代表健康）：%s" % err.strip()[:90])
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def sts_identity(endpoint=None, timeout=60):
    """(ok, error). Verifies the credential with one STS call."""
    args = ["aliyun", "sts", "GetCallerIdentity", "--region", REGION]
    if endpoint:
        args += ["--endpoint", endpoint]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:                      # timeout / CLI missing
        return False, _redact(str(e))[:160]
    if r.returncode == 0:
        return True, ""
    return False, _redact((r.stderr or r.stdout or "").strip()[:400])[:160]


def preflight_cloud():
    """Cheap credential check before running minutes of cloud queries.

    The CLI defaults to the global sts.aliyuncs.com gateway; when that is
    unreachable the regional endpoint is tried before blaming the credential.
    Both endpoints serve the same identity, so a success on either means the
    product calls below will work."""
    err = ""
    for endpoint in (None, REGIONAL_STS_ENDPOINT):
        ok, err = sts_identity(endpoint)
        if ok:
            return True
    low = err.lower()
    if any(h in low for h in _CRED_ERR_HINTS):
        CRED_BROKEN.append(err)
        WARN.append("阿里云凭据失效（aliyun sts GetCallerIdentity 失败），"
                    "云侧检查全部跳过：%s" % err[:90])
    else:
        CLOUD_UNREACHABLE.append(err)
        WARN.append("阿里云 STS 端点不可达（网络/TLS 阻断，非凭据失效；已试 %s），"
                    "云侧检查全部跳过：%s" % (REGIONAL_STS_ENDPOINT, err[:90]))
    return False


def owns(inst_name, ns, env="prod"):
    """`env--namespace--resource`; a resource belongs to ns when the namespace
    segment matches, or when another namespace holds a resource named after it
    (e.g. prod--retail-bff--goods-mongodb belongs to goods)."""
    parts = str(inst_name or "").split("--")
    if len(parts) < 3 or parts[0] != env:
        return False
    owner_ns, res = parts[1], parts[-1]
    return owner_ns == ns or res == ns or res.startswith(ns + "-")


def _pc(v, nd=0):
    return ("%.*f%%" % (nd, v)) if v is not None else "?"


def mongo_health(ns):
    """24h CPU / memory peak per shard. Slow-log counting and disk skew were
    dropped to keep the report to one screen (and it removes the slowest call)."""
    out, insts = [], []
    for kind in ("sharding", "replicate"):   # sharded ones are omitted by default
        d = aliyun(["dds", "DescribeDBInstances", "--PageSize", "100", "--DBInstanceType", kind])
        insts += ((d or {}).get("DBInstances") or {}).get("DBInstance") or []
    end = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
    f = "%Y-%m-%dT%H:%MZ"
    for i in insts:
        name = i.get("DBInstanceDescription")
        if not owns(name, ns):
            continue
        iid = i.get("DBInstanceId")
        at = aliyun(["dds", "DescribeDBInstanceAttribute", "--DBInstanceId", iid])
        attr = ((at or {}).get("DBInstances") or {}).get("DBInstance") or [{}]
        info = {"name": name, "shards": [], "note": []}
        for s_ in (attr[0].get("ShardList") or {}).get("ShardAttribute") or []:
            perf = aliyun(["dds", "DescribeDBInstancePerformance", "--DBInstanceId", iid,
                           "--NodeId", s_.get("NodeId"), "--Key", "CpuUsage,MemoryUsage",
                           "--StartTime", (end - datetime.timedelta(hours=24)).strftime(f),
                           "--EndTime", end.strftime(f)])
            vals = {}
            for pk in ((perf or {}).get("PerformanceKeys") or {}).get("PerformanceKey", []):
                nums = []
                for v in (pk.get("PerformanceValues") or {}).get("PerformanceValue", []):
                    try:
                        nums.append(float(str(v.get("Value", "")).split("&")[0]))
                    except ValueError:
                        pass
                if nums:
                    vals[pk.get("Key")] = max(nums)
            info["shards"].append({"cpu": vals.get("CpuUsage"), "mem": vals.get("MemoryUsage")})
        for n, sh in enumerate(info["shards"], 1):
            if (sh["cpu"] or 0) >= 70:
                info["note"].append("分片%d CPU 峰值 %.0f%%" % (n, sh["cpu"]))
            if (sh["mem"] or 0) >= 80:
                info["note"].append("分片%d 内存峰值 %.0f%%" % (n, sh["mem"]))
        out.append(info)
    return out


def _redis_instances():
    """Page through every Redis instance. There are more than 100 in the
    account, so a single PageSize=100 call silently drops later pages —
    which is exactly where `prod--retail-bff--goods-redis` lived."""
    insts, page = [], 1
    while True:
        d = aliyun(["r-kvstore", "DescribeInstances", "--PageSize", "100",
                    "--PageNumber", str(page)])
        batch = ((d or {}).get("Instances") or {}).get("KVStoreInstance") or []
        insts += batch
        total = (d or {}).get("TotalCount") or len(insts)
        if len(batch) < 100 or len(insts) >= total:
            break
        page += 1
    return insts


def redis_eviction_risk(iid, end, fmt, mem_peak):
    """volatile-* policy + near-zero TTL coverage means hitting maxmemory makes
    WRITES FAIL instead of evicting cold keys — memory usage alone never shows
    this. Only worth the two extra API calls once memory is already climbing."""
    if mem_peak is None or mem_peak < 70:
        return None
    pol = aliyun(["r-kvstore", "DescribeParameters", "--DBInstanceId", iid])
    policy = None
    for group in ("RunningParameters", "ConfigParameters"):
        for p_ in ((pol or {}).get(group) or {}).get("Parameter") or []:
            if p_.get("ParameterName") == "maxmemory-policy":
                policy = p_.get("ParameterValue")
    if not policy or not str(policy).startswith("volatile"):
        return None
    ks = aliyun(["r-kvstore", "DescribeHistoryMonitorValues",
                 "--InstanceId", iid,
                 "--StartTime", (end - datetime.timedelta(hours=1)).strftime(fmt),
                 "--EndTime", end.strftime(fmt), "--IntervalForHistory", "05m",
                 "--MonitorKeys", "Keys,Expires"])
    keys = expires = None
    try:
        for _, m in json.loads((ks or {}).get("MonitorHistory") or "{}").items():
            if isinstance(m, dict):
                try:
                    keys, expires = float(m["Keys"]), float(m["Expires"])
                except (KeyError, TypeError, ValueError):
                    pass
    except Exception:
        pass
    if not keys:
        return None
    cov = (expires or 0) / keys
    if cov < 0.05:
        return ("内存峰值 %.0f%% + %s 且 TTL 覆盖率仅 %.2f%% —— 撞 maxmemory 会写入报错而非驱逐"
                % (mem_peak, policy, cov * 100))
    return None


def redis_health(ns):
    """24h CPU / memory peak, plus the volatile-policy eviction risk."""
    out = []
    end = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
    f = "%Y-%m-%dT%H:%M:%SZ"
    for i in _redis_instances():
        name = i.get("InstanceName")
        if not owns(name, ns):
            continue
        mon = aliyun(["r-kvstore", "DescribeHistoryMonitorValues",
                      "--InstanceId", i.get("InstanceId"),
                      "--StartTime", (end - datetime.timedelta(hours=24)).strftime(f),
                      "--EndTime", end.strftime(f), "--IntervalForHistory", "05m",
                      "--MonitorKeys", "CpuUsage,memoryUsage"])
        agg = collections.defaultdict(list)
        try:
            for _, m in json.loads((mon or {}).get("MonitorHistory") or "{}").items():
                if isinstance(m, dict):
                    for k, v in m.items():
                        try:
                            agg[k].append(float(v))
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass
        cpu = max(agg["CpuUsage"]) if agg.get("CpuUsage") else None
        mem = max(agg["memoryUsage"]) if agg.get("memoryUsage") else None
        note = []
        if (cpu or 0) >= 70:
            note.append("CPU 峰值 %.0f%%" % cpu)
        if (mem or 0) >= 80:
            note.append("内存峰值 %.0f%%" % mem)
        risk = redis_eviction_risk(i.get("InstanceId"), end, f, mem)
        if risk:
            note.append(risk)
        out.append({"name": name, "cpu": cpu, "mem": mem, "note": note})
    return out


def mq_lag(ns, declared, scan_limit=20):
    """Namespaces often consume from other namespaces' instances, so scan prod
    instances and match consumer groups by name. Unlike Kafka, the RocketMQ lag
    API exposes no client IP, so attribution here stays name-based."""
    out = []
    d = aliyun(["rocketmq", "ListInstances"])
    insts = ((d or {}).get("data") or {}).get("list") or []
    prod_insts = [x for x in insts if str(x.get("instanceName", "")).startswith("prod")]
    if len(prod_insts) > scan_limit:
        WARN.append("RocketMQ: %d 个 prod 实例，只扫了前 %d 个（提高 scan_limit）"
                    % (len(prod_insts), scan_limit))
    for inst in prod_insts[:scan_limit]:
        iid, iname = inst.get("instanceId"), inst.get("instanceName")
        own = owns(iname, ns)
        g = aliyun(["rocketmq", "ListConsumerGroups", "--instanceId", iid], timeout=60)
        for grp in ((g or {}).get("data") or {}).get("list") or []:
            gid = grp.get("consumerGroupId") or ""
            if not (own or gid in declared):   # only groups the manifests declare
                continue
            lag = aliyun(["rocketmq", "GetConsumerGroupLag", "--instanceId", iid,
                          "--consumerGroupId", gid], timeout=60)
            t = ((lag or {}).get("data") or {}).get("totalLag") or {}
            ready = t.get("readyCount")
            out.append({"inst": iname, "group": gid, "ready": ready,
                        "inflight": t.get("inflightCount"),
                        "hot": bool(ready and ready > 1000)})
    return out


def kafka_backlog_peak(instances, hours=24):
    """24h backlog peak per consumer group. One DescribeMetricList per instance
    returns every group, so this costs one call per Kafka instance.
    median vs peak is what separates a transient burst from a standing backlog."""
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(hours=hours)
    f = "%Y-%m-%dT%H:%M:%SZ"
    out = {}
    for iid in sorted(instances or []):
        d = aliyun(["cms", "DescribeMetricList", "--Namespace", "acs_kafka",
                    "--MetricName", "MessageAccumulationOnetopicV3", "--Period", "300",
                    "--StartTime", start.strftime(f), "--EndTime", end.strftime(f),
                    "--Dimensions", json.dumps({"instanceId": iid})], timeout=180)
        try:
            pts = json.loads((d or {}).get("Datapoints") or "[]")
        except Exception:
            pts = []
        byg = collections.defaultdict(list)
        for pt in pts:
            byg[(pt.get("consumerGroup"), pt.get("topic"))].append(
                (pt.get("timestamp") or 0, pt.get("Value") or 0))
        for g, series in byg.items():
            series.sort()
            vals = [v for _, v in series]
            if not vals:
                continue
            i = max(range(len(vals)), key=lambda x: vals[x])
            out[g] = {"group": g[0], "topic": g[1], "peak": vals[i],
                      "peak_at": int(series[i][0] / 1000),
                      "median": statistics.median(vals), "last": vals[-1]}
    return out


def k8s_events(ns, hours=24, top=12):
    """Warning-level K8s events for the namespace. These are invisible to
    Prometheus — kube_event_count is not exported — yet they are the only place
    that says *why* something failed (e.g. a CronJob that stopped scheduling)."""
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    q = ("level: Warning | select json_extract_scalar(eventId, '$.reason') as reason, "
         "json_extract_scalar(eventId, '$.involvedObject.kind') as kind, count(*) as cnt, "
         "count(distinct json_extract_scalar(eventId, '$.involvedObject.name')) as objs, "
         "arbitrary(json_extract_scalar(eventId, '$.involvedObject.name')) as sample "
         "where json_extract_scalar(eventId, '$.metadata.namespace') = '%s' "
         "group by reason, kind order by cnt desc limit %d" % (ns, top))
    d = aliyun(["sls", "GetLogs", "--project", SLS_PROJECT, "--logstore", SLS_LOGSTORE,
                "--from", str(now - hours * 3600), "--to", str(now),
                "--query", q, "--line", "100"], timeout=180, quiet=True)
    if d is None:
        WARN.append("K8s 事件（SLS）未取到：FailedScheduling/ImagePullBackOff 等原因不可见")
        return None
    rows = d if isinstance(d, list) else []
    out = []
    for r in rows:
        try:
            cnt = int(r.get("cnt") or 0)
        except ValueError:
            cnt = 0
        reason = r.get("reason")
        try:
            objs = int(r.get("objs") or 0)
        except ValueError:
            objs = 0
        bad = reason in BAD_REASONS
        out.append({"reason": reason, "kind": r.get("kind"), "cnt": cnt, "objs": objs,
                    "sample": r.get("sample"), "bad": bad,
                    "why": BAD_REASONS.get(reason, "")})
    return out


def ns_pod_ips(p, ns):
    """Pod IPs of the namespace, used to prove who really consumes a group."""
    return {s["metric"].get("pod_ip")
            for s in p.q('kube_pod_info{namespace="%s"}' % ns, "now")
            if s["metric"].get("pod_ip")}


def pods_without_ip(p, ns):
    """A pod that stays without an IP has never started — the CNI on its node is
    failing to allocate an address. This is the ONLY signal of that failure: the
    node still reports Ready, no resource pressure, and terway/istio-cni/ztunnel
    all look healthy (observed 2026-09-07 on node 172.16.128.18)."""
    ages = p.by('time() - kube_pod_created{namespace="%s"} > %d' % (ns, STUCK_MIN * 60),
                ("pod",), "now")
    noip = p.by('kube_pod_info{namespace="%s", pod_ip=""}' % ns, ("pod", "node"), "now")
    out = []
    for (pod, node) in noip:
        age = ages.get((pod,))
        if age is None:
            continue
        out.append({"pod": pod, "node": node, "mins": int(age / 60)})
    out.sort(key=lambda x: -x["mins"])
    return out


def cronjobs_from_manifest(ns, root="repos/gitops/apps"):
    """Parse <ns>/cronJobs.jsonnet for job definitions. The two styles need
    different liveness checks:
      * resident jobs (`while true; do curl ...; sleep`) run forever inside one
        Job pod — liveness = a pod exists;
      * periodic jobs — liveness = at least one new Job pod recently."""
    path = os.path.join(root, ns, "cronJobs.jsonnet")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return []
    jobs = []
    matches = list(re.finditer(r"name:\s*'([a-z0-9-]+)',\s*schedule:\s*'([^']+)'", text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():end]
        jobs.append({"name": m.group(1), "schedule": m.group(2).strip(),
                     "resident": "while true" in seg})
    return jobs


def _runs_at_least_daily(sched):
    parts = sched.split()
    # day-of-month and month both wildcard => fires daily or more often
    return len(parts) == 5 and parts[2] == "*" and parts[3] == "*"


def cronjob_health(p, ns):
    """A CronJob that stopped scheduling is invisible to Prometheus (no
    kube_event_count) and TooManyMissedTimes alone is ambiguous — resident
    `while true` jobs emit it forever as expected noise. So check the actual
    failure mode instead: resident jobs must still have a running pod; periodic
    jobs must have produced a Job pod within 24h."""
    out = []
    for j in cronjobs_from_manifest(ns):
        sel = 'namespace="%s", pod=~"%s-[0-9]+-.*"' % (ns, j["name"])
        item = {"name": j["name"], "schedule": j["schedule"],
                "resident": j["resident"], "alive": True, "detail": ""}
        if j["resident"]:
            # A pod can exist yet not be running (Failed/Unknown after a crash,
            # before the controller notices), so liveness means phase Running.
            running = p.by('kube_pod_status_phase{%s,phase="Running"}' % sel,
                           ("pod",), "now")
            live = {pod for pod, v in running.items() if v == 1}
            if not live:
                item["alive"] = False
                item["detail"] = "无 Running Pod（常驻任务停了控制器不会自愈）"
            else:
                starts = p.by('kube_pod_start_time{%s}' % sel, ("pod",), "now")
                ages = [time.time() - v for pod, v in starts.items() if pod in live]
                if ages:
                    item["detail"] = "Pod 已运行 %.0f 天" % (max(ages) / 86400)
        elif _runs_at_least_daily(j["schedule"]):
            # Two distinct failure modes, both invisible to a "did a pod get
            # created" check:
            #   1. the controller stopped scheduling — no pod at all;
            #   2. a pod was created but never started — wedged in a pre-start
            #      waiting state (observed: CNI failure left 17 cron pods in
            #      ContainerCreating with no IP for hours).
            # Do NOT test for a Succeeded pod in the last 24h: an hourly job has
            # plenty of those from earlier runs, which masks the wedged one.
            created = p.num('count(kube_pod_created{%s} > (time() - 86400))' % sel,
                            "now")
            if not created:
                item["alive"] = False
                item["detail"] = "24h 内没有新 Job（可能停止调度）"
            else:
                waiting = p.by('kube_pod_container_status_waiting_reason{%s,'
                               ' reason=~"ContainerCreating|ImagePullBackOff'
                               '|ErrImagePull|CreateContainerConfigError'
                               '|InvalidImageName"} == 1' % sel,
                               ("pod", "reason"), "now")
                ages = p.by('time() - kube_pod_created{%s}' % sel, ("pod",), "now")
                wedged = {k: v for k, v in waiting.items()
                          if ages.get((k[0],), 0) > STUCK_MIN * 60}
                if wedged:
                    item["alive"] = False
                    mins = int(max(ages.get((k[0],), 0) for k in wedged) / 60)
                    item["detail"] = ("最新 Pod 卡在 %s 已 %d 分钟未启动，"
                                      "任务实际未执行"
                                      % ("、".join(sorted({k[1] for k in wedged})),
                                         mins))
        out.append(item)
    return out


def kafka_lag(p, ns, deps):
    """Instance discovery must come from the gitops manifests: goods' Kafka is
    named `goods-prod` / `qnh-prod`, so any name-prefix scan misses it entirely.
    Once the instance is config-derived, every group on it is presumptively the
    namespace's; ClientIp is still used to drop groups consumed from elsewhere."""
    out = []
    own_ips = ns_pod_ips(p, ns)
    names = {}
    d = aliyun(["alikafka", "GetInstanceList"])
    for x in ((d or {}).get("InstanceList") or {}).get("InstanceVO") or []:
        names[x.get("InstanceId")] = x.get("Name")
    for iid in sorted(deps.get("kafka") or []):
        iname = names.get(iid, iid)
        c = aliyun(["alikafka", "GetConsumerList", "--InstanceId", iid], timeout=60)
        for g in ((c or {}).get("ConsumerList") or {}).get("ConsumerVO") or []:
            gid = g.get("ConsumerId") or ""
            pr = aliyun(["alikafka", "GetConsumerProgress", "--InstanceId", iid,
                         "--ConsumerId", gid], timeout=60)
            tl = (((pr or {}).get("ConsumerProgress") or {}).get("TopicList") or {}).get("TopicList") or []
            lag, ips = 0, set()
            for t in tl:
                for o in (t.get("OffsetList") or {}).get("OffsetList") or []:
                    try:
                        lag += int(o.get("Accumulate") or 0)
                    except (TypeError, ValueError):
                        pass
                    cip = str(o.get("ClientIp") or "").lstrip("/")
                    if cip:
                        ips.add(cip)
            if ips and not (ips & own_ips):
                continue          # another namespace's pods consume this group
            out.append({"inst": iname, "group": gid, "lag": lag, "topics": len(tl),
                        "pods": len(ips), "hot": lag > 1000,
                        "idle": len(ips) == 0, "verified": bool(ips & own_ips)})
    return out


# ----------------------------------------------------------------- k8s side
def traffic_peak(p, ns):
    """Peak / trough QPS for the last 24h and the 24h before it."""
    D = 'destination_service_namespace="%s", destination_workload!="waypoint"' % ns
    expr = 'sum(rate(istio_requests_total{%s}[5m]))' % D
    out = {}
    for label, start, end in (("today", "now-24h", "now"), ("prev", "now-48h", "now-24h")):
        res = p.range(expr, start, end, 300)
        vals = []
        for series in res:
            for ts, v in series.get("values", []):
                if v == "NaN":
                    continue
                try:
                    vals.append((int(float(ts)), float(v)))
                except ValueError:
                    pass
        if not vals:
            out[label] = None
            continue
        hi, lo = max(vals, key=lambda x: x[1]), min(vals, key=lambda x: x[1])
        out[label] = {"peak": hi[1], "peak_at": hi[0], "trough": lo[1],
                      "ratio": (hi[1] / lo[1]) if lo[1] > 0 else None,
                      "avg": sum(v for _, v in vals) / len(vals)}
    return out


def service_spikes(p, ns, hours=24):
    """24h aggregates hide spikes entirely: pc-bff's P99 averaged fine while
    intermittently hitting 4.5s. Compare each series' max against its median."""
    D = 'destination_service_namespace="%s", destination_workload!="waypoint"' % ns
    out = []
    for label, expr, unit, abs_th in (
        ("P99",
         'histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s}[5m]))'
         ' by (destination_service_name, le))/1000' % D, "s", 1.0),
        ("5xx",
         '100 * sum(rate(istio_requests_total{%s,response_code=~"5.."}[5m]))'
         ' by (destination_service_name) / sum(rate(istio_requests_total{%s}[5m]))'
         ' by (destination_service_name)' % (D, D), "%", 1.0),
    ):
        for sr in p.range(expr, "now-%dh" % hours, "now", 300):
            svc = sr["metric"].get("destination_service_name")
            vals = [(int(float(t)), float(v)) for t, v in sr.get("values", []) if v != "NaN"]
            if len(vals) < 12:
                continue
            nums = [v for _, v in vals]
            med = statistics.median(nums)
            i = max(range(len(nums)), key=lambda x: nums[x])
            peak, at = nums[i], vals[i][0]
            spiky = peak >= abs_th and (med <= 0.02 or peak >= med * 3)
            if spiky:
                out.append({"svc": svc, "kind": label, "peak": peak, "at": at,
                            "median": med, "unit": unit,
                            "ratio": (peak / med) if med > 0 else None})
    out.sort(key=lambda x: -(x["ratio"] or 1e9))
    return out


def alert_instances(p, ns, at):
    """Alert identity is the full label set; the distinguishing label differs per
    rule (pod, horizontalpodautoscaler, persistentvolumeclaim, node...)."""
    SKIP = {"__name__", "alertstate", "prometheus", "job", "endpoint", "service",
            "container", "uid"}
    TARGET = ("pod", "horizontalpodautoscaler", "persistentvolumeclaim", "deployment",
              "daemonset", "statefulset", "job_name", "cronjob", "node", "instance",
              "device", "mountpoint")
    out = {}
    for s in p.q('ALERTS{alertstate="firing",namespace="%s"}' % ns, at):
        m = s["metric"]
        sig = tuple(sorted((k, v) for k, v in m.items() if k not in SKIP))
        target = next((m[t] for t in TARGET if m.get(t)), "-")
        out[sig] = (m.get("alertname", "-"), m.get("severity", "-"),
                    m.get("namespace", "-"), target)
    return out


def k8s_summary(p, ns):
    """Alerts (24h diff), service QPS/5xx/P99 regression, restart/OOM counts,
    NotReady pods, and the 1h RED snapshot the card's headline numbers come from."""
    warns_before = len(WARN)
    now = alert_instances(p, ns, "now")
    prev = alert_instances(p, ns, "now-24h")
    # A query failure returns [] — an empty `now` with a populated `prev` almost
    # always means the ALERTS query broke, not that every alert recovered at once.
    alert_query_failed = not now and len(WARN) > warns_before
    new = [] if alert_query_failed else [now[s] for s in now if s not in prev]
    gone = [] if alert_query_failed else [prev[s] for s in prev if s not in now]
    rules = collections.Counter((v[0], v[1]) for v in now.values())

    W = "24h"
    D = 'destination_workload!="waypoint",destination_service_namespace="%s"' % ns
    k = ("destination_service_namespace", "destination_service_name")
    q = 'sum(rate(istio_requests_total{%s}[%s])) by (%s, %s)' % (D, W, k[0], k[1])
    qn, qo = p.by(q, k, "now"), p.by(q, k, "now-24h")
    e5 = p.by('sum(rate(istio_requests_total{%s,response_code=~"5.."}[%s])) by (%s, %s)'
              % (D, W, k[0], k[1]), k, "now")
    sig = sorted({x[1] for x in set(qn) | set(qo)
                  if max(qn.get(x, 0), qo.get(x, 0)) >= 0.5 and x[1]})
    pn = po = {}
    if sig:
        pq = ('histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s,'
              'destination_service_name=~"%s"}[%s])) by (%s, %s, le))/1000'
              % (D, "|".join(sig), W, k[0], k[1]))
        pn, po = p.by(pq, k, "now"), p.by(pq, k, "now-24h")

    svc = []
    for key in set(qn) | set(qo):
        a, b = qn.get(key, 0), qo.get(key, 0)
        if max(a, b) < 0.5:
            continue
        flags = []
        dq = pct(a, b)
        if abs(dq) >= TH["qps_pct"]:
            flags.append("QPS %s" % arrow(dq))
        err = 100 * e5.get(key, 0) / a if a else 0
        if err >= TH["err_pct"]:
            flags.append("5xx %.2f%%" % err)
        p9, p9o = pn.get(key), po.get(key)
        if p9 is not None and p9 >= TH["p99_abs"]:
            flags.append("P99 %.2fs" % p9)
        if p9 is not None and p9o is not None:
            dp = pct(p9, p9o)
            if dp != float("inf") and dp >= TH["p99_pct"] and p9 > 0.05:
                flags.append("P99 %.2fs→%.2fs" % (p9o, p9))
        if flags:
            svc.append((key[1], a, flags))
    svc.sort(key=lambda x: -x[1])

    F = 'namespace="%s",container!="",container!="POD"' % ns
    stab = {}
    for lbl, metric in (("restart", "kube_pod_container_status_restarts_total"),
                        ("oom", "container_oom_events_total")):
        stab[lbl] = p.num('sum(max_over_time(%s{%s}[24h]) - min_over_time(%s{%s}[24h]))'
                          % (metric, F, metric, F))

    ready = p.q('kube_pod_status_ready{namespace="%s",condition="false"}==1' % ns, "now")
    succeeded = {m.get("pod") for m, _ in _values(p.q(
        'kube_pod_status_phase{namespace="%s",phase="Succeeded"}==1' % ns, "now"))}
    notready = sorted({m.get("pod", "?") for m, _ in _values(ready)
                       if m.get("pod") not in succeeded})

    return {"alerts": len(now), "alerts_prev": len(prev), "new": new, "gone": gone,
            "rules": rules, "svc": svc, "stab": stab, "peak": traffic_peak(p, ns),
            "spikes": service_spikes(p, ns), "notready": notready,
            "red": red_snapshot(p, ns), "series": int(p.num("count(up)")),
            "cron": cronjob_health(p, ns), "no_ip": pods_without_ip(p, ns)}


def _values(result):
    out = []
    for s in result or []:
        for pair in s.get("values") or ([s["value"]] if s.get("value") else []):
            try:
                out.append((s.get("metric") or {}, float(pair[1])))
            except (ValueError, IndexError, TypeError):
                pass
    return out


def red_snapshot(p, ns, top_n=3):
    """1h RED numbers for the card headline. Request volume below 100 makes any
    ratio meaningless, so it is reported as UNKNOWN rather than 0.000%."""
    D = 'destination_service_namespace="%s",destination_workload!="waypoint"' % ns
    bad = p.sum('sum(increase(istio_requests_total{%s,response_code=~"5.."}[1h]))' % D)
    allr = p.sum('sum(increase(istio_requests_total{%s}[1h]))' % D)
    p99 = []
    expr = ('topk(%d, histogram_quantile(0.99, sum(rate('
            'istio_request_duration_milliseconds_bucket{%s}[5m]))'
            ' by (destination_service_name, le))/1000)' % (top_n, D))
    for metric, v in _values(p.q(expr)):
        p99.append((metric.get("destination_service_name", "?"), v * 1000))
    p99.sort(key=lambda x: -x[1])
    return {"bad": bad, "all": allr, "ratio": (bad / allr * 100) if allr else None,
            "sampled": allr >= 100, "p99": p99}