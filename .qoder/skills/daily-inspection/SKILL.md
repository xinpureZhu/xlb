---
name: daily-inspection
description: Daily production monitoring inspection for the XLB Ali-prod Kubernetes cluster — starts from firing Prometheus ALERTS and their 24h diff, reports only day-over-day changes in service QPS/error-rate/P99, container restarts and OOM, drills into per-endpoint latency via http_server_requests, and explicitly lists monitoring blind spots. Use for daily or routine health checks, shift handover, morning inspection, or when asked 巡检 / 日报 / 每日检查 / 今天线上有没有问题 / 有什么变化 / 昨天到今天的对比. For deep-diving one namespace instead, use k8s-namespace-monitor.
---

# 每日巡检

## Overview

Produce a short, diff-based inspection report: what alerts changed, what
regressed since yesterday, which endpoints cost the most, and what cannot be
seen at all. Silence on a clean item is the goal, not completeness.

## Workflow

```bash
python3 .qoder/skills/daily-inspection/scripts/daily_inspect.py
python3 .qoder/skills/daily-inspection/scripts/daily_inspect.py --section alerts
```

Flags include `--namespace NS` to scope the whole report to one namespace.

Sections: `alerts` / `diff` / `endpoints` / `blind` / `all` (default).
Flags: `--top N`, `--timeout`, `--grafana`, `--datasource`, `--token`.
A full run takes 2–5 minutes; the endpoint queries alone cost ~15s each.

Then write the report in this order, and **lead with what changed**:

1. **告警变化** — new and resolved instances matter more than the standing total.
   A rule whose count is flat is background noise; a newly firing `critical` is
   the headline.
2. **需要今天处理的** — crash loops at the backoff ceiling, OOM loops, new 5xx,
   latency regressions.
3. **趋势与观察项** — things drifting but not urgent.
4. **盲区** — restate that these are unknown, not healthy.

Skip anything clean with a single "无异常". Do not restate normal metrics.
A daily report longer than roughly one screen has failed at its job.

**精简判据（`ns_lark_report.py` 已按此收敛）** —— 正常态不占版面：
- 待处理列表**就是**报告本体；其余只留几格上下文指标，不要按资源逐段罗列。
- K8s 事件**排除 `TooManyMissedTimes` 与 `Unhealthy`** —— 二者动辄上千条，会把
  真正要处理的 reason 淹没。只留 OOM / 驱逐 / 启动失败 / 调度失败 / HPA 失效 /
  磁盘回收失败这类硬故障。
- MongoDB、Redis **只看 CPU 与内存 24h 峰值**，超阈值（CPU 70% / 内存 80%）才进
  待处理。顺带砍掉了慢日志计数与参数查询 —— 整体耗时从 3–5 分钟降到 ~30 秒。
- MQ **只报积压峰值与当前最高值，并带上 group / topic**。用
  `MessageAccumulationOnetopicV3`（instanceId 单维度即返回全部 group×topic）。
  只有**中位 > 50（持续积压）**才进待处理，峰值毛刺不进。
- 服务日环比：纯 QPS 变化要求 ≥50 qps 才报；5xx / P99 类不设量级门槛。

## 单 namespace + 云资源 → 飞书卡片

For a per-namespace daily report that also covers the cloud resources that
namespace owns, and delivers it to Lark:

```bash
# 预览(不发送)
python3 .qoder/skills/daily-inspection/scripts/ns_lark_report.py goods
# 发送到私聊
python3 .qoder/skills/daily-inspection/scripts/ns_lark_report.py goods \
    --send --user-id ou_5d3dc8766fa70f73a8c0ace68cbbb565
```

Covers: 告警 24h diff、昨日峰值/谷值 QPS 与峰谷比、服务日环比、重启/OOM、
该 namespace 名下的 MongoDB(分片倾斜)/Redis(TTL 覆盖率 × 淘汰策略)/RocketMQ 与 Kafka 消费积压
(Kafka 用 ClientIp 反查确认归属)。

- **Sending is opt-in.** Without `--send` it only prints the text preview.
  Requires `--user-id` or `--chat-id`; `--print-card` dumps the card JSON.
