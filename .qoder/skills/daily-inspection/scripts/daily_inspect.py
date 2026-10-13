#!/usr/bin/env python3
"""Daily monitoring inspection: start from firing alerts, diff against 24h ago,
drill into per-endpoint latency, and state blind spots explicitly.
Reports only what changed or breaches a threshold."""

import argparse
import collections
import datetime
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_GRAFANA = "https://grafana.observability.ali-prod.xlbsoft.com"
DEFAULT_DS = "prometheus"
TZ = 8 * 3600

# Report thresholds. Anything below these is silence.
TH = {
    "qps_pct": 30.0,      # traffic change worth mentioning
    "p99_pct": 50.0,      # latency regression
    "p99_abs": 2.0,       # chronic slowness; below this, rely on regression + alert rules
    "err_pct": 0.1,       # 5xx share of traffic
    "res_pct": 50.0,      # cpu/mem change
    "conc": 5.0,          # endpoint concurrency (rate of _sum) worth listing
    "lat_abs": 0.5,       # endpoint avg latency seconds
}

# Above this average duration an endpoint is a stream (SSE / long-poll), not a slow request.
STREAM_SEC = 60.0

# Namespace scope. Empty = whole cluster. Set from --namespace.
NS = ""


def scope(label="namespace"):
    """Matcher fragment appended inside an existing {...}, e.g. ',namespace="goods"'."""
    return ',%s="%s"' % (label, NS) if NS else ""


def sel(metric):
    """Metric selector carrying the scope, e.g. 'foo{namespace="goods"}' or 'foo'."""
    return '%s{namespace="%s"}' % (metric, NS) if NS else metric

HASH = __import__("re").compile(r"(-[0-9a-f]{6,10})?(-[0-9a-z]{5})$")


def workload_of(pod):
    """retail-member-pos-gray-6cf4648b9-htrks -> retail-member-pos-gray"""
    return HASH.sub("", pod or "") or pod

_warn = []


