---
name: code-task-create
description: Drive 轻舟 (Efficiency platform) code tasks and branch flow over HTTP with a browser cookie, for when the efficiency MCP server is unavailable or lacks the tool — create code tasks on a Feishu work item, pull the requirement branch from a task, and flip the iteration-branch skip switches (promotion gate / dev CD / release_test auto pass). Use when asked 创建 code task / 创建代码任务 / 给工作项加服务 / 拉分支 / 跳过门禁 / 跳过 dev cd 部署 / release_test 自动放行, or when create_branch has no task_id because the work item has no code task.
---

# 创建轻舟 code task

## Overview

Efficiency MCP 只暴露了 code task 的**查询/归档**能力，没有创建工具。创建能力在后端
`feishu-tasks` 公开路由组上，本 skill 直接调用它，用浏览器会话 cookie 通过网关。

一个 code task = 一个飞书工作项 × 一个 CMDB Application CI。拉分支
(`create_branch`) 必须先有 code task。

## Contract

```
POST {base}/feishu-tasks/batch-create        创建（数组入参）
POST {base}/feishu-tasks/by-workitem         查询 {"work_item_id":"..."}
POST {base}/feishu-tasks/delete              删除 {"id":"<taskId>"}
POST {base}/api/v1/service-tree/flat         按名字查 Application CI id {"search":"dfp-sync"}
POST {base}/api/v1/branches/create-from-task 拉需求分支 {"taskId":"...","branchFlow":"iteration"}
POST {base}/api/v1/branches/merge            合并到迭代分支 {"branchId":N,"mergeMessage":"..."}
POST {base}/api/v1/iteration-branches/skip-promotion-gates    {"iterationBranchId":N,"skipGates":[...]}
POST {base}/api/v1/iteration-branches/skip-dev-cd             {"iterationBranchId":N,"skipDevCd":true}
POST {base}/api/v1/iteration-branches/release-test-auto-pass  {"iterationBranchId":N,"releaseTestAutoPass":true}
base = https://z.xlbsoft.com/api/efficiency
```

注意两套前缀：`feishu-tasks/*` 挂在公开组（无鉴权中间件），`api/v1/*` 走鉴权组，
门禁/dev CD 开关还要过 `OpSkipPromotionGate` 分支负责人权限校验。

**门禁白名单**：只有 `requirement_completeness`（需求分支完整性检查）可豁免。
`rebuild_pending` / `prod_merge_conflict` / `behind_prod` / `prod_gray_baseline` 是红线，
传了直接报 `核心门禁 X 不允许跳过`。空数组 `[]` 表示撤销全部豁免。

三个开关都是**迭代分支级粘性配置**，带审计日志（操作人来自 cookie 身份），后续迭代
一直生效，不是一次性的。`skipDevCd` 和 `releaseTestAutoPass` 相互独立：只跳 dev CD 的话
release_test 仍需人工放行。

请求体只有 4 个字段真正生效：

```json
[{ "work_item_id": "7059848621", "type": "story",
   "owner_side": "BACKEND", "application_ids": [2350] }]
```

- `type`：`story` 需求 / `issue` 缺陷，必须与飞书工作项类型一致。
- `owner_side`：`BACKEND` / `PC` / `APP` / `MINIAPP` / `POSQT` / `DW`。
- `application_ids`：**CMDB Application CI id**，不是服务名、不是 serviceId。
- `product_line_id` / `system_ids` / `module_ids` 传了也会被忽略——产线、系统、模块、
  代码仓库全部由后端从 CMDB CI 关系图反查（深度 5：产线-系统-模块-服务-仓库）。

响应 `{"code":0,"data":{"Count":1},"msg":"批量创建任务成功"}`；HTTP 恒为 200，成败看
`code` 和 `Count`。

服务端路由无鉴权中间件，cookie 只是为了过网关。

## Workflow

1. **拿 Application CI id**：用 MCP `search_application`，keyword 用服务名。注意同名
   多套（如 `goods` / `goods-wanquan` / `goods-wanchi` 各有一个
   `op-store-product-relation`），必须让用户确认 namespace，选错会把 task 建到错误的
   租户仓库上。
2. **查现状**（只读，先做）：

   ```bash
   python3 .qoder/skills/code-task-create/scripts/code_task.py list --work-item 7059848621
   ```

   已存在同 work item + application 的 task 时不要重复创建。
3. **dry-run 给用户确认**（不带 `--yes` 即为 dry-run，只打印 payload）：

   ```bash
   python3 .qoder/skills/code-task-create/scripts/code_task.py create \
     --work-item 7059848621 --app-id 2350 --type story --owner-side BACKEND
   ```
4. **用户确认后**加 `--yes` 提交，脚本会自动回查该工作项下的 task 总数。
5. **拉需求分支**（不带 `--yes` 先 dry-run）：

   ```bash
   python3 .qoder/skills/code-task-create/scripts/code_task.py branch --task-id <taskId> --yes
   ```

   返回需求分支 id 和迭代分支 id；同一服务同一发布日期的多个规划迭代会归到同一条
   `I<日期>` 迭代分支，每个服务各有自己的迭代分支。
