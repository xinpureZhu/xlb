#!/usr/bin/env python3
"""Namespace-scoped daily inspection: Kubernetes + owned cloud resources,
delivered as a Lark interactive card.

Ownership comes from the live instance naming convention `env--namespace--resource`.
Sending is opt-in: without --send the message is only previewed as text.
"""
import argparse
import collections
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_inspect as di  # noqa: E402  (reuse Prom client + alert diff)
import gitops_deps  # noqa: E402  (namespace -> middleware, from gitops manifests)

TZ = 8 * 3600
REGION = "cn-hangzhou"
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


def now_cst():
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=TZ)


def cst(ts):
    return datetime.datetime.fromtimestamp(ts + TZ, datetime.UTC).strftime("%m-%d %H:%M")


# Set when the aliyun CLI cannot authenticate at all (expired OAuth token).
# Every cloud section then returns empty, which would otherwise render as a
# healthy "无数据" — the same trap as printing a failed query as 0.
CRED_BROKEN = []

# Set when cloud calls fail for network/TLS reasons rather than credentials.
# Kept apart from CRED_BROKEN: "凭据失效" sends people to re-run `aliyun
# configure`, which cannot fix a blocked endpoint (2026-10-27: the global
# sts.aliyuncs.com gateway reset the TLS handshake while every product
# endpoint and the regional STS endpoint were reachable).
CLOUD_UNREACHABLE = []

REGIONAL_STS_ENDPOINT = "sts.%s.aliyuncs.com" % REGION

# The CLI echoes the signed request URL in some errors, and that URL carries the
# live STS token; warnings end up in Lark cards and log files.
_SECRET_PARAM_RE = re.compile(
    r'(?i)\b(access_key_id|accesskeyid|access_key_secret|accesskeysecret|'
    r'sts_token|securitytoken|signature|oauth_access_token|'
    r'oauth_refresh_token|access_token|refresh_token)"?\s*[=:]\s*"?[^&"\s,}]*')


def _redact(text):
    """Remove credentials from CLI output before it is logged or surfaced."""
    return _SECRET_PARAM_RE.sub(r"\1=***", text or "")


# Errors that mean the credential itself is unusable, not the network.
_CRED_ERR_HINTS = ("refresh token", "invalidcredential", "invalidaccesskeyid",
                   "invalidsecuritytoken", "securitytokenexpired",
                   "unauthorized", "forbidden", "no credential",
                   "init client failed", "init openapi client failed")

# Errors that mean the request never reached the service.
_NET_ERR_HINTS = ("ssl", "eof", "connection reset", "connection refused",
                  "connection aborted", "timeout", "timed out",
                  "no such host", "dial tcp")


def aliyun(args, timeout=120):
    try:
        r = subprocess.run(["aliyun"] + args + ["--region", REGION],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    err = _redact(r.stderr or "")
    if r.returncode != 0 and not CRED_BROKEN and not CLOUD_UNREACHABLE:
        low = err.lower()
        if any(h in low for h in _CRED_ERR_HINTS):
            CRED_BROKEN.append(err.strip()[:120])
            di._warn.append("阿里云凭据失效，云侧检查全部跳过（MongoDB/Redis/MQ 显示「无数据」"
                            "不代表健康）：%s" % err.strip()[:90])
        elif any(h in low for h in _NET_ERR_HINTS):
            CLOUD_UNREACHABLE.append(err.strip()[:120])
            di._warn.append("阿里云接口不可达（网络/TLS，非凭据问题；相应云检查未完成，"
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
    """Cheap credential check before running ~5 minutes of cloud queries.
    Returns True when the aliyun CLI can authenticate.

    The CLI defaults to the global sts.aliyuncs.com gateway; when that is
    unreachable the regional endpoint is tried before blaming the credential.
    Both endpoints serve the same identity, so a success on either means the
    product calls below (which use their own endpoints) will work."""
    err = ""
    for endpoint in (None, REGIONAL_STS_ENDPOINT):
        ok, err = sts_identity(endpoint)
        if ok:
            return True
    low = err.lower()
    if any(h in low for h in _CRED_ERR_HINTS):
        CRED_BROKEN.append(err)
        di._warn.append("阿里云凭据失效（aliyun sts GetCallerIdentity 失败），"
                        "云侧检查全部跳过：%s" % err[:90])
    else:
        CLOUD_UNREACHABLE.append(err)
        di._warn.append("阿里云 STS 端点不可达（网络/TLS 阻断，非凭据失效；已试 %s），"
                        "云侧检查全部跳过：%s" % (REGIONAL_STS_ENDPOINT, err[:90]))
    return False


def range_q(p, expr, start, end, step):
    """daily_inspect.Prom only does instant queries; peak detection needs a range."""
    import time as _time
    url = p.url.replace("/api/v1/query", "/api/v1/query_range")
    params = {"query": expr, "start": start, "end": end, "step": step}
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"Accept": "application/json"})
    if p.token:
        req.add_header("Authorization", "Bearer " + p.token)
    body, last_err = None, None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=p.timeout) as r:
                body = json.load(r)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt == 1:
                _time.sleep(2)   # transient SSL EOF / connection reset: retry once
    if last_err is not None:
        di._warn.append("range query failed: %s" % str(last_err)[:80])
        return []
    if body.get("status") != "success":
        return []
    return body["data"]["result"]


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


