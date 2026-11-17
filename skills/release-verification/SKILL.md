---
name: release-verification
description: 发布单→观测联动验证。按 V2 发布单取服务与分支，计算 baseline / +15m / +60m 三段窗口，用 Grafana Prometheus 查 Istio RED、P99 与 Pod 重启，输出 PASS/WARN/FAIL/UNKNOWN。用于“发布后验证/上线后有没有问题/灰度验证/发布回归”。
---

# 发布后验证

## 输入

- `--release-order <V2 ObjectID>`：精确发布单；或
- `--branch <iter-xxx>`：按分支名找最近的 V2 发布单。

## 运行

```bash
# 一次跑完：落地核对 + 运行态
python3 skills/release-verification/verify.py --release-order <id>

# 分开跑
python3 skills/release-verification/deploy_check.py --release-order <id>
python3 skills/release-verification/runtime_check.py --release-order <id>

# 发飞书
python3 skills/release-verification/verify.py --release-order <id> --send
```

产物：`~/.xlb/reports/release/<order-id>-<timestamp>.md`。飞书卡片：部署/运行态统计 + 每服务一行三段窗口（QPS / 5xx / P99 / 重启）；有未覆盖服务时表头转橙。

依赖 Grafana 会话 cookie：`~/.xlb/credentials/grafana-cookie.json`（600）。过期后按 `daily-inspection-mcp/SKILL.md` 的步骤重新导出；`runtime_check.py` 会以 HTTP 302/401 快速失败。

## 流程与口径

```text
query_release_orders / get_release_order_detail
  → 服务 + namespace + code_branch_id + workflow
  → t0 = workflow completed 的 branch.updated_at（回退 submitted_at）
  → baseline [-60m,0] / plus_15m [0,+15m] / plus_60m [0,+60m]
  → istio_proxy:* RED/P99 + kube_pod_container_status_restarts_total
  → 部署 PASS/FAIL + 运行态 PASS/WARN/FAIL/UNKNOWN
```

`deploy_check.py`：发布单状态 + 每服务 `execution_view.status` + target/actual image digest + 分支当前节点。

`runtime_check.py` 判定：

| 结论 | 条件 |
|---|---|
| FAIL | 任一窗口 5xx > 5%，或 P99 > 3×baseline 且 >1000ms |
| WARN | 任一窗口 5xx > 1%，或 P99 > 2×baseline 且 >500ms，或新增重启 > 0 |
| UNKNOWN | 目标 svc 在该时点无指标；不得判 PASS |
| PASS | 有数据的窗口均无上述信号 |

## 边界

- `t0` 取 workflow 完成时间，不是发布单提交时间；提交到实际部署可能跨天（本次 pc-bff 提交 11-09、完成 11-11）。
- Istio `svc` 是逻辑服务名；`pc-bff-gray` 可能没有独立 series，缺数据就标 UNKNOWN，不用主服务指标冒充灰度。
- Java 服务可另补 `http_server_requests_*`；Node/Go 可能只有 Istio 指标。
- 只读；不触发发布、不回滚。发布动作仍走 V2 发布单与人工审批。