- **Idempotency key defaults to `<ns>-<YYYYMMDD>`**, so re-running the same day
  does not re-send. Do not remove this — it is the guard against duplicate posts.
- Approved targets: DM `ou_5d3dc8766fa70f73a8c0ace68cbbb565`(朱鑫贵); the bot is
  also in 商城导购研发组 `oc_5bba5694be9f20d42adf4895d9dda668`. **Confirm with the
  user before sending to any group** — group posts are visible to others.
- Card header colour encodes severity: red = 新增 critical/error 或 OOM>0,
  orange = 其他待处理项, green = 无需处置.

## Reading Rules

**告警**
- Rank by severity, then by whether it is new. `ContainerMemoryUsageHigh +2`
  matters; `HpaMaxedOut 329 (327)` is a standing condition, not today's news.
- When one rule exceeds 50% of all firing alerts, say so explicitly — it means
  every `critical` is being drowned, and the fix is the rule or the underlying
  config, not the symptom.
- Resolved alerts are signal too: a batch of `PersistentVolumeFillingUp`
  clearing means something reclaimed space, and it will likely recur.

**日环比**
- Traffic here swings 90–170× between trough and peak, so only compare
  **equal-length 24h windows**. Never compare a point now against a point
  yesterday, and never compare partial windows.
- A QPS drop is as suspicious as a spike — it can mean an upstream stopped
  calling.
- Container restarts near **288/day per container** mean a container has been
  in `CrashLoopBackOff` at the 5-minute backoff ceiling for the whole day.
  That is a standing outage of that workload; check whether it is a `-gray`
  canary before escalating, but never treat it as background noise.

**接口级**
- Rank by `rate(http_server_requests_seconds_sum)` — seconds of request time per
  second, which by Little's law is the **average in-flight concurrency**. This
  is the cost the service actually pays. A 20s endpoint at 0.03 qps ranks below
  a 1.8s endpoint at 300 qps, which is correct.
- Concurrency also explains replica counts: ~500 in-flight requests is why a
  BFF needs hundreds of thread-blocking pods. Do not recommend cutting replicas
  before the downstream latency is fixed.
- Endpoints averaging over 60s are SSE/long-poll and are long **by design**.
  The script separates them; never report them as slow.
- Prefer 5xx over 401/403. Large steady 401 rates are usually normal token
  expiry; a 500 at any rate is a defect.

**盲区**
- Missing data is not a healthy signal. Always list the blind spots, so nobody
  concludes "queues are fine" when queue metrics do not exist.
- The instrumentation gap list is itself an inspection finding: a
  high-traffic namespace with no `http_server_requests` cannot be diagnosed
  at the endpoint level at all.

## Metric Traps

These caused wrong numbers during development; the script handles them, but
preserve them in any ad-hoc query.

- **Alert identity is the full label set.** `HpaMaxedOut` identifies itself by
  `horizontalpodautoscaler` and has an empty `pod`; keying on
  `(alertname, namespace, pod)` collapsed 329 real instances into 53.
- **`container_oom_events_total` also exists as ~348 node-scoped series** with
  no `namespace`/`pod` label. Summing everything reported 166,707 OOM events
  instead of the real 1,240. Always filter `namespace!="",container!=""`.
- **Use `max_over_time - min_over_time`, not `increase()`**, for restart and OOM
  counters. `increase()` extrapolates and inflates.
- **A P99 `histogram_quantile` over all ~344 services times out** (~12s and
  climbing, often dropping the connection). Restrict it to the services that the
  QPS query already found significant — that took 0.3s.
- **Never render a failed query as `0`.** A timed-out P99 printed as `0.00s`
  reads as "healthy" and is worse than no report. The script prints `n/a` and
  emits a data-quality note.
- Exclude `destination_workload!="waypoint"` on Istio metrics (ambient mesh), and
  watch for mixed `reporter` values double-counting.

云侧(`ns_lark_report.py`)：

- **`dds DescribeDBInstances` 默认不返回分片实例**，必须对 `sharding` 和
  `replicate` 各查一次，否则分片型 MongoDB 会整段消失。