def _yi(v):
    if not v:
        return "0"
    return "%.2f 亿" % (v / 1e8) if v >= 1e8 else "%.0f 万" % (v / 1e4)


# ----------------------------------------------------------------- cloud side
def mongo_health(ns):
    """24h CPU / memory peak per shard. Nothing else — slow-log counting and disk
    skew were dropped to keep the report to a single screen (and it removes the
    slowest API call)."""
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


def mq_lag(ns, declared, own_ips=frozenset(), scan_limit=20):
    """Namespaces often consume from other namespaces' instances, so scan prod
    instances and match consumer groups by name. Unlike Kafka, the RocketMQ lag
    API exposes no client IP, so attribution here stays name-based."""
    out = []
    d = aliyun(["rocketmq", "ListInstances"])
    insts = ((d or {}).get("data") or {}).get("list") or []
    prod_insts = [x for x in insts if str(x.get("instanceName", "")).startswith("prod")]
    if len(prod_insts) > scan_limit:
        di._warn.append("RocketMQ: %d 个 prod 实例，只扫了前 %d 个（提高 scan_limit）"
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
            # ListConsumerConnections returns nothing for these groups in practice,
            # so RocketMQ attribution stays unverified rather than assumed correct.
            conn = aliyun(["rocketmq", "ListConsumerConnections", "--instanceId", iid,
                           "--consumerGroupId", gid], timeout=60)
            ips = {str(c.get("clientIp") or c.get("clientId") or "").split(":")[0]
                   for c in ((conn or {}).get("data") or {}).get("list") or []}
            out.append({"inst": iname, "group": gid, "ready": ready,
                        "inflight": t.get("inflightCount"),
                        "hot": bool(ready and ready > 1000),
                        "ips": {i for i in ips if i}})
    return out


def ns_keywords(p, ns):
    """Consumer group names rarely contain the namespace (goods' groups are called
    official_store_price_dts_group etc.), so derive candidate keywords from the
    namespace's own workload names and use those as the prefilter."""
    import re as _re
    kws = {ns, ns.replace("-", "_")}
    for sr in p.q('count(kube_pod_owner{namespace="%s",owner_kind="ReplicaSet"}) by (owner_name)'
                  % ns, "now"):
        wl = _re.sub(r"-[0-9a-z]{5,10}$", "", sr["metric"].get("owner_name", ""))
        if not any(t in wl for t in ("mqconsumer", "consumer", "sync", "subscriber")):
            continue
        base = _re.sub(r"-(gray|mqconsumer|consumer|subscriber)$", "", wl)
        for cand in (base, _re.sub(r"-sync$", "", base)):
            k = cand.replace("-", "_")
            # single-token keywords are far too broad: stripping suffixes off
            # task-sync-mqconsumer yields "task", which matches marketing-task-log.
            if "_" in k and len(k) >= 6:
                kws.add(k)
    return kws


def kafka_backlog_peak(instances, hours=24):
    """24h backlog peak per consumer group. One DescribeMetricList per instance
    returns every group, so this costs one call per Kafka instance.
    median vs peak is what separates a transient burst from a standing backlog."""
    import statistics
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
                "--query", q, "--line", "100"], timeout=180)
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
    return {s["metric"].get("pod_ip") for s in p.q('kube_pod_info{namespace="%s"}' % ns, "now")
            if s["metric"].get("pod_ip")}


