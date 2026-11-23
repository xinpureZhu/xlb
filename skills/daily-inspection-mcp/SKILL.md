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

## 卡片结构（Card JSON 2.0）

版式对齐团队在看的「云资源日报」卡片：4 列 KPI + 一行派生指标 + 灰条表头 + 右对齐数值的分组行 + 灰字脚注。表头配色：红=告警/OOM/CronJob/网络，**wathet（浅蓝）=其它待处置项或缺数据**（橙色发闷，已换掉），绿=无需处置。正文只保留红色一个强调义，容量类数字用常规深色。

1. **KPI 两列 × 两行**（`flex_mode: bisect`，四格并排在手机上会挤）：待处置 / 告警 / 5xx（1h）/ 重启·OOM。数值按方向着色；1h 请求 <100 时显示 `UNKNOWN` 而不是 0.000%。
2. **派生行**：告警规则构成，例如 `` `HpaMaxedOut` / warning ×37``。
3. **分组事项**：每个 section 由「`▎名称 · 说明` + 一句灰字结论 + 若干行」组成。行内左列是对象 + 灰色上下文，右列右对齐数值。结论行是新增的「讲性」层：突刺说清谁最严重、是单点还是持续；延迟与容量给环比与最高水位是否越过 80%；积压给「几组超 1 万、当前是否已清空」。重复项标「已连续 N 天」。
4. **突刺行**：`中位 49 ms · ×490 · 10:50 · 2 点/10m` —— 倍数说明尖峰程度，`点数/时长` 区分「一次抖动」与「持续抖动」。
5. **延迟与容量 / 积压**：峰值 QPS（峰/谷/峰谷比/环比）、Mongo 分片与 Redis 的 `CPU / 内存`、消费组 24h 积压峰值（当前为 0 时不显示）。
5. **数据质量**：任何未完成的检查都列出来（云凭据/网络、SLS、查询失败）。
6. **脚注**：口径、盲区、series 总数。

### 约束

- 飞书单卡上限 **200 个组件**（含嵌套）：待处置最多 10 行（其余折成「…另 N 项」，完整清单在 MD 报告里），积压最多 3 组 + 汇总行，Mongo 最多 3 个分片。`test_inspection.py` 有 budget 回归用例，改版式后先跑它。
- Card 2.0 需要飞书客户端 ≥ 7.20；老客户端会显示升级提示。
- `XLB_CARD_SCHEMA=1.0` 切回旧的 fields 版式（无表格，纯文本降级）。
- `text_color` 只接受颜色枚举；直接写 `rgba(...)` 会被服务端以 `10002 invalid color` 拒绝（自定义色要先用 `config.style.color` 注册再引用）。
- 服务端拒绝 2.0 时 `send_card` 会自动换幂等键回退 1.0 再发一次，不会让当天报告丢失。

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

86 项无网络行为测试：Redis 分页、告警假恢复、CronJob 判活（常驻/周期/卡住未启动）、无 IP Pod 按节点聚合、复发天数（同日不膨胀、预览不写、未检查类别不清零）、卡片不假绿、5xx 小样本不报 0%、卡片组件数不超 200、SLS 不可达、重试、凭据与 STS 端点回退、密钥脱敏。