- **资源归属用线上实例名，不要用 Pulumi state。** 命名规范是
  `env--namespace--resource`。Pulumi 只管了 59 个云资源，而线上有 100+ Redis、
  32 MongoDB、29 RocketMQ，用它做归属会漏掉一半。
  注意资源可能被别的 namespace 持有：`prod--retail-bff--goods-mongodb` 属于 goods。
  匹配时要能区分 `goods` 与 `goods-wanchi` / `goods-wanquan`。
- **Redis 的 TTL 覆盖率必须和淘汰策略一起读。** `Expires / Keys` 极低且策略是
  `volatile-lru` 时，撞 maxmemory 会**写入报错**而不是驱逐冷数据 —— 单看内存使用率
  完全看不出这个风险。
- **K8s 事件在 SLS，不在 Prometheus。** `kube_event_count` 没有导出，事件在
  SLS project `k8s-log-<ACK clusterId>` 的 `k8s-event` logstore。事件 JSON 在
  `eventId` 字段里，用 `json_extract_scalar(eventId, '$.reason')` 之类取值，
  SLS SQL 可直接聚合。这是唯一能说明「为什么失败」的数据源：实测 goods 有 4 个
  CronJob 报 `TooManyMissedTimes` 各 288 次 —— **控制器已停止排程，任务实际没在跑**，
  而 Prometheus 侧完全看不出来。
- **积压要看峰值 + 中位，不能只看瞬时值。** 云监控 `acs_kafka` /
  `MessageAccumulationV3` 只传 `instanceId` 就返回该实例所有消费组的时序
  （一实例一次调用），取值字段是 `Value`。判据：
  **中位 > 50 = 持续积压**（消费能力不足，要处理）；
  **峰值高但中位≈0 = 业务毛刺**（正常）。
  注意云监控 5 分钟周期的峰值与 `GetConsumerProgress` 瞬时值口径不同，两个都列。
- **突刺必须用 5 分钟粒度时序，24h 聚合会把它抹平。** 判据是
  **峰值 ≥ 中位 ×3 且峰值超绝对阈值**（P99 ≥1s / 5xx ≥1%）。实测 `erp-store-sync`
  P99 峰值 29.1s 而中位 0.02s（1455 倍），`pc-bff` 峰值 9.55s —— 这两个在 24h
  聚合里都不显眼。namespace 级时序查询只要 0.2 秒，成本可忽略。
- MongoDB / Redis 的 CPU 与内存都取 **24h 窗口的 max**，不要用均值 —— 均值会把
  峰值稀释掉。
- **中间件实例必须从 `repos/gitops/apps/<ns>/*.app.yaml` 解析，不能靠实例名猜。**
  这是之前数据错得最狠的地方：goods 的 Kafka 实例叫 `goods-prod` 和 `qnh-prod`，
  按 `Name.startswith("prod")` 扫会**全部漏掉**（28 个实例只有 13 个是 prod 前缀），
  结果报告显示「Kafka 1 个消费组、积压 0」，真实是 **23 个组、其中一个积压 7000+**。
  manifest 里同时给出权威的 topic / consumer group 名与接入点；
  `IAC__<OWNER>__<RESOURCE>__*` 占位符直接点明托管资源，
  如 `IAC__RETAIL_BFF__GOODS_REDIS__HOST` → `retail-bff--goods-redis`。
  解析时必须跳过 key 含 PASSWORD / SECRET / TOKEN / ACCESS_KEY 的项 ——
  这些 manifest 里存在明文口令。
- **实例来自配置后，仍要用 ClientIp 校验归属。** 同一实例上会有别的 namespace 的
  消费组（`qnh-prod` 上有 6 个组的消费者实际在 `dfp`）。用 Kafka
  `GetConsumerProgress` 的 `ClientIp` 反查 `kube_pod_info` 的 `pod_ip`。
  消费者数应能对上 Pod 数（96 / 60 / 48 / 32），对不上就是归属判错了。
- RocketMQ 的 `ListConsumerConnections` 实测恒返回 0 连接，拿不到客户端 IP，
  所以只按 manifest 声明的组名匹配，并**标注「归属未校验」**。
