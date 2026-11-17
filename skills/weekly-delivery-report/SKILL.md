---
name: weekly-delivery-report
description: 个人交付周报（只读 Efficiency MCP）。汇总名下需求、代码任务与失败节点、周期内 V2 发布单，生成 Markdown/JSON 并可发飞书机器人消息。用于“周报/交付度量/我这周做了什么/失败节点盘点”。
---

# 交付周报

## 运行

```bash
# 只看
python3 skills/weekly-delivery-report/report.py --days 7

# 发飞书（bot 私聊）
python3 skills/weekly-delivery-report/report.py --days 7 --send

# launchd 入口
XLB_NO_SEND=1 skills/weekly-delivery-report/run.sh
```

产物：`~/.xlb/reports/weekly/weekly-<YYYYMMDD>.md` 与 `.json`；日志 `~/Library/Logs/xlb/weekly-report.log`。

飞书发送为 **interactive 卡片**：顶部 4 个统计字段，下面按“需求分布 / 失败节点（按工单分组）/ 周期内发布单”分段；Markdown 表格只保留在归档文件里，不进入卡片。

## 数据源

| 段 | 工具 | 说明 |
|---|---|---|
| 需求 | `query_my_requirements` | 当前 PAT 用户名下需求，按产品线聚合 |
| 代码任务 | `query_my_code_tasks(archive_status=all)` | 文本块解析；失败项再用 `query_workflow_status(branch_id)` 补最后更新时间 |
| 发布单 | `query_release_orders(created_by_me=true)` | V1/V2 都返回；时间格式必须是 `YYYY-MM-DD HH:MM:SS` |
| 待审批 | 不使用 | 接口是全局 984 条，无法按人归属，避免误报 |

## 口径与坑

- `query_release_orders` 的 `start_time/end_time` 只接受 `YYYY-MM-DD HH:MM:SS`；RFC3339 会被拒。
- 代码任务是**当前状态快照**，不是本周新增；失败节点看“最后更新”判断是否历史遗留。
- 需求状态是平台 `state_key`，未映射中文；只按原值计数。
- 凭据复用 `~/Library/Application Support/efficiency-mcp/credentials.json`（600，token 到期 2026-12-08）；过期后先跑一次 efficiency bridge 登录。也可用 `EFFICIENCY_MCP_URL` + `EFFICIENCY_MCP_PAT` 覆盖。

## 定时

```bash
cp skills/weekly-delivery-report/com.xlb.weekly-report.plist ~/Library/LaunchAgents/
launchctl bootout gui/$UID/com.xlb.weekly-report 2>/dev/null || true
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.xlb.weekly-report.plist
launchctl kickstart -k gui/$UID/com.xlb.weekly-report
```

周一 09:10 触发；与每日巡检错开 10 分钟，避免同时打 MCP。