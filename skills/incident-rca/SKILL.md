---
name: incident-rca
description: 生产/测试故障复盘（RCA）的固定证据链与文档模板。用于“为什么故障 / 写复盘 / 事故报告 / 根因分析 / 影响面评估”，把告警、指标、日志、Trace、发布与配置变更串成可复核的时间线；强调 UNKNOWN 与反证，不把猜测写成结论。
---

# 故障 RCA（只读取证）

## 何时用

- 线上告警、5xx/延迟突刺、Pod 重启/OOM、中间件抖动、发布后回归。
- 需要输出可复核的复盘文档，而不是聊天里的一段推测。

## 证据顺序（先证据，后假设）

1. **影响面**：哪些服务/接口/租户/地区；开始与结束时间；错误率、P99、失败请求量。
2. **时间线**：告警首次触发、发布/配置变更、扩容/重启、恢复时间；全部用同一时区（CST）。
3. **指标**：`observability-<env>` 的 PromQL（RED、JVM/GC、连接池、Kafka lag、K8s 重启/OOM）。
4. **日志**：VictoriaLogs，按 trace_id / pod / 错误模式聚合；先 `stats` 再取样本。
5. **Trace**：VictoriaTraces；确认慢点/错误点所在服务与 span，区分 mesh 开销与业务耗时。
6. **变更**：Efficiency 发布单/工作流、GitOps 镜像与配置、ACR tag；只读核对。
7. **代码**：必要时定位到提交/分支；不在本流程里改代码。

## 规则

- 空结果写 `UNKNOWN`（缺数据/不适用），不能写成“正常”。
- 每个结论后面给证据：指标名 + 时间窗 + 数值，或日志查询 + 命中样本。
- 至少写一条**反证**：什么现象本应出现但没有出现，从而排除某个假设。
- 不打印密钥、Token、Cookie、连接串密码；只描述“存在该依赖”。
- 生产只读；任何重启/回滚/扩容只输出命令与影响，交人执行。

## 输出

复制 `templates/rca.md`，填完后放到 `docs/`（按仓库文档规范转 HTML）或事故系统。模板里的表格不要删，缺失项写 `UNKNOWN` 并说明原因。

## 相关 skill

- `observability-prod` 的 `observability-diagnostics`、`observability-inspection-mcp`、`trace-slow-loop-diagnosis`、`service-restart-cascade-diagnosis`。
- 仓库内 `skills/release-verification/`：发布后 15/60 分钟窗口验证。
- `skills/daily-inspection-mcp/`：namespace 级指标、CronJob/CNI/中间件/MQ 取证脚本（Grafana 已转 SSO，走会话 cookie；`collect.py` 可单独 import 复用）。
- `.qoder/skills/k8s-namespace-monitor/`：namespace PromQL 参考与旧版 `ns_report.py`（同样受 SSO 影响）。