def node_cni_blast_radius(p, nodes):
    """For each node with stuck pods, count how many pods cluster-wide are wedged
    on it and how many namespaces they span. A CNI failure is a node problem,
    not this namespace's problem, and the report must say so — otherwise the
    reader debugs their own workload while 9 other namespaces silently stop."""
    if not nodes:
        return {}
    stuck = p.by('kube_pod_container_status_waiting_reason{reason=~'
                 '"ContainerCreating|ImagePullBackOff|ErrImagePull'
                 '|CreateContainerConfigError|InvalidImageName"} == 1',
                 ("pod",), "now")
    info = p.by('kube_pod_info{pod_ip=""}', ("pod", "node", "namespace"), "now")
    wedged = {pod for (pod,) in stuck}
    out = {}
    for (pod, node, ns) in info:
        if node in nodes and pod in wedged:
            e = out.setdefault(node, {"pods": 0, "namespaces": set()})
            e["pods"] += 1
            e["namespaces"].add(ns)
    return {n: {"pods": e["pods"], "nss": sorted(e["namespaces"])}
            for n, e in out.items()}


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


def cronjobs_from_manifest(ns):
    """Parse repos/gitops/apps/<ns>/cronJobs.jsonnet for job definitions. The two
    styles need different liveness checks:
      * resident jobs (`while true; do curl ...; sleep`) run forever inside one
        Job pod — liveness = a pod exists;
      * periodic jobs — liveness = at least one new Job pod recently."""
    path = os.path.join("repos", "gitops", "apps", ns, "cronJobs.jsonnet")
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
            # before the controller notices), so liveness means phase Running,
            # not just kube_pod_start_time.
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
            #      ContainerCreating with no IP for hours, so the cron silently
            #      stopped delivering while looking scheduled).
            # Do NOT test for a Succeeded pod in the last 24h: an hourly job has
            # plenty of those from earlier runs, which masks the wedged one.
            created = p.num('count(kube_pod_created{%s} > (time() - 86400))' % sel,
                            "now")
            if not created:
                item["alive"] = False
                item["detail"] = "24h 内没有新 Job（可能停止调度）"
            else:
                # Query waiting_reason on its own: it carries the `reason`
                # label. Joining with `and on(pod) group_left(reason)` returns
                # nothing here, and a plain `and on(pod)` drops the label.
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


def _ip_owner(p, ip, cache):
    if ip not in cache:
        r = p.q('kube_pod_info{pod_ip="%s"}' % ip, "now")
        cache[ip] = r[0]["metric"].get("namespace") if r else None
    return cache[ip]


def kafka_lag(p, ns, deps):
    """Instance discovery must come from the gitops manifests: goods' Kafka is
    named `goods-prod` / `qnh-prod`, so any name-prefix scan misses it entirely.
    Once the instance is config-derived, every group on it is presumptively the
    namespace's; ClientIp is still used to drop groups consumed from elsewhere."""
    out, mis = [], []
    own_ips, cache = ns_pod_ips(p, ns), {}
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
                other = {_ip_owner(p, i, cache) for i in list(ips)[:3]} - {None}
                mis.append({"group": gid, "inst": iname,
                            "actual": "、".join(sorted(other)) or "未知"})
                continue
            out.append({"inst": iname, "group": gid, "lag": lag, "topics": len(tl),
                        "pods": len(ips), "hot": lag > 1000,
                        "idle": len(ips) == 0, "verified": bool(ips & own_ips)})
    return out, mis


