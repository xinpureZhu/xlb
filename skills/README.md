# XLB 仓库级 skill

跨子项目复用的操作型 skill。与 `efficiency/skills/`（效能平台专项）互不重叠：这里放**面向 ACR/GitOps 等基础设施的确定性操作**。

| Skill | 何时使用 | 入口 |
| --- | --- | --- |
| `acr-image-retag` | 把 ACR 镜像 tag 跨仓库精确复制（服务改名、建 `mart-*` 镜像、同 tag 跨仓同步）；或排查 retag 后 digest 与源不一致、`imagetools` 把单 manifest 包成 manifest list、VPC 镜像域名连不通、跨仓库复制慢 | [acr-image-retag/SKILL.md](acr-image-retag/SKILL.md) + `scripts/acr_exact_copy.py` |
| `artifacts-libsonnet-update` | 写入/更新某 GitOps 组的 `artifacts.libsonnet`（新组补镜像、按环境发 prod/prod-gray、泳道条目增删）；或排查写入后 ArgoCD 不渲染、队列 `done` 但远端未变、`artifacts-<group>` 与 `prod` 分支不一致 | [artifacts-libsonnet-update/SKILL.md](artifacts-libsonnet-update/SKILL.md) + `scripts/publish_artifact.py` |
| `cloudflare-docs-deploy` | 把根目录 `docs/` 的静态文档站部署到 Cloudflare Workers（`xlb-docs`，带 Basic Auth 访问口令）；**docs/ 更新后同步部署**；或排查 wrangler 未登录、缺 `DOCS_PASSWORD` secret、本地 `wrangler dev` 起不来、站点仍是旧内容 | [cloudflare-docs-deploy/SKILL.md](cloudflare-docs-deploy/SKILL.md) + `scripts/deploy_docs.sh` |
| `daily-inspection-mcp` | goods namespace 每日只读巡检：Grafana 会话 cookie 查 Prometheus + aliyun 只读查中间件/MQ，一张卡片给出待处理事项、健康快照、容量趋势与盲区 | [daily-inspection-mcp/SKILL.md](daily-inspection-mcp/SKILL.md) + `run.sh` / launchd plist |
| `weekly-delivery-report` | 个人交付周报：名下需求、代码任务/失败节点、周期内发布单；只读 Efficiency MCP | [weekly-delivery-report/SKILL.md](weekly-delivery-report/SKILL.md) + `report.py` / launchd plist |
| `release-verification` | 发布单→观测联动：部署核对 + baseline/+15m/+60m 运行态验证，输出 PASS/WARN/FAIL/UNKNOWN | [release-verification/SKILL.md](release-verification/SKILL.md) + `deploy_check.py` / `runtime_check.py` / `verify.py` |
| `incident-rca` | 故障复盘固定证据链与模板；指标/日志/Trace/变更/反证，空结果写 UNKNOWN | [incident-rca/SKILL.md](incident-rca/SKILL.md) + `templates/rca.md` |

前两个 skill 有先后关系：镜像先在 ACR 就位（`acr-image-retag`），再把 tag 写进 GitOps（`artifacts-libsonnet-update`）。`cloudflare-docs-deploy` 与它们无关。

`_shared/efficiency_client.py` 是只读 Efficiency MCP 客户端（Streamable HTTP + PAT）；`_shared/grafana_prom.py` 用 600 权限的 Grafana 会话 cookie 直查 Prometheus；`_shared/feishu_card.py` 统一发 interactive 卡片（统计字段 + 分段列表，不把 Markdown 表格塞进卡片）。两者供周报、巡检与发布验证复用；凭据只从环境变量或 600 凭据文件读取。

`daily-inspection-mcp` 与 `weekly-delivery-report` 各带 launchd plist，安装命令见各自 SKILL.md；两者默认只读，卡片只发给本人。巡检是唯一的每日生产者（Qoder 侧的重复定时任务已移除）。

## 通用约定

- 生产写操作（ACR push、GitOps 分支写入）都属变更操作：先只读预检 → 取得明确授权 → 执行 → 回读校验。
- 两个脚本都提供只读模式（`--preflight` / `--verify-only` / `--dry-run`），优先用它们取证。
- 凭据只从环境变量或 600 权限的临时文件读取，禁止写进仓库、日志或文档。