---
name: daily-inspection-mcp
description: goods namespace 每日只读巡检（Grafana 会话 cookie + aliyun CLI）。一张卡片合并「待处理事项 + 健康快照 + 容量趋势 + 数据质量/盲区」：告警 24h diff、CronJob 死活、CNI 无 IP、K8s 事件、P99 突刺、重启/OOM、5xx、峰值 QPS、Mongo/Redis 峰值、MQ 积压、连续复发天数。用于「每日巡检/日报/今天线上有没有问题/定时检查」。
---

# 每日巡检（goods）

## 数据入口

- **Prometheus**：Grafana 已转飞书 SSO，匿名读会 302。用 `~/.xlb/credentials/grafana-cookie.json`（600）里的会话 cookie 走 datasource proxy。
- **云侧**：`aliyun` CLI 只读 `Describe*/List*/Get*`（MongoDB / Redis / RocketMQ / Kafka / SLS）。
- **清单**：`repos/gitops/apps/<ns>/*.app.yaml` 与 `cronJobs.jsonnet` 决定该 namespace 拥有哪些中间件实例、哪些 CronJob 需要判活。

> 旧的两份巡检已合并到这里：`.qoder/skills/daily-inspection/`（依赖已被 SSO 挡掉的匿名读，Prom 查询全空 → 告警/重启被渲染成 0）与原先的 `daily-inspection-mcp`（只有告警/5xx/P99/重启四项）。现在只有这一个入口。

## 运行

```bash
# 预览（不发送；报告写到 <ns>-<date>.preview.md）
XLB_NO_SEND=1 skills/daily-inspection-mcp/run.sh
python3 skills/daily-inspection-mcp/run_inspection.py --namespace goods --no-send

# 生成并发送飞书卡片
skills/daily-inspection-mcp/run.sh
```

产物：

- 报告：`~/.xlb/reports/daily/<ns>-<YYYYMMDD>.md`（预览加 `.preview`）
- 复发状态：`~/.xlb/state/<ns>.recurrence.json`
- 日志：`~/Library/Logs/xlb/daily-inspection.log`

`--skip-cloud` 只用 Prometheus（约 4 秒），但会把「云检查已跳过」写进数据质量，不会伪装成全绿。

补发当天卡片：默认幂等键 `xlb-daily-goods-<date>` 已被当天那次发送占用，直接重跑会被飞书静默去重（返回的是旧 message_id）。补发要换键：

```bash
python3 skills/daily-inspection-mcp/run_inspection.py --idempotency-key xlb-daily-goods-<date>-resend
```

## 卡片结构

顶部按严重度着色（红=告警/OOM/CronJob/网络，橙=其它待处理项或缺数据，绿=无需处置）：

1. **待处理 N 项**：按类别分组（告警 / 网络 / CronJob / 事件 / 稳定性 / OOM / 重启 / 突刺 / 服务 / MongoDB / Redis / 积压），重复项标「已连续 N 天」。
2. **健康快照**（4 个短字段）：告警 firing/24h 前/新增/恢复、5xx（1h，样本 <100 记 UNKNOWN）、重启/OOM（24h）、NotReady。
3. **容量与延迟**：P99 Top3、告警规则构成（`HpaMaxedOut` 这类）、峰值 QPS（峰/谷/峰谷比/24h 环比）、Mongo 分片与 Redis 峰值、MQ 24h 积压峰值与当前最高。
4. **数据质量 / 盲区**：任何未完成的检查都列出来，并由 note 说明口径与未覆盖项（VictoriaLogs 错误模式、中间件服务端指标、证书到期、SLS 不可达）。

判据：正常项不占版面；缺数据不等于正常（`effective_level` 会把绿降橙）；云凭据失效、网络阻断、SLS 不可达都分开表述。

## 认证

首次/过期后，在 omp 的 browser relay 打开 Grafana，导出 `dddl` cookie：

```js
const tab = await browser.open({ name: "grafana-health", url: "https://grafana.observability.ali-prod.xlbsoft.com/api/health", app: "relay" });
const c = (await tab.cookies()).find(x => x.name === "dddl");
await Bun.write("/Users/xinpure/.xlb/credentials/grafana-cookie.json", JSON.stringify({ cookie: `${c.name}=${c.value}`, saved_at: new Date().toISOString(), host: "grafana.observability.ali-prod.xlbsoft.com" }));
await sh("chmod 600 /Users/xinpure/.xlb/credentials/grafana-cookie.json");
```

Cookie 失效时 preflight 以 `exit=2` 结束，并发一张「巡检未执行」的红卡（幂等键带 `-fail`），不再静默。

## 定时

```bash
cp skills/daily-inspection-mcp/com.xlb.daily-inspection.plist ~/Library/LaunchAgents/
launchctl bootout gui/$UID/com.xlb.daily-inspection 2>/dev/null || true
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.xlb.daily-inspection.plist
launchctl kickstart -k gui/$UID/com.xlb.daily-inspection
launchctl print gui/$UID/com.xlb.daily-inspection | head -20
```

每天 09:00；卡片只发给本人，幂等键 `xlb-daily-goods-<date>`。`XLB_NO_SEND=1` 可关闭发送。

## 测试

```bash
python3 skills/daily-inspection-mcp/test_inspection.py
```

77 项无网络行为测试：Redis 分页、告警假恢复、CronJob 判活（常驻/周期/卡住未启动）、无 IP Pod 按节点聚合、复发天数（同日不膨胀、预览不写、未检查类别不清零）、卡片不假绿、5xx 小样本不报 0%、SLS 不可达、重试、凭据与 STS 端点回退、密钥脱敏。