# ----------------------------------------------------------------- k8s side
def traffic_peak(p, ns):
    """Peak / trough QPS for the last 24h and the 24h before it."""
    D = 'destination_service_namespace="%s", destination_workload!="waypoint"' % ns
    expr = 'sum(rate(istio_requests_total{%s}[5m]))' % D
    out = {}
    for label, start, end in (("today", "now-24h", "now"), ("prev", "now-48h", "now-24h")):
        res = range_q(p, expr, start, end, "300s")
        vals = []
        if res:
            for ts, v in res[0].get("values", []):
                if v != "NaN":
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
    import statistics
    D = 'destination_service_namespace="%s", destination_workload!="waypoint"' % ns
    out = []
    for label, expr, unit, abs_th in (
        ("P99",
         'histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s}[5m]))'
         ' by (destination_service_name, le))/1000' % D, "s", 1.0),
        ("5xx",
         '100 * sum(rate(istio_requests_total{%s,response_code=~"5.."}[5m]))'
         ' by (destination_service_name) / sum(rate(istio_requests_total{%s}[5m]))'
         ' by (destination_service_name)' % (D, D), "%%", 1.0),
    ):
        for sr in range_q(p, expr, "now-%dh" % hours, "now", "300s"):
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


def k8s_summary(p, ns):
    di.NS = ns
    warns_before = len(di._warn)
    now = di.alert_instances(p, "now")
    prev = di.alert_instances(p, "now-24h")
    # A query failure returns [] — an empty `now` with a populated `prev` almost
    # always means the ALERTS query broke, not that every alert recovered at once.
    alert_query_failed = not now and len(di._warn) > warns_before
    new = [] if alert_query_failed else [now[s] for s in now if s not in prev]
    gone = [] if alert_query_failed else [prev[s] for s in prev if s not in now]
    rules = collections.Counter((v[0], v[1]) for v in now.values())

    W = "24h"
    D = 'destination_workload!="waypoint"' + di.scope("destination_service_namespace")
    k = ("destination_service_namespace", "destination_service_name")
    q = 'sum(rate(istio_requests_total{%s}[%s])) by (%s, %s)' % (D, W, k[0], k[1])
    qn, qo = p.by(q, k, "now"), p.by(q, k, "now-24h")
    e5 = p.by('sum(rate(istio_requests_total{%s,response_code=~"5.."}[%s])) by (%s, %s)'
              % (D, W, k[0], k[1]), k, "now")
    sig = sorted({x[1] for x in set(qn) | set(qo) if max(qn.get(x, 0), qo.get(x, 0)) >= 0.5 and x[1]})
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
        dq = di.pct(a, b)
        if abs(dq) >= di.TH["qps_pct"]:
            flags.append("QPS %s" % di.arrow(dq))
        err = 100 * e5.get(key, 0) / a if a else 0
        if err >= di.TH["err_pct"]:
            flags.append("5xx %.2f%%" % err)
        p9, p9o = pn.get(key), po.get(key)
        if p9 is not None and p9 >= di.TH["p99_abs"]:
            flags.append("P99 %.2fs" % p9)
        if p9 is not None and p9o is not None:
            dp = di.pct(p9, p9o)
            if dp != float("inf") and dp >= di.TH["p99_pct"] and p9 > 0.05:
                flags.append("P99 %.2fs→%.2fs" % (p9o, p9))
        if flags:
            svc.append((key[1], a, flags))
    svc.sort(key=lambda x: -x[1])

    F = 'namespace!="",container!="",container!="POD"' + di.scope()
    stab = {}
    for lbl, m in (("restart", "kube_pod_container_status_restarts_total"),
                   ("oom", "container_oom_events_total")):
        stab[lbl] = p.num('sum(max_over_time(%s{%s}[24h]) - min_over_time(%s{%s}[24h]))'
                          % (m, F, m, F))
    return {"alerts": len(now), "alerts_prev": len(prev), "new": new, "gone": gone,
            "rules": rules, "svc": svc, "stab": stab, "peak": traffic_peak(p, ns),
            "spikes": service_spikes(p, ns)}


def _issue_key(cat, text):
    """Stable identity for an issue across days: category + first backticked
    subject. Peak values drift slightly, so the full text is not usable."""
    m = re.search(r"`([^`]+)`", text)
    return "%s|%s" % (cat, m.group(1) if m else text[:24])


