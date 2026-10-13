#!/usr/bin/env python3
"""Pull a full monitoring report for one Kubernetes namespace from Grafana's
Prometheus datasource proxy. Output is plain text sized for an LLM context."""

import argparse
import collections
import datetime
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_GRAFANA = "https://grafana.observability.ali-prod.xlbsoft.com"
DEFAULT_DS = "prometheus"
TZ_OFFSET = 8 * 3600  # CST

_warnings = []


class Prom:
    def __init__(self, grafana, ds, token, timeout):
        self.base = "%s/api/datasources/proxy/uid/%s/api/v1" % (grafana.rstrip("/"), ds)
        self.token = token
        self.timeout = timeout

    def _call(self, path, params):
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            raise SystemExit(
                "ERROR: Grafana returned HTTP %s for %s\n  %s\n"
                "  Hint: pass --token with a Grafana service-account token, "
                "or check --grafana / --datasource." % (e.code, path, detail))
        except Exception as e:
            raise SystemExit("ERROR: cannot reach Grafana: %s" % e)
        if body.get("status") != "success":
            _warnings.append("query failed: %s" % str(body)[:160])
            return []
        return body["data"]["result"]

    def instant(self, expr, at):
        return self._call("/query", {"query": expr, "time": at})

    def range(self, expr, start, end, step):
        return self._call("/query_range", {"query": expr, "start": start, "end": end, "step": step})


def val_map(res, key):
    """Series -> {label_value: float}. Silently skips series missing the label."""
    out = {}
    for r in res:
        k = r["metric"].get(key)
        if k is None:
            continue
        try:
            out[k] = out.get(k, 0.0) + float(r["value"][1])
        except (ValueError, TypeError):
            pass
    return out


def scalar(res, default=0.0):
    if not res:
        return default
    try:
        return float(res[0]["value"][1])
    except (ValueError, TypeError, KeyError):
        return default


HASH_SUFFIX = re.compile(r"-[0-9a-z]{5,10}$")


def workload_of(owner_kind, owner_name):
    """ReplicaSet/Job pods carry a generated suffix; strip it to get the workload."""
    if owner_kind in ("ReplicaSet", "Job") and owner_name:
        return HASH_SUFFIX.sub("", owner_name)
    return owner_name or "-"


def pod_workload_index(prom, ns, at):
    idx = {}
    for r in prom.instant('kube_pod_owner{namespace="%s"}' % ns, at):
        m = r["metric"]
        pod = m.get("pod")
        if pod:
            idx[pod] = workload_of(m.get("owner_kind"), m.get("owner_name"))
    if not idx:
        _warnings.append("kube_pod_owner returned nothing; workload grouping unavailable")
    return idx


def by_workload(pod_values, idx):
    out = collections.defaultdict(float)
    for pod, v in pod_values.items():
        out[idx.get(pod, "(unknown)")] += v
    return dict(out)


