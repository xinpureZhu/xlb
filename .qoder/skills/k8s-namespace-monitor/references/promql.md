# PromQL 速查 — namespace 下钻

`ns_report.py` 覆盖常规巡检。以下用于报告标记异常后的下钻。

## 直接查询

```bash
G=https://grafana.observability.ali-prod.xlbsoft.com
DS=prometheus   # 显示名 "Metrics"

# 瞬时。省略 time 即为当前;后端也接受 now-30m 这类相对写法
curl -sG "$G/api/datasources/proxy/uid/$DS/api/v1/query" \
  --data-urlencode 'query=count(kube_pod_info{namespace="goods"})'

# 区间。注意参数是 start / end,写成 to= 会被静默忽略
curl -sG "$G/api/datasources/proxy/uid/$DS/api/v1/query_range" \
  --data-urlencode 'query=sum(rate(container_cpu_usage_seconds_total{namespace="goods",image!=""}[5m]))' \
  --data-urlencode 'start=now-24h' --data-urlencode 'end=now' \
  --data-urlencode 'step=300s'

# 有哪些 label 值
curl -sG "$G/api/datasources/proxy/uid/$DS/api/v1/label/namespace/values"
curl -sG "$G/api/datasources/proxy/uid/$DS/api/v1/series" \
  --data-urlencode 'match[]=istio_requests_total{destination_service_namespace="goods"}'
```

排查历史问题才写绝对时间(ISO8601 带 `Z`,UTC 输入)。日常下钻一律用
`now-<N><s|m|h|d>`,避免把某一天固化到命令里。

若匿名读被关闭,加 `-H "Authorization: Bearer $GRAFANA_TOKEN"`。

## 关键注意事项

- `container_cpu_usage_seconds_total{image=""}` 是 pod 级(pause 容器)聚合,
  `image!=""` 是业务容器。两者**不要混用**,否则数字对不上。
- Istio ambient 模式必须排除 `destination_workload!="waypoint"`。
- `reporter` label 需留意:本网格绝大多数系列是 `reporter="waypoint"`,但部分
  namespace(如 `retail-new`)同时存在 `reporter="source"`,跨 reporter 求和会把
  同一次请求算两遍。先用 `... by (reporter)` 看分布;若同时出现多个取值,
  按单一 reporter 过滤后再比较绝对值。`ns_report.py` 会在检测到混用时给出提示。
- `response_code` 是字符串 label,用 `=~"5.."` 匹配而非数值比较。
- `increase()` 跨 Pod 重建会漏计,统计重启用
  `increase(kube_pod_container_status_restarts_total[...])`,确认现状再叠加
  `kube_pod_container_status_last_terminated_reason`。
- 时间戳一律 UTC 输入,展示时 +8 小时转 CST。

## 资源下钻

```promql
# 单 pod CPU / 内存 top
topk(20, sum(rate(container_cpu_usage_seconds_total{namespace="$ns",image!=""}[5m])) by (pod))
topk(20, sum(container_memory_working_set_bytes{namespace="$ns",image!=""}) by (pod))

# 容器级(区分业务容器与 sidecar)
sum(container_memory_working_set_bytes{namespace="$ns",pod=~"$wl-.*",image!=""}) by (container)

# 内存逼近 limit 的容器(驱逐/OOM 前兆)
100 * container_memory_working_set_bytes{namespace="$ns",image!=""}
  / on(namespace,pod,container) kube_pod_container_resource_limits{namespace="$ns",resource="memory"} > 80

# Pod 存活时长(小时);同批次启动说明是一次性扩容而非渐进伸缩
(time() - kube_pod_start_time{namespace="$ns",pod=~"$wl-.*"}) / 3600

# 节点分布是否倾斜
count(kube_pod_info{namespace="$ns"}) by (node)

# 所在节点的整体压力(判断驱逐风险)
sum(kube_pod_container_resource_requests{resource="memory"}) by (node)
  / on(node) kube_node_status_allocatable{resource="memory"}
```

## 限流与 GC

```promql
# 限流占比(比 throttled_seconds 更能反映体感)
100 * sum(rate(container_cpu_cfs_throttled_periods_total{namespace="$ns",image!=""}[5m])) by (pod)
    / sum(rate(container_cpu_cfs_periods_total{namespace="$ns",image!=""}[5m])) by (pod)

# JVM(若接入 micrometer / jmx_exporter)
jvm_memory_used_bytes{namespace="$ns",area="heap"}
rate(jvm_gc_pause_seconds_sum{namespace="$ns"}[5m]) / rate(jvm_gc_pause_seconds_count{namespace="$ns"}[5m])
sum(jvm_threads_live_threads{namespace="$ns"}) by (pod)
```

先用 `/api/v1/label/__name__/values` 确认指标存在,再引用。

## 流量与延迟归因

```promql
# 本集群 istio 指标**没有** request_operation / url path 之类的 label,
# 无法用 PromQL 下钻到具体接口。只能先按协议和实例切分,
# 再转 trace / 日志做接口级归因。
histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{
  destination_service_namespace="$ns", destination_workload!="waypoint"
}[10m])) by (destination_service_name, request_protocol, le)) / 1000

# 按 pod 下钻延迟在本网格**无效**:指标由 waypoint 上报,`pod` label 是
# waypoint 代理的 pod 名,不是应用实例。group by (pod) 只会得到几个 waypoint 副本。
# 想定位单实例拖尾,只能走 trace(victoria-traces)或应用自身指标。
# 可用的替代切分:上游来源
histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{
  destination_service_name="$svc"}[10m])) by (source_app, source_workload_namespace, le)) / 1000

# 错误明细:谁在打、打到哪、什么码
sum(rate(istio_requests_total{destination_service_namespace="$ns", response_code=~"5.."}[10m]))
  by (source_app, source_workload_namespace, destination_service_name, response_code, response_flags)

# response_flags 含义:UF/UO 上游连接失败、UT 上游超时、
# URX 重试次数耗尽、DC 下游断开、NR 无路由
sum(rate(istio_requests_total{destination_service_namespace="$ns", response_flags!="-"}[10m]))
  by (destination_service_name, response_flags)

# TCP 服务(非 HTTP)
sum(rate(istio_tcp_sent_bytes_total{destination_service_namespace="$ns"}[5m])) by (destination_service_name)

# 出向依赖的健康度(判断是自己慢还是下游慢)
histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{
  source_workload_namespace="$ns"}[10m])) by (destination_service_name, destination_service_namespace, le)) / 1000
```

## 日志与链路

延迟/错误定位到服务后,换数据源继续:

- 日志:数据源 uid `victoriaLogs`(VictoriaLogs),LogsQL 形如
  `{namespace="goods", pod=~"goods-.*"} error`
- 链路:数据源 uid `victoria-traces`(Jaeger API),按 service + 耗时区间捞慢 trace