def mark_recurrence(d, persist=False, unverified=()):
    """The same spike/backlog appearing every day for weeks reads as noise, but
    it is a standing defect. Tag streaks so recurrence escalates instead of
    fading into the background.

    `persist` must only be true for the authoritative daily run (--send). A
    preview sees a different data window than the 09:00 scheduled run (e.g. a
    backlog that has since drained), so letting previews write would clobber
    the production streaks — which silently reset a 4-day backlog to day 1.

    `unverified` lists issue categories whose check did not run at all (e.g.
    积压 when the cloud credentials are broken). Absence there means "unknown",
    not "resolved", so those keys are carried over instead of dropped — the same
    rule as never rendering a failed query as a recovery.
    """
    today = now_cst().date()
    state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "%s.recurrence.json" % d["ns"])
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except Exception:
        prev = {}
    prev_keys, prev_date = prev.get("keys") or {}, prev.get("date")
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    new_keys = {}
    for i, (cat, text) in enumerate(d["issues"]):
        k = _issue_key(cat, text)
        if prev_date == yesterday:
            streak = prev_keys.get(k, 0) + 1
        elif prev_date == today.isoformat():
            # Same-day re-run: keep the recorded streak rather than inflating it.
            # Fall back to 1 only if this issue was not seen earlier today.
            streak = max(prev_keys.get(k, 1), 1)
        else:
            streak = 1
        new_keys[k] = streak
        if streak >= 2:
            d["issues"][i] = (cat, "%s（已连续 %d 天）" % (text, streak))
    if not persist:
        return d
    # Carry over categories that were never checked, so a broken credential or a
    # skipped cloud section does not read as "the backlog cleared overnight".
    carried = {k: v for k, v in prev_keys.items()
               if k.split("|", 1)[0] in set(unverified) and k not in new_keys}
    if prev_date == today.isoformat():
        # Same-day merge: never erase what an earlier authoritative run recorded.
        merged = dict(prev_keys)
        merged.update(new_keys)
    else:
        merged = dict(carried)
        merged.update(new_keys)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"date": today.isoformat(), "keys": merged},
                      fh, ensure_ascii=False)
    except Exception:
        pass
    return d


