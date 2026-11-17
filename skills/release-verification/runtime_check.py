#!/usr/bin/env python3
"""Deterministic post-release runtime check via Grafana Prometheus."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "_shared"))
from grafana_prom import GrafanaAuthError, GrafanaError, Prom, total, values  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))


def load_ctx(a):
    cmd = [sys.executable, os.path.join(HERE, "context.py")]
    if a.release_order:
        cmd += ["--release-order", a.release_order]
    else:
        cmd += ["--branch", a.branch]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(r.stderr or r.stdout)
    return json.loads(r.stdout)


def parse_ts(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            d = dt.datetime.strptime(value.replace("Z", "+0000"), fmt)
            return d.replace(tzinfo=CST) if d.tzinfo is None else d
        except Exception:
            continue
    return None


def deploy_time(ctx):
    times = []
    for w in (ctx.get("workflows") or {}).values():
        b = (w or {}).get("branch") or {}
        t = parse_ts(b.get("updated_at"))
        if t and b.get("current_node") in ("completed", "product_verification"):
            times.append(t)
    t = max(times) if times else parse_ts((ctx.get("order") or {}).get("submitted_at"))
    return t or dt.datetime.now(CST)


def pick_svc(p, ns, names, when):
    n = names[0]
    expr = 'count_over_time(istio_proxy:path_requests:count_15s{namespace="%s",svc="%s"}[5m])' % (ns, n)
    return n, total(p.query(expr, when=when)) > 0


def wstats(p, ns, svc, start, end):
    def rng(expr):
        return p.query_range(expr, start, end, 60)
    tot = total(rng('sum(istio_proxy:path_requests:count_15s{namespace="%s",svc="%s"})' % (ns, svc)))
    bad = total(rng('sum(istio_proxy:path_requests:count_15s{namespace="%s",svc="%s",response_code=~"5.."})' % (ns, svc)))
    p99v = [v for _, vals in values(rng('max(istio_proxy:path_request_duration_ms:p99_15s{namespace="%s",svc="%s"})' % (ns, svc))) for v in vals]
    rest = total(rng('sum(increase(kube_pod_container_status_restarts_total{namespace="%s",pod=~"%s-[^-]+-[^-]+"}[15m]))' % (ns, svc)))
    return {
        "req": round(tot),
        "qps": round(tot / 60 / 15, 2) if tot else 0,
        "bad": round(bad),
        "ratio": round(bad / tot * 100, 3) if tot else None,
        "p99": round(max(p99v)) if p99v else None,
        "restarts": round(rest, 1),
    }


def verdict(svc_rows):
    vals = [r for r in svc_rows.values() if r["req"] > 0]
    if not vals:
        return "UNKNOWN"
    base = svc_rows["baseline"]["p99"] or 0
    maxp = max((r["p99"] or 0) for r in vals)
    ratio = (maxp / base) if base else None
    max_bad = max((r["ratio"] or 0) for r in vals)
    rest = max(r["restarts"] for r in svc_rows.values())
    if max_bad > 5 or (ratio and ratio > 3 and maxp > 1000):
        return "FAIL"
    if max_bad > 1 or (ratio and ratio > 2 and maxp > 500) or rest > 0:
        return "WARN"
    return "PASS"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--release-order")
    g.add_argument("--branch")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    ctx = load_ctx(a)
    t0 = deploy_time(ctx)
    windows = {
        "baseline": (t0 - dt.timedelta(minutes=60), t0),
        "plus15": (t0, t0 + dt.timedelta(minutes=15)),
        "plus60": (t0, t0 + dt.timedelta(minutes=60)),
    }
    p = Prom()
    out = []
    for s in ctx.get("services") or []:
        ns = s.get("namespace")
        names = [s.get("target_service_name"), s.get("source_service_name")]
        names = [x for x in names if x]
        svc, exact = pick_svc(p, ns, names, int(t0.timestamp()))
        rows = {}
        for name, (start, end) in windows.items():
            rows[name] = wstats(p, ns, svc, int(start.timestamp()), int(end.timestamp()))
        out.append({"service": s.get("target_service_name"), "env": s.get("target_environment"),
                    "metric_svc": svc, "exact_label": exact, "windows": rows,
                    "verdict": verdict(rows)})

    known = [s for s in out if s["verdict"] != "UNKNOWN"]
    overall = "UNKNOWN"
    if known:
        overall = "PASS"
        for s in known:
            if s["verdict"] == "FAIL":
                overall = "FAIL"
            elif s["verdict"] == "WARN" and overall == "PASS":
                overall = "WARN"
    unknown = [s["service"] for s in out if s["verdict"] == "UNKNOWN"]
    result = {"order": ctx.get("order"), "deploy_time": t0.isoformat(), "verdict": overall, "services": out,
              "note": "t0 取 workflow completed 的 updated_at；若为空回退 submitted_at。"}
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("## 发布后运行态验证：%s" % (ctx.get("order") or {}).get("name"))
    print("")
    print("- 发布单：`%s`" % (ctx.get("order") or {}).get("id"))
    print("- t0：%s（workflow 完成时间）" % t0.strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("- 结论：**%s**%s" % (overall, ("（未覆盖：%s）" % ", ".join(unknown)) if unknown else ""))
    print("")
    print("| 服务 | 环境 | 指标 svc | 窗口 | QPS | 5xx% | P99 ms | 重启 |")
    print("|---|---|---|---|---|---|---|---|")
    for s in out:
        for w, r in s["windows"].items():
            print("| %s | %s | %s%s | %s | %s | %s | %s | %s |" % (
                s["service"], s["env"], s["metric_svc"], "" if s["exact_label"] else "*",
                w, r["qps"], "n/a" if r["ratio"] is None else r["ratio"],
                "n/a" if r["p99"] is None else r["p99"], r["restarts"]))
    print("")
    print("> " + result["note"])
    print("> * 目标 svc 在发布时点无指标，按 UNKNOWN 处理。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GrafanaAuthError, GrafanaError) as e:
        raise SystemExit("Grafana error: %s" % e)