- **不要用关键词模糊匹配组名。** 从 workload 名剥后缀得到的单词关键词
  （`task-sync-mqconsumer` → `task`）会命中 `marketing-task-log-gray` 这类无关组，
  把别人的积压报成自己的。
- **Redis 实例 >100 时必须分页。** `DescribeInstances --PageSize 100` 只返回
  第一页，账号里有 117 个实例，`prod--retail-bff--goods-redis`（goods 唯一的
  prod Redis）正好落在第二页，导致报告整段丢掉 Redis 行。用 `_redis_instances()`
  翻页拿全。
- **告警查询失败不能渲染成「恢复」。** ALERTS 查询超时返回空集时，
  与 24h 前的 diff 会显示「全部恢复」（09-04 报告误报过 `firing 0 / 恢复 24`）。
  `k8s_summary` 现在用 `_warn` 增量检测查询失败，失败时不输出增删。
  同理所有 Grafana 查询都加了单次重试（实测最近 25 次运行中有 3 天出现该错误：
  09-04 的瞬时查询失败直接造成告警误报；08-16/17 的区间查询失败使突刺检测
  无数据，报告假绿显示「无需处置」，而后续运行证明当时确有突刺）。
- **CronJob 有两种形态，`TooManyMissedTimes` 只说明「调度在迟到」，不说明任务没跑。**
  goods 的 4 个 `while true` 常驻任务用 `* * * * *` 触发，事件永久刷屏（每天 1152 条）
  但业务实际在跑（Pod 已运行 52-148 天，目标接口每分钟有流量）。真正的失败模式是
  常驻 Pod 死掉后控制器不会自愈。`cronjob_health` 解析 `cronJobs.jsonnet`：
  常驻任务查 `kube_pod_status_phase{phase="Running"}`（只有 `kube_pod_start_time`
  不够——Failed 相位的残留 Pod 会被误判存活）；周期任务查 24h 内有无新
  `kube_pod_created`。
- **「Pod 被创建」≠「任务执行了」。** 周期任务不能只查 `kube_pod_created`：
  2026-09-07 实测节点 `172.16.128.18` 的 CNI 故障让 17 个 cron Pod 卡在
  `ContainerCreating`（无 IP）长达 4h+，Pod 全都"被创建"了，但 `product-list-task-cron-job`
  自 09:00 后 5 个小时级执行全部未发生（目标接口 24h 仅 19 次调用，应 24 次）。
  正确判据是查 `kube_pod_container_status_waiting_reason` 中卡在启动前状态
  （ContainerCreating / ImagePullBackOff / ErrImagePull / CreateContainerConfigError）
  且年龄 > `STUCK_MIN`(30) 的 Pod。
  - **不要用「24h 内有 Succeeded 的 Pod」判活**：小时级任务历史上有大量成功记录，
    会把当前这次卡住完全掩盖（实测该判据返回 8，误判 alive）。
  - **取 waiting reason 时要单独查该指标**：它自带 `reason` 标签；与年龄用
    `and on(pod)` join 会丢标签，`group_left(reason)` 在此处返回空。分开查再在
    Python 侧按年龄过滤。
- **节点 `Ready=1` 不代表能正常拉新 Pod。** 上述 CNI 故障期间节点 Ready、四类资源
  压力全为 false、`unschedulable=0`（仍在吞新 Pod），terway-eniip / istio-cni /
  ztunnel 全部 Running 且 0 重启——常规健康检查看不出任何异常，只有
  「新 Pod 拿不到 IP」这一个信号。巡检要盯 `kube_pod_info{pod_ip=""}`。
- **Redis 监控时间格式必须带秒**（`%Y-%m-%dT%H:%M:%SZ`）；只到分钟时
  `DescribeHistoryMonitorValues` 静默返回空。淘汰策略与 TTL 覆盖率
  （`DescribeParameters` + `Keys/Expires`）仅在内存峰值 ≥70% 时才查，
  避免给每个实例多两次 API 调用。