def parse_time(s):
    """Accept ISO8601, epoch seconds, epoch millis, or now-<N><unit>."""
    s = str(s).strip()
    if s in ("now", ""):
        return None
    m = re.fullmatch(r"now-(\d+)([smhd])", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return -int(m.group(1)) * mult  # negative = relative to end
    if re.fullmatch(r"\d{13}", s):
        return int(s) // 1000
    if re.fullmatch(r"\d{10}", s):
        return int(s)
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        raise SystemExit("ERROR: cannot parse time %r" % s)


def from_dashboard_url(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    get = lambda k: q.get(k, [None])[0]
    return get("var-namespace"), get("from"), get("to")


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts + TZ_OFFSET, datetime.UTC).strftime("%m-%d %H:%M")


GIB = 1024 ** 3
MIB = 1024 ** 2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("namespace", nargs="?", help="Kubernetes namespace")
    p.add_argument("--url", help="Grafana dashboard URL; namespace/from/to are read from it")
    p.add_argument("--from", dest="start", default="now-24h")
    p.add_argument("--to", dest="end", default="now")
    p.add_argument("--grafana", default=DEFAULT_GRAFANA)
    p.add_argument("--datasource", default=DEFAULT_DS)
    p.add_argument("--token", default=None, help="Grafana bearer token if anonymous access is off")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--rate-window", default="5m",
                   help="rate() window for CPU/throttling series; short keeps peaks visible")
    a = p.parse_args()

    ns, url_from, url_to = (None, None, None)
    if a.url:
        ns, url_from, url_to = from_dashboard_url(a.url)
    ns = a.namespace or ns
    if not ns:
        raise SystemExit("ERROR: namespace required (positional arg or --url with var-namespace)")

    end = parse_time(url_to or a.end)
    if end is None or end < 0:
        end = int(datetime.datetime.now(datetime.UTC).timestamp()) + (end or 0)
    start = parse_time(url_from or a.start)
    if start is None:
        start = end - 86400
    elif start < 0:
        start = end + start
    if start >= end:
        raise SystemExit("ERROR: --from must be earlier than --to")

    span = end - start
    rate_win = a.rate_window
    step = max(300, span // 288)
    prom = Prom(a.grafana, a.datasource, a.token, a.timeout)
    at = str(end)

    print("=" * 78)
    print("Namespace 监控报告: %s" % ns)
    print("窗口: %s ~ %s CST  (%.1fh)  rate窗口=%s  step=%ds"
          % (fmt_ts(start), fmt_ts(end), span / 3600.0, rate_win, step))
    print("=" * 78)

    idx = pod_workload_index(prom, ns, at)

    if not scalar(prom.instant('count(kube_pod_info{namespace="%s"})' % ns, at)):
        names = sorted({r["metric"].get("namespace") for r in
                        prom.instant('count(kube_pod_info) by (namespace)', at)} - {None})
        print("\nnamespace %r 在该时刻没有任何 Pod —— 名字写错了,或该窗口内它还不存在。" % ns)
        hint = [n for n in names if ns.lower() in n.lower() or n.lower() in ns.lower()]
        if hint:
            print("相近的 namespace: %s" % ", ".join(hint))
        print("\n集群现有 namespace (%d 个):\n  %s" % (len(names), "\n  ".join(
            ", ".join(names[i:i + 6]) for i in range(0, len(names), 6))))
        return

    # ---------- 1. overview ----------
    print("\n## 1. 规模与资源总览")
    cpu_use_now = scalar(prom.instant(
        'sum(rate(container_cpu_usage_seconds_total{namespace="%s", image!=""}[%s]))' % (ns, rate_win), at))
    cpu_req = scalar(prom.instant(
        'sum(kube_pod_container_resource_requests{namespace="%s", resource="cpu"})' % ns, at))
    cpu_lim = scalar(prom.instant(
        'sum(kube_pod_container_resource_limits{namespace="%s", resource="cpu"})' % ns, at))
    mem_use_now = scalar(prom.instant(
        'sum(container_memory_working_set_bytes{namespace="%s", image!=""})' % ns, at))
    mem_req = scalar(prom.instant(
        'sum(kube_pod_container_resource_requests{namespace="%s", resource="memory"})' % ns, at))
    mem_lim = scalar(prom.instant(
        'sum(kube_pod_container_resource_limits{namespace="%s", resource="memory"})' % ns, at))

    cpu_series = prom.range(
        'sum(rate(container_cpu_usage_seconds_total{namespace="%s", image!=""}[%s]))' % (ns, rate_win),
        start, end, step)
    mem_series = prom.range(
        'sum(container_memory_working_set_bytes{namespace="%s", image!=""})' % ns, start, end, step)

    def stats(res, scale=1.0):
        if not res:
            return None
        vs = [float(v[1]) / scale for v in res[0]["values"] if v[1] != "NaN"]
        return (min(vs), sum(vs) / len(vs), max(vs)) if vs else None

    cs, ms = stats(cpu_series), stats(mem_series, 1.0)
    print("  Pod: %d 个  Service: %d 个  PVC: %d 个  集群占比: %.2f%%" % (
        scalar(prom.instant('count(kube_pod_info{namespace="%s"})' % ns, at)),
        scalar(prom.instant('sum(kube_service_info{namespace="%s"})' % ns, at)),
        scalar(prom.instant('sum(kube_persistentvolumeclaim_info{namespace="%s"})' % ns, at)),
        100.0 * scalar(prom.instant('count(kube_pod_info{namespace="%s"})' % ns, at))
        / max(1.0, scalar(prom.instant('count(kube_pod_info)', at)))))
    if cs:
        print("  CPU  使用 min/avg/max = %.2f / %.2f / %.2f 核   requests=%.1f  limits=%.1f"
              % (cs[0], cs[1], cs[2], cpu_req, cpu_lim))
        print("       requests 利用率: avg %.1f%%  peak %.1f%%   limits 超卖 %.1fx"
              % (100 * cs[1] / cpu_req if cpu_req else 0,
                 100 * cs[2] / cpu_req if cpu_req else 0,
                 cpu_lim / cpu_req if cpu_req else 0))
    else:
        print("  CPU  使用(瞬时)=%.2f 核  requests=%.1f  limits=%.1f" % (cpu_use_now, cpu_req, cpu_lim))
    if ms:
        print("  内存 使用 min/avg/max = %.1f / %.1f / %.1f GiB  requests=%.1f  limits=%.1f"
              % (ms[0] / GIB, ms[1] / GIB, ms[2] / GIB, mem_req / GIB, mem_lim / GIB))
        print("       requests 利用率: avg %.1f%%  peak %.1f%%   limits 超卖 %.1fx"
              % (100 * ms[1] / mem_req if mem_req else 0,
                 100 * ms[2] / mem_req if mem_req else 0,
                 mem_lim / mem_req if mem_req else 0))
    else:
        print("  内存 使用(瞬时)=%.1f GiB requests=%.1f limits=%.1f"
              % (mem_use_now / GIB, mem_req / GIB, mem_lim / GIB))

    # ---------- 2. per-workload ----------
    print("\n## 2. 各 workload 资源账单  (use=窗口末瞬时, L/R=limit/request 倍数)")
    pods_cnt = collections.Counter(idx.values())
    cu = by_workload(val_map(prom.instant(
        'sum(rate(container_cpu_usage_seconds_total{namespace="%s", image!=""}[%s])) by (pod)'
        % (ns, rate_win), at), "pod"), idx)
    cr = by_workload(val_map(prom.instant(
        'sum(kube_pod_container_resource_requests{namespace="%s", resource="cpu"}) by (pod)' % ns, at), "pod"), idx)
    cl = by_workload(val_map(prom.instant(
        'sum(kube_pod_container_resource_limits{namespace="%s", resource="cpu"}) by (pod)' % ns, at), "pod"), idx)
    mu = by_workload(val_map(prom.instant(
        'sum(container_memory_working_set_bytes{namespace="%s", image!=""}) by (pod)' % ns, at), "pod"), idx)
    mr = by_workload(val_map(prom.instant(
        'sum(kube_pod_container_resource_requests{namespace="%s", resource="memory"}) by (pod)' % ns, at), "pod"), idx)
    ml = by_workload(val_map(prom.instant(
        'sum(kube_pod_container_resource_limits{namespace="%s", resource="memory"}) by (pod)' % ns, at), "pod"), idx)

    hdr = ("  %-40s %4s | %7s %7s %7s %6s | %8s %8s %8s %6s"
           % ("workload", "pods", "cpuUse", "cpuReq", "cpuLim", "use%", "memUse", "memReq", "memLim", "use%"))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for w in sorted(pods_cnt, key=lambda x: -cr.get(x, 0)):
        n = pods_cnt[w]
        u, r, l = cu.get(w, 0), cr.get(w, 0), cl.get(w, 0)
        U, R, L = mu.get(w, 0) / GIB, mr.get(w, 0) / GIB, ml.get(w, 0) / GIB
        print("  %-40s %4d | %7.2f %7.1f %7.1f %5.1f%% | %7.1fG %7.1fG %7.1fG %5.1f%%"
              % (w[:40], n, u, r, l, 100 * u / r if r else 0, U, R, L, 100 * U / R if R else 0))

    print("\n  单 Pod 规格 (request -> limit):")
    print("  %-40s %4s | %8s %8s %5s | %9s %9s %5s"
          % ("workload", "pods", "cpuReq", "cpuLim", "L/R", "memReq", "memLim", "L/R"))
    for w in sorted(pods_cnt, key=lambda x: -pods_cnt[x]):
        n = pods_cnt[w] or 1
        a1, b1 = cr.get(w, 0) / n, cl.get(w, 0) / n
        a2, b2 = mr.get(w, 0) / n / MIB, ml.get(w, 0) / n / MIB
        print("  %-40s %4d | %8.2f %8.2f %4.0fx | %8.0fMi %8.0fMi %4.0fx"
              % (w[:40], n, a1, b1, b1 / a1 if a1 else 0, a2, b2, b2 / a2 if a2 else 0))

    # ---------- 3. health ----------
    print("\n## 3. 健康与异常  (窗口内累计)")
    for title, expr, key in [
        ("Pod phase", 'sum(kube_pod_status_phase{namespace="%s"}) by (phase)' % ns, "phase"),
        ("QoS class", 'sum(kube_pod_status_qos_class{namespace="%s"}) by (qos_class)' % ns, "qos_class"),
        ("status reason", 'sum(kube_pod_status_reason{namespace="%s"}) by (reason) > 0' % ns, "reason"),
        ("Waiting reason", 'sum(kube_pod_container_status_waiting_reason{namespace="%s"} > 0) by (reason)' % ns, "reason"),
        ("Terminated reason", 'sum(kube_pod_container_status_terminated_reason{namespace="%s"} > 0) by (reason)' % ns, "reason"),
    ]:
        d = val_map(prom.instant(expr, at), key)
        d = {k: v for k, v in d.items() if v}
        print("  %-18s %s" % (title + ":", ", ".join("%s=%d" % (k, v) for k, v in
                                                     sorted(d.items(), key=lambda x: -x[1])) or "(无)"))

    win = "%ds" % span
    for title, expr, key in [
        ("容器重启", 'sum(increase(kube_pod_container_status_restarts_total{namespace="%s"}[%s])) by (pod) > 0.9' % (ns, win), "pod"),
        ("OOM 事件", 'sum(increase(container_oom_events_total{namespace="%s"}[%s])) by (pod) > 0.9' % (ns, win), "pod"),
        ("最近 OOMKilled", 'sum(kube_pod_container_status_last_terminated_reason{namespace="%s", reason="OOMKilled"}) by (pod) > 0' % ns, "pod"),
    ]:
        d = val_map(prom.instant(expr, at), key)
        if d:
            print("  %s (%d 个 pod):" % (title, len(d)))
            for k, v in sorted(d.items(), key=lambda x: -x[1])[:a.top]:
                print("      %-56s %.0f" % (k[:56], v))
        else:
            print("  %-18s 无" % (title + ":"))

    thr = val_map(prom.instant(
        '100 * sum(rate(container_cpu_cfs_throttled_periods_total{namespace="%s", image!=""}[%s])) by (pod)'
        ' / sum(rate(container_cpu_cfs_periods_total{namespace="%s", image!=""}[%s])) by (pod) > 0.5'
        % (ns, rate_win, ns, rate_win), at), "pod")
    if thr:
        print("  CPU 限流 >0.5%% 的 pod (%d 个, top%d):" % (len(thr), a.top))
        for k, v in sorted(thr.items(), key=lambda x: -x[1])[:a.top]:
            print("      %-56s %.2f%%" % (k[:56], v))
    else:
        print("  CPU 限流:          无 pod 超过 0.5%")

    # ---------- 4. HPA ----------
    print("\n## 4. HPA 弹性")
    mn = val_map(prom.instant('kube_horizontalpodautoscaler_spec_min_replicas{namespace="%s"}' % ns, at), "horizontalpodautoscaler")
    mx = val_map(prom.instant('kube_horizontalpodautoscaler_spec_max_replicas{namespace="%s"}' % ns, at), "horizontalpodautoscaler")
    cur = val_map(prom.instant('kube_horizontalpodautoscaler_status_current_replicas{namespace="%s"}' % ns, at), "horizontalpodautoscaler")
    if not mx:
        print("  该 namespace 无 HPA")
    else:
        limited = scalar(prom.instant(
            'sum(kube_horizontalpodautoscaler_status_condition{namespace="%s", condition="ScalingLimited", status="true"})' % ns, at))
        fixed = [k for k in mx if mn.get(k) == mx.get(k)]
        at_min = [k for k in mx if cur.get(k) == mn.get(k) and mn.get(k) != mx.get(k)]
        print("  HPA 总数=%d  ScalingLimited=true: %d  min==max(等于固定副本): %d  贴在 min 未扩容: %d"
              % (len(mx), limited, len(fixed), len(at_min)))
        print("  %-42s %6s %6s %6s  %s" % ("hpa", "min", "cur", "max", "状态"))
        for k in sorted(mx, key=lambda x: -mx[x]):
            lo, c, hi = mn.get(k, 0), cur.get(k, 0), mx[k]
            tag = "固定" if lo == hi else ("贴min" if c == lo else ("贴max" if c == hi else ""))
            print("  %-42s %6.0f %6.0f %6.0f  %s" % (k[:42], lo, c, hi, tag))

    # ---------- 5. replica churn ----------
    print("\n## 5. 窗口内副本变化 (发布 / 扩缩容)")
    own = prom.range('count(kube_pod_owner{namespace="%s"}) by (owner_name)' % ns, start, end, step)
    churn = collections.defaultdict(dict)
    for r in own:
        w = workload_of("ReplicaSet", r["metric"].get("owner_name", ""))
        for ts, v in r["values"]:
            churn[w][int(ts)] = churn[w].get(int(ts), 0) + float(v)
    changed = []
    for w, series in churn.items():
        pts = [series[t] for t in sorted(series)]
        if pts and max(pts) != min(pts):
            changed.append((w, min(pts), max(pts), pts[0], pts[-1], sorted(series)))
    if not changed:
        print("  全窗口所有 workload 副本数无变化")
    for w, lo, hi, first, last, ks in sorted(changed, key=lambda x: -(x[2] - x[1]))[:15]:
        print("  %-40s min=%.0f max=%.0f 起=%.0f 止=%.0f" % (w[:40], lo, hi, first, last))
        prev, marks = None, []
        for t in ks:
            v = churn[w][t]
            if prev is not None and v != prev:
                marks.append("%s->%.0f" % (fmt_ts(t), v))
            prev = v
        if marks:
            print("      " + "  ".join(marks[:12]) + (" ..." if len(marks) > 12 else ""))

    # ---------- 6. traffic ----------
    print("\n## 6. Istio 流量 (窗口平均 QPS)")
    dst = 'destination_service_namespace="%s", destination_workload!="waypoint"' % ns
    inb = prom.instant('sum(rate(istio_requests_total{%s}[%s])) by (destination_service_name, response_code) > 0'
                       % (dst, win), at)
    if not inb:
        print("  无 istio_requests_total 数据 (未接入网格或无流量)")
    else:
        rep = val_map(prom.instant(
            'sum(rate(istio_requests_total{%s}[%s])) by (reporter)' % (dst, win), at), "reporter")
        if len(rep) > 1:
            print("  !! reporter 混用: %s"
                  % ", ".join("%s=%.2f qps" % (k, v) for k, v in sorted(rep.items(), key=lambda x: -x[1])))
            print("     同一请求可能被重复计数,比较绝对值前请按单一 reporter 过滤")
        tot = collections.defaultdict(float)
        code = collections.defaultdict(lambda: collections.defaultdict(float))
        for r in inb:
            s = r["metric"].get("destination_service_name", "-")
            c = r["metric"].get("response_code", "-")
            v = float(r["value"][1])
            tot[s] += v
            code[s][c] += v
        print("  %-34s %9s %8s %8s   状态码分布" % ("service", "qps", "5xx%", "4xx%"))
        for s in sorted(tot, key=lambda x: -tot[x]):
            t = tot[s] or 1e-12
            e5 = sum(v for c, v in code[s].items() if c.startswith("5"))
            e4 = sum(v for c, v in code[s].items() if c.startswith("4"))
            dist = " ".join("%s:%.2f" % (c, v) for c, v in sorted(code[s].items(), key=lambda x: -x[1])[:4])
            print("  %-34s %9.2f %7.2f%% %7.2f%%   %s" % (s[:34], tot[s], 100 * e5 / t, 100 * e4 / t, dist))

        p99 = val_map(prom.instant(
            'histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{%s}[%s])) by (destination_service, le)) / 1000 > 0'
            % (dst, win), at), "destination_service")
        print("\n  被调 P99 耗时 (秒):")
        for k, v in sorted(p99.items(), key=lambda x: -x[1]):
            flag = "  <== 偏高" if v > 1 else ""
            print("      %-52s %8.3f%s" % (k[:52], v, flag))

        print("\n  调用来源 top%d:" % a.top)
        src = [("%s.%s -> %s" % (r["metric"].get("source_app", "-"),
                                 r["metric"].get("source_workload_namespace", "-"),
                                 r["metric"].get("destination_service_name", "-")), float(r["value"][1]))
               for r in prom.instant(
                   'sum(rate(istio_requests_total{%s}[%s])) by (source_app, source_workload_namespace, destination_service_name) > 0'
                   % (dst, win), at)]
        for k, v in sorted(src, key=lambda x: -x[1])[:a.top]:
            print("      %-60s %8.2f" % (k[:60], v))

        print("\n  出向调用 top%d:" % a.top)
        out = [("-> %s.%s code=%s" % (r["metric"].get("destination_service_name", "-"),
                                      r["metric"].get("destination_service_namespace", "-"),
                                      r["metric"].get("response_code", "-")), float(r["value"][1]))
               for r in prom.instant(
                   'sum(rate(istio_requests_total{source_workload_namespace="%s", destination_workload!="waypoint"}[%s]))'
                   ' by (destination_service_name, destination_service_namespace, response_code) > 0' % (ns, win), at)]
        for k, v in sorted(out, key=lambda x: -x[1])[:a.top]:
            print("      %-60s %8.2f" % (k[:60], v))

    # ---------- 7. hourly curve ----------
    print("\n## 7. 入向 QPS 逐小时曲线 (CST)")
    res = prom.range('sum(rate(istio_requests_total{%s}[5m]))' % dst, start, end, max(300, step))
    if not res:
        print("  无数据")
    else:
        vals = [(int(v[0]), float(v[1])) for v in res[0]["values"] if v[1] != "NaN"]
        if vals:
            hi = max(vals, key=lambda x: x[1])
            lo = min(vals, key=lambda x: x[1])
            print("  峰值 %.0f qps @ %s      谷值 %.0f qps @ %s      峰谷比 %.0fx"
                  % (hi[1], fmt_ts(hi[0]), lo[1], fmt_ts(lo[0]), hi[1] / lo[1] if lo[1] > 0 else 0))
            buckets = collections.defaultdict(list)
            for ts, v in vals:
                buckets[datetime.datetime.fromtimestamp(ts + TZ_OFFSET, datetime.UTC).strftime("%m-%d %H")].append(v)
            peak = max((sum(x) / len(x)) for x in buckets.values()) or 1
            for h in sorted(buckets):
                avg = sum(buckets[h]) / len(buckets[h])
                print("    %s:00 %8.0f  %s" % (h, avg, "#" * int(60 * avg / peak)))

    # ---------- 8. storage ----------
    pvc = val_map(prom.instant(
        'sum(kubelet_volume_stats_used_bytes{namespace="%s"}) by (persistentvolumeclaim)'
        ' / sum(kubelet_volume_stats_capacity_bytes{namespace="%s"}) by (persistentvolumeclaim) * 100'
        % (ns, ns), at), "persistentvolumeclaim")
    print("\n## 8. 存储")
    if pvc:
        for k, v in sorted(pvc.items(), key=lambda x: -x[1]):
            print("  %-52s %6.1f%%" % (k[:52], v))
    else:
        print("  无 PVC 用量数据 (无 PVC 或 kubelet_volume_stats 未采集)")

    if _warnings:
        print("\n## 数据质量提示")
        for w in dict.fromkeys(_warnings):
            print("  - " + w)
    print("\n(报告结束)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