# ----------------------------------------------------------------- build/render
def build(ns, k, mongo, redis, mq, kafka, kmis, events, backlog, cron=None,
          no_ip=None):
    """The actionable list *is* the report; everything else is one-line context."""
    issues = []
    for name, sev, _n, tgt in [v for v in k["new"] if v[1] in ("critical", "error")]:
        issues.append(("告警", "%s `%s`" % (name, tgt)))

    # Aggregate by node: a CNI failure wedges every new pod on that node at once,
    # and 13 near-identical rows would bury the one fact that matters (which node).
    by_node = collections.OrderedDict()
    for x in no_ip or []:
        by_node.setdefault(x["node"], []).append(x)
    for node, pods in by_node.items():
        worst = max(p["mins"] for p in pods)
        issues.append(("网络", "%d 个 Pod 在节点 `%s` 上已 %d 分钟拿不到 IP — "
                       "CNI 未分配地址，新 Pod 全部无法启动（节点仍报 Ready）"
                       % (len(pods), node, worst)))

    for j in cron or []:
        if not j["alive"]:
            if j["resident"]:
                hint = "常驻任务停了控制器不会自愈，需人工删 Pod 触发重建"
            elif "卡在" in j["detail"]:
                # Pod was created but never started: a node/network problem,
                # not a scheduler one. Saying "check the scheduler" sends the
                # reader to the wrong place.
                hint = "Pod 已创建但未启动，查所在节点的 CNI/IP 分配（见「网络」项）"
            else:
                hint = "检查调度器与 suspend 状态"
            issues.append(("CronJob", "`%s` %s — %s" % (j["name"], j["detail"], hint)))

    for e in events:
        if not e["bad"]:
            continue
        where = "%d 个 %s" % (e["objs"], e["kind"] or "对象")
        if e["objs"] == 1 and e.get("sample"):
            where = "`%s`" % e["sample"]
        issues.append(("事件", "%s ×%d（%s）— %s" % (e["reason"], e["cnt"], where, e["why"])))

    if k["stab"]["oom"] > 0:
        issues.append(("OOM", "%.0f 次 / 24h" % k["stab"]["oom"]))
    if k["stab"]["restart"] > 100:
        issues.append(("重启", "%.0f 次 / 24h" % k["stab"]["restart"]))

    for sp in k.get("spikes") or []:
        issues.append(("突刺", "`%s` %s 峰值 **%.2f%s**（中位 %.2f）@ %s"
                       % (sp["svc"], sp["kind"], sp["peak"],
                          sp["unit"].replace("%%", "%"), sp["median"], cst(sp["at"]))))

    for name, qps, flags in k["svc"]:
        signal = [f for f in flags if f.startswith(("5xx", "P99"))]
        if signal or qps >= 50:
            issues.append(("服务", "`%s` %.0f qps — %s" % (name, qps, "；".join(flags))))

    for m in mongo:
        issues += [("MongoDB", n) for n in m["note"]]
    for r in redis:
        issues += [("Redis", n) for n in r["note"]]

    for x in backlog:
        if x["median"] > 50:      # sustained: consumers cannot keep up
            issues.append(("积压", "`%s` 持续积压，中位 %.0f / 当前 %.0f（topic `%s`）"
                           % (x["group"], x["median"], x["last"], x["topic"])))
    for m in mq:
        if m["hot"]:
            issues.append(("积压", "`%s` 待消费 %s（RocketMQ，归属未校验）"
                           % (m["group"], m["ready"])))

    hard = any(c in ("告警", "OOM", "CronJob", "网络") for c, _ in issues) or \
        any(e["bad"] for e in events)
    level = "red" if hard else ("orange" if issues else "green")
    return {"ns": ns, "k": k, "issues": issues, "level": level, "events": events,
            "backlog": backlog, "mongo": mongo, "redis": redis, "kafka_n": len(kafka),
            "cron": cron or [], "no_ip": no_ip or []}


def _peak(pk):
    t, pv = pk.get("today"), pk.get("prev")
    if not t:
        return None
    line = "**%.0f** qps @ %s" % (t["peak"], cst(t["peak_at"]))
    sub = "谷 %.0f　峰谷比 %s" % (t["trough"], ("%.0f×" % t["ratio"]) if t["ratio"] else "n/a")
    if pv:
        dd = di.pct(t["peak"], pv["peak"])
        if dd != float("inf"):
            sub += "　环比 %s" % di.arrow(dd)
    return line, sub


def _fields(d):
    """(label, value, is_short). Multi-line values take a full row: at half
    width they wrap mid-value and the two columns drift out of alignment."""
    k = d["k"]
    pk = _peak(k["peak"])
    return [("告警", "firing **%d**　新增 %d / 恢复 %d"
             % (k["alerts"], len(k["new"]), len(k["gone"])), True),
            ("重启 / OOM", "**%.0f** / **%.0f** 次 24h"
             % (k["stab"]["restart"], k["stab"]["oom"]), True),
            ("昨日峰值 QPS", "%s\n%s" % pk if pk else "无 Istio 流量", False),
            ("中间件峰值 24h", _mw_field(d), False),
            ("MQ 积压峰值", _backlog_field(d), False)]


def _no_cloud():
    """Label for cloud sections when the aliyun CLI could not be queried."""
    if CRED_BROKEN:
        return "⚠️ 凭据失效未检查"
    if CLOUD_UNREACHABLE:
        return "⚠️ 云端未检查"
    return "无数据"


def _backlog_field(d):
    bl = d.get("backlog") or []
    if not bl:
        return _no_cloud()
    top = bl[0]
    cur = max(bl, key=lambda x: x["last"])
    lines = ["24h **%.0f** @ `%s` %s" % (top["peak"], top["group"], cst(top["peak_at"]))]
    if cur["last"] > 0:
        lines.append("当前最高 **%.0f** @ `%s` / topic `%s`"
                     % (cur["last"], cur["group"], cur["topic"]))
    return "\n".join(lines)