- **复发标记**：同一待处理项（突刺/积压）连续多天出现时，
  `mark_recurrence` 在条目后标注「已连续 N 天」。连续复发的突刺不是噪音，
  是未修复的缺陷，标注让复发升级为处理信号而非被习惯化忽略。
  状态文件在 `scripts/state/<ns>.recurrence.json`。
  - **只有 `--send`（权威日报）才写状态文件。** 预览（无 `--send`）只标注不落盘。
    原因：预览与 09:00 调度运行处于不同数据窗口（积压可能已消退），若预览也写，
    会把生产的 streak 抹掉——2026-09-07 实测发生过：一条连续 4 天的积压 streak
    被一次预览覆盖丢失。
  - **「没查到」不等于「已解决」。** 凭据失效或 `--skip-cloud` 时积压/事件类检查
    根本没运行，其条目自然缺失；此时这些类别的 streak 会被保留而非清零
    （`unverified` 参数），否则一次凭据故障会伪装成「积压一夜之间全清了」。
    与「查询失败不得渲染成恢复」是同一条原则。
- **阿里云凭据会过期，且过期是静默的。** `aliyun()` 原本吞掉所有异常返回 `None`，
  OAuth token 失效时 MongoDB/Redis/MQ 段全部返回空，报告渲染成看似健康的「无数据」。
  现在：启动前 `preflight_cloud()` 用 `aliyun sts GetCallerIdentity` 预检；失效则
  跳过云侧、字段显示 `⚠️ 凭据失效未检查`、并写入数据质量警告。
  凭据刷新属认证操作，需人工执行 `aliyun configure --profile xlb-prod`，不要代劳。
  **排查时同理**：`aliyun ... 2>/dev/null` 会把凭据失效伪装成「没有数据」，
  导致得出错误结论（2026-09-07 的一次误判正源于此）。查询失败务必保留 stderr。

## Escalation

When the report surfaces something that needs depth, hand off:

- one namespace in trouble → `k8s-namespace-monitor` skill
- a slow endpoint needing a cause → traces (`victoria-traces`) or app logs
  (`victoriaLogs`), since neither Istio nor micrometer explains *why*
- node-level suspicion → the `🔥Nodes` dashboard (`/d/nodes`), node_exporter

## Resources

- `scripts/daily_inspect.py` — the report generator. Queries Prometheus through
  Grafana's datasource proxy (uid `prometheus`); no cluster access needed.
  Thresholds live in the `TH` dict at the top: `qps_pct` 30%, `p99_pct` 50%,
  `p99_abs` 2s, `err_pct` 0.1%, `res_pct` 50%, endpoint `conc` 5, plus
  `STREAM_SEC` 60 for the streaming split. Tune these if the report gets noisy —
  a report nobody reads is worse than a narrower one.
- `scripts/ns_lark_report.py` — 单 namespace 版，输出飞书交互式卡片。复用
  `daily_inspect` 的 `Prom` 客户端与告警 diff，另加了 `range_q`(峰值检测需要区间
  查询，`Prom` 只有瞬时查询)。依赖：`aliyun` CLI 已配置只读凭据、`lark-cli` 已配置
  应用并把 bot 加入目标会话。实测完整一次约 30–50 秒（2026-09 数据）。
- `scripts/gitops_deps.py` — 从 `repos/gitops/apps/<ns>/*.app.yaml` 提取该 namespace 的
  prod 中间件依赖（Kafka 实例、Redis/ES 主机、consumer group 与 topic 名、
  `IAC__` 资源名）。可单独运行 `python3 scripts/gitops_deps.py goods` 核对。
  跳过一切疑似凭据的 key —— manifest 里有明文口令。
- `scripts/test_ns_lark_report.py` — 无网络的行为测试（17 项）：分页、假恢复保护、
  CronJob 判死/判活、复发标记同日不膨胀、截断告警、注入式重试。改逻辑前先跑它：
  `python3 .qoder/skills/daily-inspection/scripts/test_ns_lark_report.py`。
- SLS 事件依赖 `aliyun sls` 只读权限与正确的 `SLS_PROJECT`（脚本顶部常量，
  换集群需同步改）。
