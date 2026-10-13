---
name: k8s-namespace-monitor
description: Analyze Grafana/Prometheus monitoring for a Kubernetes namespace in the XLB Ali-prod cluster — resource waste, HPA elasticity, restarts/OOM, CPU throttling, Istio QPS/error-rate/P99, callers and hourly traffic curves. Use when asked to look at, analyze, diagnose or health-check the monitoring of a namespace (e.g. "分析下 goods 的监控", "看看 retail-bff namespace", "这个 namespace 有没有问题", "帮我看下资源有没有浪费"), or when given a Grafana namespaces-dashboard URL.
---

# K8s Namespace 监控分析

## Overview

Pull a full metrics report for one namespace and turn it into a prioritized
diagnosis. All data comes from Prometheus via Grafana's datasource proxy — no
kubectl or cluster access needed.

## Workflow

1. Run the report script. If the user pasted a Grafana dashboard URL, feed it in
   with `--url` so namespace and time window are taken verbatim from the URL.

   ```bash
   python3 .qoder/skills/k8s-namespace-monitor/scripts/ns_report.py <namespace>
   python3 .qoder/skills/k8s-namespace-monitor/scripts/ns_report.py --url "<grafana dashboard url>"
   python3 .qoder/skills/k8s-namespace-monitor/scripts/ns_report.py <namespace> --from now-7d
   ```

   Defaults: last 24h, Grafana `https://grafana.observability.ali-prod.xlbsoft.com`,
   datasource uid `prometheus`, timestamps rendered in CST (+8).
   Other flags: `--top N`, `--rate-window 5m`, `--timeout`, `--grafana`,
   `--datasource`, `--token` (only if anonymous read gets disabled).

   **Never pin an absolute date by default.** Leave the window alone so it
   trails the present, or use relative `now-<N><s|m|h|d>` forms. Absolute
   ISO8601 timestamps are only for a window the user supplied, a URL's `from`/`to`,
   or reproducing a specific past incident. Reusing a date from earlier in the
   conversation silently reports stale data. Always state the analysed window,
   which the report header prints.

2. Read the report and apply the diagnosis rules below.

3. Drill into anything flagged. Use `references/promql.md` for ad-hoc queries —
   per-pod resource breakdowns, container-level memory, pod age, node pressure,
   JVM/GC, latency by caller, and `response_flags` error causes. Neither
   endpoint-level nor per-app-pod latency attribution is available from these
   metrics; the reference explains why and hands off to traces and logs. Not
   every namespace exports JVM metrics — check before theorising about thread
   pools, and label an unconfirmed cause as a hypothesis. Do not guess a cause
   the metrics can confirm.

4. Report in chat: verdict first, then the 2–4 findings that actually matter,
   each with the number that proves it, then a priority-ordered recommendation
   list. Skip sections that are clean — say "无异常" and move on.

5. Only write a `docs/*.html` document if the user asks. `AGENTS.md` then
   applies: self-hosted assets, at least one architecture diagram, no secrets.

## Diagnosis Rules

Read every number against these thresholds rather than describing the report.

**Resource waste (usually the biggest finding)**
- CPU requests utilization < 20% avg → over-provisioned. Quantify the reclaimable
  cores per workload; name the worst offender explicitly.
- A workload whose `cpuUse%` is ~0% while holding tens of cores of request is a
  scheduling-capacity leak even though nothing is "broken".
- Very low per-pod memory (< 50 MiB) across many replicas → pods are idle;
  question whether the replica count is justified at all.

**Right-sizing risk**
- `memUse%` > 100% → request is undersized. The pod is only surviving on burst;
  it is a first-pick eviction target under node memory pressure.
- `L/R` (limit/request) > 5x → node overcommit. Report the extreme cases; a
  50Mi→8Gi (164x) spec means the scheduler has essentially no information.
- QoS all `Burstable` with 0 `Guaranteed` → no workload is protected from
  eviction. Worth stating once for the namespace.

**Elasticity**
- Compare the QPS peak/trough ratio (section 7) against replica churn
  (section 5). A high ratio with flat replicas means capacity is pinned at peak
  and paid for 24h.
- HPA `min == max` is a fixed replica count wearing an HPA costume.
- HPA sitting at `min` all window with a much higher `max` means `min` was set
  to the peak water line — recommend lowering `min`, not raising `max`.
- `ScalingLimited=true` count is the fastest signal that autoscaling is inert.

**Stability**
- Any restart, OOM event, or `OOMKilled` last-terminated reason → investigate the
  pod before commenting on anything else.
- `Waiting reason` of `CrashLoopBackOff` / `ImagePullBackOff`, or non-zero
  `Pending`/`Failed` phase → a real incident, lead with it.
- CPU throttle > 5% → raise the limit. 1–3% on a request-starved pod is worth a
  mention but is not urgent.
- Section 5 blips where replicas jump and return within ~5–10 min are rolling
  deployments, not autoscaling. Count them as releases.

**Traffic**
- 5xx > 0.1% on any service with real volume → investigate.
- P99 > 1s → flag. Cross-check the QPS: a 20s P99 at 0.03 qps is a slow cron-job
  path, not a user-facing outage. Always pair latency with volume before judging
  severity.
- Caller concentration: if one `source_app` is > 80% of inbound, the namespace's
  blast radius is that one upstream. Say so.
- Compare inbound vs outbound to identify the namespace's real dependencies.

## Environment Facts

- Namespaces dashboard: `/d/namespaces/f09f94a5-namespaces`, variables
  `var-namespace`, `var-pod` (prefix regex), `var-resolution`.
- Datasource uid `prometheus` (display name "Metrics") holds kube-state-metrics,
  cAdvisor and Istio series — this is the one to query. Other datasources:
  `efcea6h94peyoc` / `cfcbge3q8j1tsd` (Aliyun Prometheus), `victoriaLogs` and
  `cfs2rslrixwcgc` (logs), `victoria-traces` (Jaeger traces), `ffmeoq2l6l6v4d`
  (MySQL `sr_dqc`).
- The datasource proxy currently answers unauthenticated reads. If that changes,
  pass `--token` with a Grafana service-account token.
- The mesh runs Istio ambient, so always exclude `destination_workload!="waypoint"`
  or waypoint proxies double-count traffic.
- Workloads are grouped by stripping the generated suffix from
  `kube_pod_owner`'s `owner_name`. The cluster also has a
  `namespace_workload_pod:kube_pod_owner:relabel` recording rule, but it omits
  CronJob pods — prefer `kube_pod_owner`.
- Cluster-wide pod count is ~16k, useful for sizing a namespace's share.

## Resources

- `scripts/ns_report.py` — the report generator. Sections: 1 overview,
  2 per-workload resource bill + per-pod specs, 3 health/restarts/OOM/throttling,
  4 HPA, 5 replica churn, 6 Istio traffic/error rate/P99/callers, 7 hourly QPS
  curve, 8 storage. Degrades gracefully when a metric family is absent and prints
  a data-quality note.
- `references/promql.md` — curl recipes against the datasource proxy plus PromQL
  for drill-downs the report does not cover.