class Prom:
    def __init__(self, base, ds, token, timeout):
        self.url = "%s/api/datasources/proxy/uid/%s/api/v1/query" % (base.rstrip("/"), ds)
        self.token = token
        self.timeout = timeout

    def q(self, expr, at="now"):
        params = {"query": expr, "time": at}
        req = urllib.request.Request(self.url + "?" + urllib.parse.urlencode(params),
                                     headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        body, last_err = None, None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = json.load(r)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                raise SystemExit("ERROR: Grafana HTTP %s — %s\n  Hint: check --grafana/--datasource, "
                                 "or pass --token if anonymous read is disabled."
                                 % (e.code, e.read()[:160].decode("utf-8", "replace")))
            except Exception as e:
                last_err = e
                if attempt == 1:
                    time.sleep(2)   # transient SSL EOF / connection reset: retry once
        if last_err is not None:
            _warn.append("query timeout/failure: %s" % str(last_err)[:100])
            return []
        if body.get("status") != "success":
            _warn.append("query failed: %s" % str(body)[:120])
            return []
        return body["data"]["result"]

    def num(self, expr, at="now", default=0.0):
        r = self.q(expr, at)
        try:
            return float(r[0]["value"][1])
        except (IndexError, ValueError, KeyError, TypeError):
            return default

    def by(self, expr, keys, at="now"):
        """-> {tuple(label values): float}"""
        out = {}
        for s in self.q(expr, at):
            m = s["metric"]
            k = tuple(m.get(k, "") for k in keys)
            try:
                out[k] = float(s["value"][1])
            except (ValueError, TypeError):
                pass
        return out


def pct(new, old):
    if old == 0:
        return float("inf") if new > 0 else 0.0
    return (new - old) / old * 100


def arrow(p):
    if p == float("inf"):
        return "新增"
    return ("+%.0f%%" % p) if p >= 0 else ("%.0f%%" % p)


def h(title):
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


# ---------------------------------------------------------------- alerts
def alert_instances(p, at):
    """Alert identity is the full label set; the distinguishing label differs per
    rule (pod, horizontalpodautoscaler, persistentvolumeclaim, node...)."""
    SKIP = {"__name__", "alertstate", "prometheus", "job", "endpoint", "service",
            "container", "uid"}
    TARGET = ("pod", "horizontalpodautoscaler", "persistentvolumeclaim", "deployment",
              "daemonset", "statefulset", "job_name", "cronjob", "node", "instance",
              "device", "mountpoint")
    out = {}
    for s in p.q('ALERTS{alertstate="firing"%s}' % scope(), at):
        m = s["metric"]
        sig = tuple(sorted((k, v) for k, v in m.items() if k not in SKIP))
        target = next((m[t] for t in TARGET if m.get(t)), "-")
        out[sig] = (m.get("alertname", "-"), m.get("severity", "-"), m.get("namespace", "-"), target)
    return out


def section_alerts(p, top):
    h("1. 告警(巡检入口)")
    now = alert_instances(p, "now")
    prev = alert_instances(p, "now-24h")
    if not now and not prev:
        print("  无 ALERTS 数据 —— 未配置告警规则,或 ALERTS 序列未被采集")
        return
    print("  firing: 当前 %d 条 | 24h 前 %d 条 | 净变化 %+d" % (len(now), len(prev), len(now) - len(prev)))

    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    by_rule = collections.Counter((v[0], v[1]) for v in now.values())
    prev_rule = collections.Counter((v[0], v[1]) for v in prev.values())

    # alert fatigue: one rule dominating drowns the rest
    if by_rule:
        (top_rule, top_sev), top_n = by_rule.most_common(1)[0]
        share = 100.0 * top_n / len(now)
        sev_hi = sum(n for (r, s), n in by_rule.items() if s in ("critical", "error"))
        if share > 50 and len(by_rule) > 1 and sev_hi:
            print("  !! 告警疲劳: %s 占 %.0f%% (%d/%d),淹没了 %d 条 critical/error"
                  % (top_rule, share, top_n, len(now), sev_hi))

    print("\n  按规则(括号为 24h 前):")
    for (name, sev), n in sorted(by_rule.items(), key=lambda x: (sev_rank.get(x[0][1], 9), -x[1])):
        was = prev_rule.get((name, sev), 0)
        mark = "  <== 新增" if was == 0 else ("  <== +%d" % (n - was) if n > was else "")
        print("    %-9s %-44s %4d (%d)%s" % (sev or "-", str(name)[:44], n, was, mark))

    new = [now[s] for s in now if s not in prev]
    gone = [prev[s] for s in prev if s not in now]
    for label, items in (("24h 内新增的告警实例", new), ("24h 内已恢复的告警实例", gone)):
        print("\n  %s: %d 条" % (label, len(items)))
        items.sort(key=lambda v: sev_rank.get(v[1], 9))
        for name, sev, ns, target in items[:top]:
            print("      %-9s %-34s %s/%s" % (sev, str(name)[:34], ns, str(target)[:30]))
        if len(items) > top:
            print("      ... 另 %d 条" % (len(items) - top))

    ns = collections.Counter(v[2] for v in now.values() if v[2] != "-")
    if ns:
        print("\n  按 namespace: " + ", ".join("%s=%d" % (a, b) for a, b in ns.most_common(10)))


# ---------------------------------------------------------------- service diff
def section_service_diff(p, top):
    h("2. 服务日环比(仅列出超阈值项)")
    W = "24h"
    D = 'destination_workload!="waypoint"' + scope("destination_service_namespace")
    q_qps = 'sum(rate(istio_requests_total{%s}[%s])) by (destination_service_namespace, destination_service_name)' % (D, W)
    q_e5 = 'sum(rate(istio_requests_total{%s,response_code=~"5.."}[%s])) by (destination_service_namespace, destination_service_name)' % (D, W)
    q_p99 = ('histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s}[%s])) '
             'by (destination_service_namespace, destination_service_name, le))/1000' % (D, W))
    k = ("destination_service_namespace", "destination_service_name")
    qn, qo = p.by(q_qps, k, "now"), p.by(q_qps, k, "now-24h")
    en = p.by(q_e5, k, "now")
    if not qn:
        print("  无 istio_requests_total 数据")
        return

    # P99 across all ~344 services times out; restrict to services with real traffic.
    sig = sorted({key[1] for key in set(qn) | set(qo)
                  if max(qn.get(key, 0), qo.get(key, 0)) >= 0.5 and key[1]}, )[:150]
    pn = po = {}
    if sig:
        q_p99 = ('histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s,'
                 'destination_service_name=~"%s"}[%s])) by (destination_service_namespace, '
                 'destination_service_name, le))/1000' % (D, "|".join(sig), W))
        pn, po = p.by(q_p99, k, "now"), p.by(q_p99, k, "now-24h")
    if not pn:
        _warn.append("P99 数据缺失(查询超时或无 histogram),延迟列显示为 n/a")

    rows = []
    for key in set(qn) | set(qo):
        svc = "%s/%s" % (key[0], key[1])
        a, b = qn.get(key, 0), qo.get(key, 0)
        if max(a, b) < 0.5:      # ignore noise-level services
            continue
        dq = pct(a, b)
        err = 100 * en.get(key, 0) / a if a else 0
        p9, p9o = pn.get(key), po.get(key)
        dp = pct(p9, p9o) if (p9 is not None and p9o is not None) else 0.0
        flags = []
        if abs(dq) >= TH["qps_pct"]:
            flags.append("QPS %s" % arrow(dq))
        if err >= TH["err_pct"]:
            flags.append("5xx %.2f%%" % err)
        if p9 is not None and p9 >= TH["p99_abs"]:
            flags.append("P99 %.2fs" % p9)
        if dp != float("inf") and dp >= TH["p99_pct"] and (p9 or 0) > 0.05:
            flags.append("P99 %s" % arrow(dp))
        if flags:
            rows.append((svc, a, p9, flags, max(abs(dq), dp if dp != float("inf") else 0)))
    if not rows:
        print("  无异常 —— 所有服务 QPS/错误率/P99 均在阈值内")
    else:
        rows.sort(key=lambda x: -x[1])
        print("  %-42s %9s %8s  %s" % ("service", "qps", "P99", "变化"))
        for svc, a, p9, flags, _ in rows[:top]:
            print("  %-42s %9.1f %8s  %s"
                  % (svc[:42], a, ("%.2fs" % p9) if p9 is not None else "n/a", "; ".join(flags)))
        if len(rows) > top:
            print("  ... 另 %d 项" % (len(rows) - top))

    # resource + stability diff by namespace
    print("\n  资源与稳定性:")
    q_cpu = ('sum(rate(container_cpu_usage_seconds_total{container!="",container!="POD"%s}[%s])) by (namespace)'
             % (scope(), W))
    cn = p.by(q_cpu, ("namespace",), "now")
    co = p.by(q_cpu, ("namespace",), "now-24h")
    out = []
    for key in cn:
        a, b = cn[key], co.get(key, 0)
        if max(a, b) < 1.0:
            continue
        d = pct(a, b)
        if d != float("inf") and abs(d) >= TH["res_pct"]:
            out.append("%s CPU %.1f->%.1f 核 %s" % (key[0], b, a, arrow(d)))
    if not out:
        print("    CPU: 无 namespace 变化超过 %.0f%%" % TH["res_pct"])
    for txt in out[:top]:
        print("    " + txt)

    # counters: node-scoped oom series carry no namespace and would swamp the sum;
    # max_over_time-min_over_time avoids increase()'s extrapolation.
    F = 'namespace!="",container!="",container!="POD"' + scope()
    for label, metric in (("容器重启", "kube_pod_container_status_restarts_total"),
                          ("OOM 事件", "container_oom_events_total")):
        expr = ('topk(%d, sum(max_over_time(%s{%s}[%s]) - min_over_time(%s{%s}[%s])) by (namespace,pod) > 0.9)'
                % (top, metric, F, W, metric, F, W))
        rows = p.by(expr, ("namespace", "pod"), "now")
        total = p.num('sum(max_over_time(%s{%s}[%s]) - min_over_time(%s{%s}[%s]))'
                      % (metric, F, W, metric, F, W))
        if not rows:
            print("    %s: 无" % label)
            continue
        agg = collections.defaultdict(lambda: [0.0, 0, 0.0])   # sum, pods, worst
        for (ns, pod), v in rows.items():
            a = agg["%s/%s" % (ns, workload_of(pod))]
            a[0] += v
            a[1] += 1
            a[2] = max(a[2], v)
        print("    %s: 24h 共 %.0f 次(下列为 top%d workload):" % (label, total, top))
        for wl, (s, n, worst) in sorted(agg.items(), key=lambda x: -x[1][0])[:top]:
            hot = "   <== 达 CrashLoopBackOff 退避上限(约288/日)" if label == "容器重启" and worst > 240 else ""
            print("      %-48s %6.0f 次 / %d 个 pod%s" % (wl[:48], s, n, hot))


# ---------------------------------------------------------------- endpoints
def section_endpoints(p, top):
    h("3. 接口级(http_server_requests,Istio 拿不到的维度)")
    W = "24h"
    k = ("namespace", "uri", "method")
    # rate(_sum) is seconds of request time per second == average in-flight concurrency
    thr = TH["conc"] / 10 if NS else TH["conc"]   # a single namespace has smaller absolute numbers
    conc_n = p.by('sum(rate(%s[%s])) by (namespace,uri,method) > %s'
                  % (sel("http_server_requests_seconds_sum"), W, thr), k, "now")
    if not conc_n:
        print("  无 http_server_requests 数据(或无接口超过并发阈值)")
        return
    cnt_n = p.by('sum(rate(%s[%s])) by (namespace,uri,method)'
                 % (sel("http_server_requests_seconds_count"), W), k, "now")
    conc_o = p.by('sum(rate(%s[%s])) by (namespace,uri,method)'
                  % (sel("http_server_requests_seconds_sum"), W), k, "now-24h")

    print("  并发在途 = rate(_sum),即「每秒消耗的请求秒数」,等于平均并发请求数(Little 定律)。")
    print("  按它排序而非按最慢接口 —— 慢且低频的接口不值得先修。\n")
    rows, stream = [], []
    for key, c in conc_n.items():
        n = cnt_n.get(key, 0)
        avg = c / n if n else 0
        rec = (key, c, n, avg, pct(c, conc_o.get(key, 0)))
        # SSE / long-poll are long by design; treating them as slow is a false positive
        (stream if avg >= STREAM_SEC else rows).append(rec)

    print("  请求型接口 (top%d 按并发):" % top)
    print("  %-56s %9s %8s %8s %7s" % ("namespace / uri", "并发", "qps", "均耗时", "环比"))
    for key, c, n, avg, d in sorted(rows, key=lambda x: -x[1])[:top]:
        lbl = "%s %s" % (key[0], key[1])
        flag = ""
        if avg >= TH["lat_abs"]:
            flag += "  <== 均耗时偏高"
        if d != float("inf") and d >= TH["p99_pct"]:
            flag += "  <== 环比恶化"
        print("  %-56s %9.1f %8.1f %7.3fs %7s%s"
              % (lbl[:56], c, n, avg, arrow(d) if d != float("inf") else "新增", flag))

    if stream:
        print("\n  长连接型接口 (均耗时 >%ds,SSE/长轮询,长耗时属正常,仅供参考):" % STREAM_SEC)
        for key, c, n, avg, d in sorted(stream, key=lambda x: -x[1])[:6]:
            print("  %-56s %9.1f %8.1f %6.0fs %7s"
                  % (("%s %s" % (key[0], key[1]))[:56], c, n, avg,
                     arrow(d) if d != float("inf") else "新增"))

    err = p.by('sum(rate(http_server_requests_seconds_count{outcome=~"SERVER_ERROR|CLIENT_ERROR"%s}[%s])) '
               'by (namespace,uri,status,outcome) > 0.01' % (scope(), W),
               ("namespace", "uri", "status", "outcome"), "now")
    print("\n  非 2xx 接口 (>0.01/s,5xx 优先):")
    if not err:
        print("    无")
    # server errors first: 401/403 are usually expired tokens, 5xx are real defects
    for key, v in sorted(err.items(), key=lambda x: (0 if x[0][3] == "SERVER_ERROR" else 1, -x[1]))[:top]:
        print("    %-50s status=%-4s %-13s %.3f/s" % (("%s %s" % (key[0], key[1]))[:50], key[2], key[3], v))


# ---------------------------------------------------------------- blind spots
BLIND = [
    ("消息队列积压", ["rocketmq_consumer_lag", "rocketmq_group_diff", "kafka_consumergroup_lag",
                 "kafka_consumer_lag", "rabbitmq_queue_messages"],
     "mqconsumer 类工作负载的积压完全不可见,是最大盲区"),
    ("MySQL", ["mysql_global_status_threads_connected", "mysql_up"], "连接数/慢查询/复制延迟不可见"),
    ("Redis", ["redis_connected_clients", "redis_up"], "命中率/内存/连接不可见"),
    ("MongoDB", ["mongodb_up", "mongodb_connections_current"], "慢查询只能走阿里云控制台 API"),
    ("黑盒探测", ["probe_success", "probe_duration_seconds"], "外部可用性不可见"),
    ("证书到期", ["certmanager_certificate_expiration_timestamp_seconds", "probe_ssl_earliest_cert_expiry",
              "x509_cert_not_after"], "证书过期无法提前预警"),
    ("K8s 事件", ["kube_event_count"], "FailedScheduling/ImagePullBackOff/Evicted 只能看到结果不见原因"),
    ("发布事件", ["argocd_app_info", "rollout_info"], "无法关联「发布后指标变化」,只能靠 ReplicaSet hash 反推"),
    ("配额护栏", ["kube_resourcequota", "kube_limitrange"], "无 quota/limitrange,request 超配无结构性约束"),
    ("业务指标", ["order_count_total", "biz_order_total", "payment_success_total"],
     "无法判断技术异常的业务影响"),
]


def section_blind(p):
    h("4. 监控盲区(缺失即无法判断,不等于健康)")
    missing = []
    for name, metrics, impact in BLIND:
        if all(p.num("count(%s)" % m) == 0 for m in metrics):
            missing.append((name, impact))
    if not missing:
        print("  无盲区")
    for name, impact in missing:
        print("  [缺失] %-14s %s" % (name, impact))

    print("\n  埋点覆盖缺口:")
    http_ns = {k[0] for k in p.by('count(http_server_requests_seconds_count) by (namespace)', ("namespace",))}
    jvm_ns = {k[0] for k in p.by('count(jvm_memory_used_bytes) by (namespace)', ("namespace",))}
    busy = p.by('topk(25, sum(rate(istio_requests_total{destination_workload!="waypoint"}[24h])) '
                'by (destination_service_namespace))', ("destination_service_namespace",))
    gaps = [(k[0], v) for k, v in busy.items() if k[0] and k[0] not in http_ns]
    if not gaps:
        print("    高流量 namespace 均已接入接口级指标")
    for ns, v in sorted(gaps, key=lambda x: -x[1])[:10]:
        tag = "无 http_server_requests"
        if ns not in jvm_ns:
            tag += " + 无 JVM 指标"
        print("    %-24s %8.1f qps   %s" % (ns, v, tag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grafana", default=DEFAULT_GRAFANA)
    ap.add_argument("--datasource", default=DEFAULT_DS)
    ap.add_argument("--token")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--namespace", default="", help="scope the whole report to one namespace")
    ap.add_argument("--section", default="all",
                    choices=["all", "alerts", "diff", "endpoints", "blind"])
    a = ap.parse_args()
    global NS
    NS = a.namespace
    p = Prom(a.grafana, a.datasource, a.token, a.timeout)

    now = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=TZ)
    print("每日巡检报告  生成时刻 %s CST" % now.strftime("%Y-%m-%d %H:%M"))
    print("对比窗口: 最近 24h  vs  前一个 24h (窗口等长,已消除昼夜波动)")
    print("范围: %s" % ("namespace = %s" % NS if NS else "全集群"))

    if a.section in ("all", "alerts"):
        section_alerts(p, a.top)
    if a.section in ("all", "diff"):
        section_service_diff(p, a.top)
    if a.section in ("all", "endpoints"):
        section_endpoints(p, a.top)
    if a.section in ("all", "blind"):
        section_blind(p)

    if _warn:
        print("\n## 数据质量提示")
        for w in dict.fromkeys(_warn):
            print("  - " + w)
    print("\n(报告结束)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