def _mw_field(d):
    parts = []
    for m in d.get("mongo") or []:
        parts.append("Mongo " + "　".join(
            "分片%d CPU %s 内存 %s" % (i, _pc(x["cpu"]), _pc(x["mem"]))
            for i, x in enumerate(m["shards"], 1)))
    for r in d.get("redis") or []:
        parts.append("Redis CPU %s 内存 %s" % (_pc(r["cpu"], 2), _pc(r["mem"])))
    return "\n".join(parts) or _no_cloud()


def _issue_rows(issues):
    """Group by category so a repeated label becomes one heading instead of
    being restated on every line. Single-item categories stay inline."""
    groups = collections.OrderedDict()
    for cat, text in issues:
        groups.setdefault(cat, []).append(text)
    rows = []
    for cat, texts in groups.items():
        if len(texts) == 1:
            rows.append("**%s**　%s" % (cat, texts[0]))
        else:
            rows.append("**%s**" % cat)
            rows += ["　• %s" % t for t in texts]
    return rows


def _warnings():
    return list(dict.fromkeys(di._warn))


def effective_level(d):
    """Missing data is not a healthy signal: never report green while any check
    failed to run, otherwise a query failure reads as an all-clear
    (observed 2026-08-16/17: green card, spike detection had no data)."""
    if d["level"] == "green" and _warnings():
        return "orange"
    return d["level"]


def render_text(d):
    mark = {"red": "[红]", "orange": "[橙]", "green": "[绿]"}[effective_level(d)]
    L = ["%s %s 巡检 · %s CST" % (mark, d["ns"], now_cst().strftime("%m-%d %H:%M"))]
    if d["issues"]:
        L.append("待处理 %d 项：" % len(d["issues"]))
        L += ["  - [%s] %s" % (c, t.replace("**", "").replace("`", "")) for c, t in d["issues"]]
    elif _warnings():
        L.append("未发现问题，但部分检查未完成 ⚠️（详见数据质量说明）")
    else:
        L.append("无需处置")
    L.append("")
    L += ["  %s: %s" % (a, b.replace("**", "").replace("\n", " / "))
          for a, b, _ in _fields(d)]
    return "\n".join(L)