6. **跳过门禁 / dev CD**（作用于上一步返回的**迭代分支 id**）：

   ```bash
   python3 .qoder/skills/code-task-create/scripts/code_task.py switches --iteration-branch <id>
   python3 .qoder/skills/code-task-create/scripts/code_task.py skip --iteration-branch <id> --yes
   ```

   `skip` 默认三件套全开（门禁豁免 + 跳过 dev CD + release_test 自动放行），用
   `--no-gates` / `--no-dev-cd` / `--no-auto-pass` 单独关掉。执行后会自动回读确认。
7. **合并到迭代分支**（如需要）：`POST /api/v1/branches/merge`，body
   `{"branchId":<需求分支id>,"mergeMessage":"..."}`，返回 MR id 与 `mergeStatus`。

## Cookie

从 `/private/tmp/z_cookie.txt` 读取（`Z_COOKIE_FILE` 可覆盖路径，`Z_COOKIE` 可直接给
值）。cookie 是 `z.xlbsoft.com` 的浏览器会话（`production_qiankun_token` 等）。
**不要打印、复述、写入文档或提交 cookie 值**；只描述"已配置"。
`Z_BASE_URL` 可切换环境。

## 失败对照

| 返回 | 原因 | 处理 |
|---|---|---|
| `工作项 X 不存在` | Mongo `work_items` 没同步该工作项 | 先 `POST /feishu-workitem/sync-by-ids` 同步 |
| `工作项 X 未关联规划迭代` | 飞书工作项没挂规划迭代（后端已自动重拉一次仍为空） | 回飞书补规划迭代，再重试 |
| `服务X已经存在` | 同 work item + application 已有 task（含未归档的） | 用 `list` 确认；要重建先 `delete` |
| `服务X在本次请求中重复提交` | 同一次请求里 `application_ids` 有重复 | 去重 |
| `code:0` 但 `Count:0` | Application CI 查不到或 CI 摘要为空，后端静默跳过 | 核对 CI id 是否为 Application 类型、CMDB 关系是否完整 |
| task 建出来了但 `code_repository` 为空 | Application CI 没绑 Repository | 先补 CMDB 绑定，否则拉分支会失败（见 efficiency `skills/workitem-branch-creation-debugging.md`）|
| 拉分支返回 `app_service_configuration_incomplete` + `missingBindings` | AppService 缺技术栈/仓库/流水线配置（实测 dfp-sync 缺 `technology_stack`）| 平台「服务管理 → 应用服务」补齐后重试；不要绕过校验，也不要自行改生产服务配置 |
| 拉分支返回 `task_repository_binding_mismatch` | 任务里的仓库快照已失效 | 见 `workitem-branch-creation-debugging.md`，别改 MySQL |
| 开关返回 `核心门禁 X 不允许跳过` | 传了红线门禁 | 只能传 `requirement_completeness` |
| 开关返回 403 `仅具有权限的负责人可配置门禁豁免` | 当前 cookie 身份不是该分支负责人 | 换有权限的人操作，不要提权绕过 |
| `/iteration-branches/*` 返回 404 | 线上部署版本没有这组路由（本地代码有，实测 2026-08-24 出现过）| 开关类配置改不了就跳过；门禁豁免失效时保证需求分支处于已合并状态即可让 `requirement_completeness` 自然通过 |

## 安全边界

- 创建/删除都是对生产 MongoDB 的写操作，**必须先 dry-run 并取得用户明确确认**才加
  `--yes`。
- 删除只用于回滚刚建错的 task；已经关联分支的 task 后端会拒绝删除。
- 一次只处理用户点名的服务，不要自行扩展到"看起来相关"的同名服务。
- 跳过门禁 / dev CD 是**绕过发布安全检查**，且是粘性配置：必须由用户明确要求，说清
  影响面（后续迭代持续生效、审计日志记在 cookie 身份名下），不要作为"让流程跑通"的
  默认手段。红线门禁不要尝试绕过——遇到它说明真有问题要修。
- 拉分支被 `app_service_configuration_incomplete` 挡住时，去补服务配置，不要改
  MySQL/Mongo 或绕过校验。

## 代码出处

- 路由：`efficiency/router/feishu/feishu.go:157`（feishu-tasks）、
  `efficiency/router/workitem/workitem.go:787`（branches）、`:830`（iteration-branches）
- handler：`efficiency/api/v1/feishu/feishu_task_handler.go:47`、
  `efficiency/api/v1/workitem/promotion_gate_skip_handler.go:42`、
  `efficiency/api/v1/workitem/dev_cd_skip_handler.go:42`
- 入参与枚举：`efficiency/model/feishu/feishu_task.go:7`、`:90`、
  `efficiency/model/workitem/request/requirement_branch_request.go:4`
- 创建逻辑（CI 反查、去重、插库）：`efficiency/pkg/feishu/feishu_task_service.go:1450`
- 规划迭代校验：`efficiency/pkg/feishu/feishu_task_service.go:1350`
- 门禁白名单/红线常量：`efficiency/pkg/workitem/service/promotion_gate_constants.go:13`
- 开关服务与审计：`efficiency/pkg/workitem/service/promotion_gate_skip_service.go:35`、
  `efficiency/pkg/workitem/service/dev_cd_skip_service.go:35`
- 外部地址规则：`docs/17-efficiency-api-manual.html:81`