def render_card(d):
    E = []
    warns = list(dict.fromkeys(di._warn))
    if d["issues"]:
        shown = d["issues"][:8]
        rows = "\n".join(_issue_rows(shown))
        more = "\n…另 %d 项" % (len(d["issues"]) - 8) if len(d["issues"]) > 8 else ""
        E.append({"tag": "div", "text": {"tag": "lark_md",
                  "content": "**待处理 %d 项**\n%s%s" % (len(d["issues"]), rows, more)}})
    elif warns:
        # No issues found, but some checks did not run. Claiming ✅ here is how a
        # query failure becomes a false green (observed 2026-08-16/17).
        E.append({"tag": "div", "text": {"tag": "lark_md",
                  "content": "**未发现问题，但部分检查未完成** ⚠️\n详见下方数据质量说明"}})
    else:
        E.append({"tag": "div", "text": {"tag": "lark_md", "content": "**无需处置** ✅"}})
    E.append({"tag": "hr"})
    E.append({"tag": "div", "fields": [
        {"is_short": short, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (a, b)}}
        for a, b, short in _fields(d)]})
    if warns:
        E.append({"tag": "hr"})
        E.append({"tag": "div", "text": {"tag": "lark_md",
                  "content": "**⚠️ 数据质量（%d 项）**\n%s"
                             % (len(warns), "\n".join("• %s" % w for w in warns[:6]))}})
    E.append({"tag": "note", "elements": [{"tag": "lark_md", "content":
              "对比最近 24h vs 前 24h · 突刺为 5 分钟粒度峰值/中位 · "
              "积压取云监控 24h 峰值 · 仍缺业务指标与证书到期"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": effective_level(d),
                       "title": {"tag": "plain_text",
                                 "content": "%s 巡检 · %s"
                                            % (d["ns"], now_cst().strftime("%m-%d %H:%M"))}},
            "elements": E}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("namespace")
    ap.add_argument("--send", action="store_true", help="真正发送（默认只预览）")
    ap.add_argument("--user-id", help="飞书私聊 open_id (ou_xxx)")
    ap.add_argument("--chat-id", help="飞书群 chat_id (oc_xxx)")
    ap.add_argument("--grafana", default=di.DEFAULT_GRAFANA)
    ap.add_argument("--datasource", default=di.DEFAULT_DS)
    ap.add_argument("--token")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--skip-cloud", action="store_true")
    ap.add_argument("--idempotency-key", help="默认 <ns>-<日期>，同一天重复跑不会重复发")
    ap.add_argument("--print-card", action="store_true", help="打印卡片 JSON 便于调试")
    a = ap.parse_args()

    ns = a.namespace
    p = di.Prom(a.grafana, a.datasource, a.token, a.timeout)
    sys.stderr.write("[1/8] K8s 指标、峰值、突刺...\n")
    k = k8s_summary(p, ns)
    mongo = redis = mq = kafka = kmis = events = backlog = []
    cron = cronjob_health(p, ns)
    no_ip = pods_without_ip(p, ns)
    cloud_ok = preflight_cloud()
    if a.skip_cloud:
        cloud_ok = False
    if cloud_ok:
        sys.stderr.write("[2/8] MongoDB...\n")
        mongo = mongo_health(ns)
        sys.stderr.write("[3/8] Redis...\n")
        redis = redis_health(ns)
        deps = gitops_deps.collect(ns)
        sys.stderr.write("[4/8] RocketMQ(%d 个声明的消费组)...\n" % len(deps["groups"]))
        mq = mq_lag(ns, deps["groups"], own_ips=ns_pod_ips(p, ns))
        sys.stderr.write("[5/8] Kafka 消费组...\n")
        kafka, kmis = kafka_lag(p, ns, deps)
        sys.stderr.write("[6/8] Kafka 积压峰值(24h)...\n")
        backlog = sorted((kafka_backlog_peak(deps["kafka"]) or {}).values(),
                         key=lambda x: -x["peak"])

        sys.stderr.write("[7/8] K8s 事件(SLS)...\n")
        events = k8s_events(ns)
    sys.stderr.write("[8/8] 汇总报告...\n")
    d = build(ns, k, mongo, redis, mq, kafka, kmis, events, backlog, cron, no_ip)
    # Categories whose check never ran are "unknown", not "resolved": carrying
    # them over stops a broken credential from reading as an overnight recovery.
    unverified = () if cloud_ok else ("积压", "MongoDB", "Redis", "事件")
    # Only the authoritative daily run (--send) may persist streaks; a preview
    # works off a different data window and would corrupt the recorded history.
    d = mark_recurrence(d, persist=bool(a.send), unverified=unverified)
    print(render_text(d))
    if a.print_card:
        print("\n--- card json ---")
        print(json.dumps(render_card(d), ensure_ascii=False, indent=2))
    if di._warn:
        print("\n(数据质量: %s)" % "; ".join(dict.fromkeys(di._warn)))

    if not a.send:
        sys.stderr.write("\n--- 未发送（加 --send 才会发出）---\n")
        return
    if not (a.user_id or a.chat_id):
        raise SystemExit("ERROR: --send 需要 --user-id 或 --chat-id")
    target = ["--user-id", a.user_id] if a.user_id else ["--chat-id", a.chat_id]
    key = a.idempotency_key or "%s-%s" % (ns, now_cst().strftime("%Y%m%d"))
    r = subprocess.run(["lark-cli", "im", "+messages-send"] + target +
                       ["--msg-type", "interactive",
                        "--content", json.dumps(render_card(d), ensure_ascii=False),
                        "--idempotency-key", key, "--as", "bot"],
                       capture_output=True, text=True, timeout=120)
    try:
        res = json.loads(r.stdout)
    except Exception:
        raise SystemExit("发送失败: %s" % ((r.stdout or r.stderr)[:300]))
    if not res.get("ok"):
        raise SystemExit("发送失败: %s" % json.dumps(res, ensure_ascii=False)[:300])
    info = res.get("data") or {}
    print("\n[已发送] message_id=%s  idempotency-key=%s  %s"
          % (info.get("message_id"), key, info.get("create_time")))


if __name__ == "__main__":
    main()
