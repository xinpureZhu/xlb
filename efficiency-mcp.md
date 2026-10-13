---
name: efficiency-mcp
description: "Activate when the user interacts with the Efficiency Platform (效能平台) MCP server, asks about DevOps workflows (branching, deployment, approval, release), CMDB modeling/data operations, service tree queries, or needs guidance on MCP tools for code task management, branch operations, workflow orchestration, app-service creation, and CMDB maintenance."
license: MIT
compatibility: opencode
---

# Efficiency MCP Skill

MCP server 已通过协议自身暴露了 71 个 tool 的名称、描述、参数、Read/Write 分类和 Instructions。本 Skill 仅补充**协议未覆盖的决策知识**。

## 核心规则

1. **Phase 1 范围**：仅 dev / test / staging。prod-gray / prod 是 Phase 2。
2. **查询优先**：先用 Read tool 获取 branch_id / task_id / product_line_id / system_id / module_id，再调 Write tool。
3. **reset_branch_state 禁止自动确认**：返回 preview 后停止，等用户手动 confirm_action。
4. **运行态 Critical 操作禁止自动确认**：`delete_app_service_pod`、`restart_app_service_workload`、`rollback_app_service` 返回 preview 后必须停止，等用户手动 confirm_action。
5. **不要频繁轮询**：Write 操作后等 ≥1 分钟再查状态，连续查询不超过 3 次。
6. **服务命名需用户确认**：`app_code` 和 `name` 是业务意图字段，AI 可以建议但不能替用户最终决定；创建前必须用 `check_app_code` 精确查重。
7. **CMDB 写入更谨慎**：CMDB 的 attribute / CI type 绑定 / CI / relation / relation view 写工具都会先返回 preview，必须让用户确认 `confirm_action`；写入前先读取现有模型、属性、关系和 CI 详情。
8. **CMDB 写入需要角色白名单**：调用 CMDB 写工具的用户必须命中服务端 `mcp.cmdb-write-authority-names` 配置；未配置时默认拒绝。工具仍可能出现在列表里，无权限时会在 preview 或 confirm 阶段失败。
9. **MySQL 修复 SQL 直接输出原始 DML**：当用户要求生成 MySQL 修改或修复语句时，主输出必须是可直接执行的 `UPDATE`、`INSERT` 或 `DELETE` 原始 SQL，不得使用 `START TRANSACTION`、`COMMIT`、`ROLLBACK` 等事务语句包装。校验用 `SELECT` 可以单独附加，但不能替代 DML。修改语句必须使用精确 `WHERE` 条件约束记录 ID、业务键、当前值和软删除状态，并说明预期影响行数；除非用户明确要求执行，否则只生成语句，不连接数据库写入。
10. **批量克隆仅限平台超级管理员**：`batch_clone_app_services` 和 `get_app_service_batch_clone_detail` 只允许“超级管理员 / admin / 系统管理员”角色调用；写操作仍需 `confirm_action`，确认时服务端会重新校验最新角色。

## AppService 自助运维能力

运行态排障优先使用 Read tool，只有在日志、事件、pipeline/provision 信息足够明确后才提出 Write 操作：

- **服务运行状态**：`get_app_service_status`
- **服务创建/重试历史**：`query_app_service_provisions`
- **仓库上下文**：`get_app_service_repository_runtime`、`get_app_service_repository_file_content`
- **Pipeline 排障**：`get_app_service_pipeline_runtime`、`query_app_service_pipeline_history`、`get_app_service_pipeline_job_log`
- **Pod 排障**：`get_app_service_pod_detail`、`query_app_service_pod_events`、`get_app_service_pod_logs`
- **受控恢复操作**：`retry_app_service_provision`、`delete_app_service_pod`、`restart_app_service_workload`、`rollback_app_service`

恢复操作规则：
- `retry_app_service_provision` 是 Write 操作，必须 preview + confirm。
- `delete_app_service_pod`、`restart_app_service_workload`、`rollback_app_service` 是 Critical 操作，AI Agent 不得自动确认，必须等待用户明确手动确认。
- `restart_app_service_workload` 仅支持 deployment / statefulset / rollout，不支持 cronjob。
- `get_app_service_pod_logs` 默认 `tail_lines=1000`；`get_app_service_pipeline_job_log` 默认 `limit=2000`、最大 `5000`。

## AppService 批量克隆

批量克隆仅支持平台超级管理员，并固定复用源服务代码仓库：

```
query_app_services / get_app_service_detail → 获取精确 source_service_id
  → batch_clone_app_services(items=[...], reason="...") → fresh 预检
  → 存在 blocker：修正输入后重新预检，不会生成 pending action
  → 可提交：等待用户明确确认 → confirm_action
  → 记录 batch_id / approval_url
  → get_app_service_batch_clone_detail(batch_id=...) 跟踪审批和目标执行状态
```

规则：
- `batch_clone_app_services` 不接受模式参数，服务端固定使用 `reuse_source_repository`。
- `items` 是结构化数组，每项必须包含 `source_service_id`、`target_app_code`、`target_name`、`target_namespace`。
- 首次调用只做现有业务 fresh 预检；仅在 `submittable=true` 时创建 pending action。
- 幂等键由 MCP 生成并保存在 pending action 中；确认失败后重试同一 action 会复用该键。
- 批次提交的含义是创建审批和目标预约，不代表所有目标已克隆完成；用详情工具继续跟踪。

## CMDB 能力总览

CMDB 工具分五组：

- **属性定义**：`query_cmdb_attributes`、`get_cmdb_attribute`、`create_cmdb_attribute`、`update_cmdb_attribute`
- **模型定义**：`query_cmdb_citypes`、`get_cmdb_citype`、`query_cmdb_citype_attributes`、`query_cmdb_citype_relations`、`bind_cmdb_citype_attribute`
- **配置项数据**：`query_cmdb_cis`、`get_cmdb_ci`、`query_cmdb_ci_relations`、`create_cmdb_ci`、`update_cmdb_ci`
- **CI 实例关系**：`query_cmdb_relation_types`、`create_cmdb_ci_relation`、`delete_cmdb_ci_relation`、`batch_create_cmdb_ci_relations`、`batch_delete_cmdb_ci_relations`、`replace_cmdb_ci_parent_relations`
- **服务树/关系视图**：`query_cmdb_relation_views`、`get_cmdb_service_tree_full`、`get_cmdb_service_tree_flat`、`preview_cmdb_service_tree_bindings`、`bind_cmdb_service_tree_application`、`batch_bind_cmdb_service_tree_applications`、`create_cmdb_relation_view`
- **中间件运行时关系**：`preview_cmdb_middleware_runtime_bindings`、`bind_cmdb_middleware_runtime_binding`、`batch_bind_cmdb_middleware_runtime_bindings`

使用原则：
- 能用名称检索时先查列表，写入时使用精确 ID：`attribute_id`、`type_id`、`ci_id`。
- 创建或更新 CI 前，先 `get_cmdb_citype` + `query_cmdb_citype_attributes` 确认必填字段、唯一字段、展示字段和属性名；`values_json` 优先使用属性 `name`，不要依赖 alias。
- 查询 CI 关系时先用 `query_cmdb_citype_relations` 理解类型关系，再用 `query_cmdb_ci_relations(ci_id=..., direction="children|parents")` 查实例关系。
- 写 CI 实例关系前先查 `query_cmdb_relation_types` 和 `query_cmdb_citype_relations`；删除、替换、服务树绑定属于高风险操作，必须让用户确认 preview 后再 `confirm_action`。
- 服务树绑定优先使用 `preview_cmdb_service_tree_bindings` 预览，再用 `bind_cmdb_service_tree_application` 或 `batch_bind_cmdb_service_tree_applications` 落地；绑定工具会自动解析 relation type，并只替换同父类型关系。
- 中间件归属服务优先使用运行时证据：collector 通过云厂商管控面 API（首期为阿里云 OpenAPI）获取客户端 IP，后端通过 Agent 将 IP 反查到 Pod/workload/app-service，再用 `preview_cmdb_middleware_runtime_bindings` 预览 `Application -> MiddlewareInstance depends_on`，确认后用单条或批量 bind 工具落地。运行时 target 从 CMDB `MiddlewareInstance` CI 属性生成，`MiddlewareInstance` 由后端从 `db-list[slowlog]` 的 `db_asset_instances_current` 同步，不以 `cloud_services` 或 `c_middleware_ci_mappings` 作为本链路数据源。默认不得使用 Redis `CLIENT LIST`、MySQL `SHOW PROCESSLIST` 或中间件数据面账号。Node/NAT/公网出口等 weak evidence 只展示，不允许通过 `observation_id` 自动绑定。该绑定只新增缺失依赖关系，不删除已有中间件依赖。
- `get_service_tree` 面向创建应用服务的产品线/系统/模块定位；`get_cmdb_service_tree_full` / `flat` 面向通用 CMDB service tree 数据检查。
- `create_cmdb_relation_view` 创建的是 CMDB preference relation view，不等同于 Efficiency 聚合服务树。

## 批量流水线迁移

CodeUp GUI Pipeline 转 YAML、流水线接管统计、服务-仓库-流水线批量替换、Pipeline CI 同步和 CMDB 绑定计划属于治理迁移流程，不放在主 MCP Skill 中。处理这些任务时使用独立 Skill：`codeup-pipeline-yaml-migration`。

## Setup

推荐使用 IDE 风格自动登录获取 PAT：bridge 执行 `--login --save` 后会启动本地 loopback callback、打开浏览器完成 SSO、用一次性 code 换取 PAT，并保存到当前用户配置目录。之后 bridge 启动时会自动读取该凭证。环境变量优先级：`EFFICIENCY_MCP_PAT` > `EFFICIENCY_MCP_TOKEN` > 本地凭证文件。

OpenClaw 当前 MCP 工具暴露机制仅支持 stdio 本地子进程（"supported stdio servers launched as subprocesses"），不能直接连接 efficiency-mcp 的远程 HTTP 端点。使用 OpenClaw 时需要先构建本地 bridge：

完整对接文档见 `docs/efficiency-mcp-openclaw-integration.md`。

```bash
make build-mcp-stdio-bridge
```

自动登录并保存凭证：

```bash
./build/efficiency-mcp-stdio-bridge --login \
  --server "https://efficiency.example.com/efficiency/mcp" \
  --client-name "OpenClaw Efficiency MCP" \
  --save
```

如需为其他平台分发 bridge：

```bash
make build-mcp-stdio-bridge-cross
```

默认输出 `darwin/amd64`、`darwin/arm64`、`linux/amd64`、`linux/arm64`、`windows/amd64`、`windows/arm64`。可按需覆盖平台列表：

```bash
make build-mcp-stdio-bridge-cross MCP_STDIO_BRIDGE_PLATFORMS="darwin/arm64 linux/amd64"
```

OpenClaw 配置示例：

```json
{
  "mcp": {
    "servers": {
      "efficiency": {
        "command": "/Users/zhuhuijun/go/src/wanchen/efficiency/build/efficiency-mcp-stdio-bridge",
        "env": {
          "EFFICIENCY_MCP_TIMEOUT": "30s"
        }
      }
    }
  }
}
```

如未使用 `--login --save`，则需要在配置中显式传入 `EFFICIENCY_MCP_URL` 和 `EFFICIENCY_MCP_PAT`（也兼容 `EFFICIENCY_MCP_TOKEN`）。bridge 只做 stdio → Streamable HTTP 协议转发；认证、权限、审计仍由远程 efficiency-mcp 处理。远程 Tool/Resource/Prompt 变更后，重启 OpenClaw 或 bridge 以刷新本地工具快照。

OpenClaw 报 `Connection closed` 时，先在终端手动诊断：

```bash
./build/efficiency-mcp-stdio-bridge --check
```

如果未使用 `--login --save`，诊断时需要显式传入 `EFFICIENCY_MCP_URL` 和 `EFFICIENCY_MCP_PAT`。如果 `--check` 失败，说明 bridge 在 OpenClaw initialize 前已经因 URL/PAT/网络/远程 MCP 初始化失败退出；先修复该错误再重启 OpenClaw。

手动 PAT 仍可作为兜底：`POST /api/v1/user/agent-token`（仅返回一次，30天过期）。在支持 Streamable HTTP MCP 的客户端配置 `url` + `Authorization: Bearer <pat>` 即可。管理：`GET /api/v1/user/agent-tokens`（列表）、`DELETE /api/v1/user/agent-token/:id`（撤销）。

## 决策树：根据场景选择工具

### 场景 1：首次开发

```
query_code_tasks(application="...") → 获取 task_id
  → create_branch(type="requirement|defect", task_id=...) → confirm_action
  → git push（MCP 外）
  → merge_branch(branch_id=...) → confirm_action
  → 部署自动触发，按需：resume_workflow / submit_approval / cd_refresh
  → query_deployment_status 验证
```

### 场景 2：创建服务

```
get_service_tree(product_line="...", product="...", system="...", module="...") → 获取 product_line_id / system_id / module_id
  → query_app_service_creation_options(tech_stack="...") → 获取 tech_stack / template_type / workload_type 可选值
  → query_codeup_repo_options(mode="create|bind", search="...") → 获取 repo_group 或 repo_id
  → query_app_service_namespace_options(module_id=...) → 获取 namespace 候选
  → check_app_code(app_code="...", namespace="...") → 确认 app_code 可用
  → create_app_service(app_code=..., name=..., product_line_id=..., system_id=..., module_id=..., repo_mode=..., tech_stack=..., namespace=..., workload_type=...) → confirm_action
  → 记录 provision_id / approval_url
  → 等审批通过后再继续观察服务详情
  → 审批通过后可能存在后台执行失败（例如 ensure_application_ci 环节的 constraint validation 报错），此时需要用 get_app_service_detail(service_id=... 或 app_code=...) / query_app_services 检查 latestProvision 状态。如果是 constraint validation 报错，通常是 CMDB 层级的关联约束配置问题。
```

说明：
- `create_app_service` 的语义是**提交服务创建申请**，不是“服务立即创建完成”。
- `get_service_tree` 走的是 CMDB `/api/v1/service-tree/full` 同源树，返回稳定的 CI ID，可直接拿来给 `create_app_service`。如果匹配不到，尝试**放宽搜索层级，如使用 `product_line=xxx, system=xxx, module=xxx` 定位准确的组织路径**，有时同名字的节点可能在不同层级会有 ID 获取偏差。
- `query_app_service_creation_options` 用于获取 `tech_stack`、`template_type`、`workload_type`、`repo_mode` 的有效选项。**如果只指定了 `tech_stack`，系统将根据这个技术栈自动匹配对应的默认 `template_type`**。
- `query_codeup_repo_options` 用于获取 `repo_mode=create` 时的 `repo_group`，或 `repo_mode=bind` 时的 `repo_id`。
- `query_app_service_namespace_options` 基于同组织路径下已有 CMDB Application 返回 namespace 候选；只有 `requires_user_confirmation=false` 时才可使用 `recommended_namespace`，否则必须让用户选择或提供。
- `check_app_code` 用于创建前精确查重；**`app_code` 是跨命名空间的唯一标识**，如果报错 Duplicate entry for uk_app_code_namespace，需要提示用户更换 app_code 并重新查重。若 `available=false`，不要继续调用 `create_app_service`。

### 场景 3：服务自助排障与恢复

```
get_app_service_detail(service_id=... 或 app_code="...") → 确认服务、仓库、pipeline、latestProvision
  → get_app_service_status(service_id=...) → 获取环境、workload、镜像、副本、Pod 和可操作权限
  → 按问题类型选择：
     创建/重试失败 → query_app_service_provisions(service_id=...)
     仓库/配置疑问 → get_app_service_repository_runtime / get_app_service_repository_file_content
     Pipeline 失败 → get_app_service_pipeline_runtime / query_app_service_pipeline_history / get_app_service_pipeline_job_log
     Pod 异常 → get_app_service_pod_detail / query_app_service_pod_events / get_app_service_pod_logs
  → 必要恢复：
     provision 失败 → retry_app_service_provision → confirm_action
     单 Pod 异常 → delete_app_service_pod → 等用户手动确认
     workload 异常 → restart_app_service_workload → 等用户手动确认
     镜像回滚 → rollback_app_service → 等用户手动确认
```

说明：
- 先用 `get_app_service_status` 返回的环境、namespace、workload、pod_name、镜像信息作为后续工具入参，不要猜测。
- Pod 删除、Workload 重启、服务回滚属于 Critical 操作，只能在用户明确确认影响范围后调用 `confirm_action`。
- `retry_app_service_provision` 应先查看 `query_app_service_provisions`，确认失败 provision 和失败 step 后再发起。

### 场景 4：二次开发（代码已推送，需合并）

```
query_code_tasks(application="...") → 获取 branch_id + merge_status
  → 如果 merge_status="merged"：reopen_branch → confirm_action
  → merge_branch → confirm_action
  → query_deployment_status 验证
```

### 场景 5：部署问题排查

```
query_deployment_status(app_name="...") → 定位环境
  → query_workflow_status(branch_id=...) → 定位节点
  → 根据节点状态：
     failed → retry_workflow → confirm_action
     suspended → resume_workflow → confirm_action
     待审批 → submit_approval → confirm_action
     CD 慢 → cd_refresh → confirm_action（仅 dev/test/staging）
     状态异常 → reset_branch_state → 等用户手动确认
```

### 场景 6：日常巡检

```
query_deployment_status → 总览
query_pending_approvals → 待审批
query_release_orders(created_by_me="true") → 我的发布单
```

### 场景 7：按产线导出服务 Code Task / 迭代分支状态

当用户问“某产线下所有服务哪些已经创建 code task、迭代分支”或要求导出审计表时，用只读 MCP 工具聚合，不要逐服务查询 code task。

```
query_app_services(page=1..N, page_size=100)
  → 本地按 organization.productLineName 精确过滤目标产线
  → query_code_tasks(archive_status="all", page=1..N, page_size=100) 一次性分页拉全量
  → 本地按 CodeTask.Application 匹配 service.name / service.app_code
  → get_app_service_detail(service_id=...) 逐服务读取 iterationBranches
  → 导出汇总 CSV、CodeTask 明细 CSV、迭代分支明细 CSV、摘要 JSON
```

口径：
- `query_app_services` 的 `organization.productLineName` 是产线过滤依据，服务名可能重复时保留 `namespace` 和 `app_code`。
- `query_code_tasks` 不传 `application` 时可分页拉全量；按 `Application` 字段本地分组。避免对 300+ 服务逐个调用 `query_code_tasks(application=...)`，会非常慢。
- `archive_status="all"` 用于统计“是否创建过 CodeTask”；未归档任务按返回文本中的 `Archived: true` 区分，未出现 `Archived` 视为未归档。
- 任务分支以 CodeTask 明细中的 `BranchID` / `BranchName` 为准；返回 `Branch: (not created)` 表示 code task 已有但任务分支未创建。
- 迭代分支来自 `get_app_service_detail.iterationBranches`，当前 MCP 口径是未归档迭代分支；没有全局 `query_iteration_branches` 工具时只能逐服务读取。
- 远程 bridge 环境变量优先级是 `EFFICIENCY_MCP_PAT` > `EFFICIENCY_MCP_TOKEN`；用户指定用 token 时，命令中显式设置 `EFFICIENCY_MCP_PAT=""`，避免误用过期 PAT。
- 生产 MCP 偶发 `context deadline exceeded` / `unexpected EOF` 时，对只读工具重试 2-3 次；最终摘要必须报告 `detail_errors` 和 `code_task_query_errors`。

推荐汇总字段：

```
服务ID, 服务名称, app_code, namespace, 生命周期, ApplicationCIID,
CMDB产线, CMDB系统, CMDB模块,
是否已创建CodeTask, CodeTask数量, 未归档CodeTask数量, 已归档CodeTask数量, CodeTaskID列表, 工单标题列表,
是否已有任务分支, 任务分支数量, 任务分支ID列表,
是否已有未归档迭代分支, 未归档迭代分支数量, 迭代分支ID列表, 迭代分支名称列表,
当前环境列表, 节点状态列表, 错误
```

交付时给出交叉统计：
- 同时有 CodeTask 和迭代分支
- 仅有 CodeTask
- 仅有迭代分支
- 二者都没有
- 总服务数、CodeTask 总数、任务分支总数、迭代分支总数、错误数

当用户要求统计某类命名族服务（如 `wanchi`、`wanquan`）时，直接复用服务汇总 CSV，不再重新请求 MCP：
- 匹配字段使用 `服务名称`、`app_code`、`namespace` 三列；任一字段命中关键词即计入。
- 匹配前统一 `lowercase + trim`；多个关键词分别统计，并额外给出“命中任一关键词”和“同时命中多个关键词”的去重服务数。
- 去重键优先用 `服务ID`；没有服务 ID 时用 `服务名称 + namespace + app_code`，避免同名服务跨 namespace 被误合并。
- 输出表格包含：当前服务总数、各关键词服务数、命中任一关键词服务数、同时命中多个关键词服务数。
- 如用户需要明细，再导出 `服务名称 / namespace / app_code / 命中关键词`，不要只给抽样。

### 场景 7a：按产线导出已创建迭代分支的服务清单

当用户要求“消费者体验/其他产线哪些服务已经创建迭代分支”时，优先使用脚本 `skills/scripts/export_app_service_iteration_branches.py`。这个脚本只读调用 AppService list/detail 接口，产出服务级清单、迭代分支明细和摘要 JSON。

当用户要求“轻舟整体推广进度 / C端 B端整体覆盖 / 最新周报整体进度”时，优先使用全量并发导出脚本 `skills/scripts/export_app_service_iteration_branches_fast.py`，它会读取全部 AppService 列表，过滤已绑定产线服务，并发读取详情中的当前未归档 `iterationBranches`，输出全量已绑定产线服务级清单、分支明细和摘要 JSON。该脚本会把单服务详情失败记录到 `detail_errors`，不要因为少量 SSL EOF / unexpected EOF 阻塞整份报告。

口径：
- `target-product-line`：只导出指定 `organization.productLineName`，例如 `消费者体验`。
- `exclude-product-line`：导出除指定产线以外的所有已绑定产线服务，例如排除 `消费者体验`。
- 未绑定产线的服务不计入目标集合，但摘要中保留 `missing_product_line_count`。
- 迭代分支只来自 `get_app_service_detail` 同源详情中的 `iterationBranches`；这是当前未归档迭代分支口径，不等价于历史全量分支。
- HTTP AppService list 使用 `pageSize`，不是 `page_size`。之前出现过因传错参数或按错误页长计算导致只导出首页/部分数据的问题；结束条件必须以 `len(records) >= data.total` 为准。
- 输出必须至少包含：服务名称、`app_code`、`namespace`、CMDB 产线/系统/模块、服务 ID、迭代分支 ID/名称/URL、当前环境、节点状态、仓库 URL、Pipeline URL、创建人、创建/更新时间。

token 安全：
- 不要把 token 写进脚本、命令历史、输出文件或 skill。
- 优先通过 stdin 传入 token；交互终端可用 `stty -echo` 临时关闭回显。
- 若环境里同时有 `EFFICIENCY_MCP_PAT` 和 `EFFICIENCY_MCP_TOKEN`，调用前显式设置 `EFFICIENCY_MCP_PAT=""`，避免旧 PAT 优先级覆盖新 token。

消费者体验产线导出示例：

```bash
stty -echo
env EFFICIENCY_MCP_PAT="" python3 skills/scripts/export_app_service_iteration_branches.py \
  --target-product-line "消费者体验" \
  --output-dir /private/tmp/efficiency_service_mapping_outputs \
  --insecure-skip-verify
stty echo
```

然后在 stdin 粘贴 token 并回车。完成后回复用户：
- 服务级 CSV：`消费者体验产线_已创建迭代分支服务清单_YYYYMMDD.csv`
- 分支明细 CSV：`消费者体验产线_迭代分支明细_YYYYMMDD.csv`
- 摘要 JSON：`消费者体验产线_迭代分支导出摘要_YYYYMMDD.json`

排除消费者体验导出示例：

```bash
stty -echo
env EFFICIENCY_MCP_PAT="" python3 skills/scripts/export_app_service_iteration_branches.py \
  --exclude-product-line "消费者体验" \
  --output-dir /private/tmp/efficiency_service_mapping_outputs \
  --insecure-skip-verify
stty echo
```

交付摘要固定报告：
- AppService 总数、实际拉取数。
- 目标/排除产线口径。
- 未绑定产线服务数。
- 检查的目标服务数。
- 已创建迭代分支的服务数。
- 迭代分支总数。
- 详情查询错误数。
- 按产线统计已创建迭代分支服务数。

全量推广进度导出示例：

```bash
env EFFICIENCY_MCP_PAT="" python3 skills/scripts/export_app_service_iteration_branches_fast.py \
  --output-dir /private/tmp/efficiency_service_mapping_outputs \
  --date YYYYMMDD \
  --workers 6 \
  --timeout 30 \
  --retries 4 \
  --insecure-skip-verify
```

全量导出文件：
- 服务级 CSV：`全部已绑定产线_已创建迭代分支服务清单_YYYYMMDD.csv`
- 分支明细 CSV：`全部已绑定产线_迭代分支明细_YYYYMMDD.csv`
- 摘要 JSON：`全部已绑定产线_迭代分支导出摘要_YYYYMMDD.json`

### 场景 7b：基于现有 MCP 能力生成过去一周交付周报

当用户问“过去一周新增哪些 CodeTask、创建哪些迭代分支、迭代分支发布/生产发布了哪些服务”时，先说明这是**现有 MCP 能力下的近似报表**，不要声称已经得到精确历史口径。

优先使用脚本化流程生成最新数据，而不是手工逐步拼接：

1. 若 `build/efficiency-mcp-stdio-bridge` 不存在，先执行 `make build-mcp-stdio-bridge`。
2. 用 `skills/scripts/weekly_efficiency_mcp_report.py` 通过 bridge 拉取 `query_code_tasks`、逐应用 `query_deployment_status`、`query_release_orders`，生成 MCP 周报快照目录。
3. 用 `skills/scripts/export_app_service_iteration_branches_fast.py` 拉取最新 AppService 已绑定产线服务与当前未归档迭代分支，生成整体推广进度输入。
4. 用 `skills/scripts/summarize_qingzhou_weekly_report.py` 合并 MCP 快照与 AppService 全量导出，生成“轻舟推广周报摘要”“轻舟平台整体推广报告”“轻舟推广YYYYMMDD-YYYYMMDD推广进度”。

命令模板：

```bash
env EFFICIENCY_MCP_PAT="" REPORT_CURRENT_DATE=YYYY-MM-DD \
  python3 skills/scripts/weekly_efficiency_mcp_report.py

env EFFICIENCY_MCP_PAT="" python3 skills/scripts/export_app_service_iteration_branches_fast.py \
  --output-dir /private/tmp/efficiency_service_mapping_outputs \
  --date YYYYMMDD \
  --workers 6 \
  --timeout 30 \
  --retries 4 \
  --insecure-skip-verify

python3 skills/scripts/summarize_qingzhou_weekly_report.py \
  --weekly-dir /private/tmp/efficiency_mcp_weekly_snapshot_YYYYMMDD_TIMESTAMP \
  --app-summary /private/tmp/efficiency_service_mapping_outputs/全部已绑定产线_迭代分支导出摘要_YYYYMMDD.json \
  --app-service-csv /private/tmp/efficiency_service_mapping_outputs/全部已绑定产线_已创建迭代分支服务清单_YYYYMMDD.csv \
  --app-branch-csv /private/tmp/efficiency_service_mapping_outputs/全部已绑定产线_迭代分支明细_YYYYMMDD.csv \
  --overall-report /private/tmp/efficiency_service_mapping_outputs/轻舟平台整体推广报告_YYYYMMDD.md \
  --short-report /private/tmp/efficiency_mcp_weekly_snapshot_YYYYMMDD_TIMESTAMP/轻舟推广周报摘要_YYYYMMDD.md \
  --full-report /private/tmp/efficiency_mcp_weekly_snapshot_YYYYMMDD_TIMESTAMP/轻舟推广YYYYMMDD-YYYYMMDD推广进度.md
```

脚本输出口径：
- `weekly_efficiency_mcp_report.py` 的默认窗口是 `REPORT_CURRENT_DATE` 当天向前 7 天，Asia/Shanghai，输出目录形如 `/private/tmp/efficiency_mcp_weekly_snapshot_YYYYMMDD_TIMESTAMP`。
- `weekly_code_tasks_approx_by_branch_suffix.csv` 是近似新增 CodeTask，按任务分支名末尾 `-MMDD` 判断。
- `weekly_iteration_branches.csv` 是当前未归档迭代分支中 `created_at` 落在窗口内的分支。
- `weekly_prod_release_orders_latest20.csv` / `weekly_prod_release_services_latest20.csv` 是 `query_release_orders` latest20 口径。
- `weekly_first_time_iteration_services_product_line_side.csv` 是候选首次创建服务的 C/B 端明细；C/B 按 `CMDB产线 == 消费者体验` 判断。
- 摘要报告必须保留 AppService `detail_error_count`，说明覆盖率可能对这些服务轻微低估。

可用只读链路：

```
query_code_tasks(archive_status="all", page=1..N, page_size=100)
  → 本地解析 TaskID / WorkItemID / WorkItemTitle / Application / BranchID / BranchName / IterationBranch
  → 从 CodeTask.Application 去重得到应用名集合
  → query_deployment_status(app_name=应用名, branch_type="iteration") 逐应用查询当前未归档迭代分支
  → 本地按 branch_id 去重，并按 created_at 落在窗口内过滤“新增迭代分支”
  → query_release_orders() 获取最新 20 条发布单
  → 本地按发布单 name 包含 prod、created_at 落在窗口内、services 聚合“生产发布服务”
```

关键口径和限制：
- 时间窗口使用明确的 `start_at/end_at`，默认按 Asia/Shanghai；最终报告必须写清楚绝对时间范围，不只写“过去一周”。
- `query_code_tasks` 底层有 `CreatedAt`，但当前 MCP 文本输出没有 `created_at`，也没有时间过滤参数。若不补新工具，只能把“新增 CodeTask”标为近似口径：优先用任务分支 `BranchName` 末尾 `-MMDD` 判断是否落在窗口内；不要用迭代分支名里的 `I202606xx` 判断 CodeTask 创建时间，那通常是迭代/发布窗口编号。
- `query_deployment_status` 必须传 `app_name`，没有全局迭代分支列表；用 CodeTask 中出现的应用集合逐个查，适合先出快照，不适合覆盖所有服务。结果需按 `branch_id` 去重。
- `query_deployment_status` 返回的是当前未归档分支状态；用 `created_at` 可过滤窗口内新建迭代分支，但已归档迭代分支可能缺失。
- `query_release_orders` 当前固定只返回最新 20 条，缺少 page/time/branch_id/environment 参数；只能做“最新 20 条发布单口径”的生产发布服务统计。`branch_names` 可能为空，不能稳定反推迭代分支发布次数。
- 发布单状态码按 MCP 说明解释：`2=成功`、`1=进行中`、`9=结单`；如果返回文本显示 `status_text=未知`，报告中保留原始状态码，不要把 `9` 误判为失败。
- 生产发布服务统计用发布单 `services` 去重并计数；如果用户要求“发布了多少次生产”，需说明这是发布单服务出现次数，不是 `code_branch_release_order(environment=prod)` 的权威次数。

如果用户提供 `getReleaseOrderList` / `query_release_orders` 原始 JSON 附件，发布统计优先使用附件替换 MCP latest20 发布段：
- 先读取附件的 `data.list`、`data.total`、`page`、`pageSize`，按发布单 `id` 去重；报告必须写明“附件页口径”和接口 reported total，不要把第 1 页当全量。
- 按发布单 `created_at` 的 Asia/Shanghai 时间过滤周报窗口；输出附件全量口径和周报窗口口径时要分开。
- 发布环境用发布单 `related_env` / `environment` 判断 `prod`、`prod-gray`；服务 `cluster_type` 常常仍是 `prod`，不要把它当灰度/生产环境字段。
- 服务状态来自 `service_list.status`，同样使用发布状态码映射；服务发布次数统计按 `service_list` 条目数，唯一服务按服务名或 `serviceId + namespace + name` 去重。
- 最终周报中直接替换原来的 `latest20` 发布单和发布服务章节，不要新增一个“附件补充”章节导致重复口径。

当用户问“本周创建的迭代分支中哪些是第一次创建的服务”时，用当前已有快照做候选判定，不要声称历史绝对准确：
- 主集合使用 `weekly_iteration_branches.csv`，按 `service_id` 分组，每个服务取窗口内最早 `created_at` 分支作为本周首次分支。
- 与 `iteration_branches_current_mcp.csv` 同 `service_id` 比较；若存在早于窗口开始时间的当前未归档迭代分支，分类为“非首次”。
- 若当前未归档快照无更早分支，再用 `code_tasks_current_mcp.csv` 里同 `Application == service_name` 的 `IterationBranch` 做辅助证据：没有其它迭代分支名则为“强候选”；有其它迭代分支名则为“当前未归档口径候选，需查历史归档确认”。
- 这个判断受限于当前 MCP 只有未归档迭代分支快照、CodeTask 快照也不保证全量历史；最终报告必须使用“候选首次创建服务”表述。
- 如果要给出分支级明细，标明“首次创建分支-强候选”“首次创建分支-当前未归档口径候选”“同服务本周后续分支/非首次分支”。

当需要把首次创建服务按 C/B 端汇总时，优先按 CMDB 产线，不按 CodeTask `OwnerSide`：
- 业务口径：`CMDB产线 == 消费者体验` 归 C端，其它产线归 B端。
- 先使用按产线导出的 AppService/迭代分支服务清单（例如 `C端_非C端_已创建迭代分支服务清单_YYYYMMDD.csv`，或消费者体验/非消费者体验两份清单）补齐 `CMDB产线/系统/模块`。
- 不同导出中的 `服务ID` 可能不是 `query_deployment_status` 的 `service_id`；匹配时优先用“迭代分支 ID”精确匹配，再 fallback 到 `服务名称 + namespace`。
- 汇总时同时给“强候选首次创建服务”和“强候选 + 当前未归档口径候选”两套 C/B 端数量，用户要求一句话时默认用强候选主口径。

推荐交付文件：

```
weekly_code_tasks_approx_by_branch_suffix.csv
weekly_iteration_branches.csv
weekly_prod_release_orders_latest20.csv
weekly_prod_release_services_latest20.csv
weekly_release_orders_from_attachment.csv
weekly_release_order_services_from_attachment.csv
weekly_first_time_iteration_services.csv
weekly_first_time_iteration_branches.csv
weekly_first_time_iteration_services_product_line_side.csv
weekly_requested_report.md
summary.json
```

如果当前 Codex 会话没有直接暴露 Efficiency MCP tools，可临时通过本仓库 bridge 调用远端只读工具：
- 使用 `./build/efficiency-mcp-stdio-bridge`，通过 stdin/stdout 发送 newline-delimited JSON-RPC。
- initialize 后发送 `notifications/initialized`，再调用 `tools/call`。
- 若环境同时有 `EFFICIENCY_MCP_PAT` 和 `EFFICIENCY_MCP_TOKEN` 且 PAT 可能过期，显式设置 `EFFICIENCY_MCP_PAT=""` 让 bridge 使用 `EFFICIENCY_MCP_TOKEN`。
- 输出放 `/private/tmp`；可复用脚本应放 `skills/scripts/`。如遇 `127.0.0.1:7890 operation not permitted` 或 EOF，按只读操作申请沙箱外执行并对只读查询重试 2-3 次。
- 若环境支持 Streamable HTTP，可优先用仓库脚本 `go run ./skills/scripts/efficiency-mcp-call --tool <tool> --args-json '<json>' --output /private/tmp/result.json`；批量参数较大时先写入 JSON 文件，再用 `--args-file /private/tmp/args.json`。写工具同样只会返回 preview，需要再由用户确认后调用 `confirm_action`。

### 场景 7c：轻舟新老平台发布服务统计

当用户提供 `getReleaseOrderList` / `query_release_orders` 发布单 JSON 和旧平台发布记录文本，要求统计某几天轻舟发布服务、新老平台占比、C/B 端占比或生成分日明细表时，优先使用脚本 `skills/scripts/summarize_qingzhou_release_platforms.py` 生成标准 CSV/Markdown，再按需用 spreadsheet skill 生成 Excel 多 tab。

命令模板：

```bash
python3 skills/scripts/summarize_qingzhou_release_platforms.py \
  --release-orders-json /path/to/getReleaseOrderList.json \
  --old-platform 2026-07-13=/path/to/old-platform-20260713.txt \
  --old-platform 2026-07-14=/path/to/old-platform-20260714.txt \
  --cmdb-service-tree-json /private/tmp/efficiency_service_mapping_outputs/get_cmdb_service_tree_full_productline_after_org_migration_YYYYMMDD.json \
  --date 2026-07-13 \
  --date 2026-07-14 \
  --output-dir /private/tmp/qingzhou_release_platforms_YYYYMMDD
```

核心口径：
- 新平台数据来自发布单 `service_list`，只统计 `service_list.status = 2` 的成功服务；发布日期优先从发布单名称中的 `YYYYMMDD` 提取，缺失时再用 `created_at`。
- 新平台灰度/生产环境用发布单 `related_env` / `environment` 判断 `prod`、`prod-gray`；服务里的 `cluster_type` 不能作为灰度环境字段。
- 旧平台文本格式是 `service.namespace: count`；`count <= 0` 的记录剔除，空服务名、空 namespace 或不符合格式的行记录为 malformed，不参与统计。
- 服务名称只去掉尾部 `-gray` 后缀，例如 `erp-pos-gray.erp` 归一为 `erp/erp-pos`；不要删除中间的 `gray` 字符串。
- 新旧平台按 `namespace/归一化服务名` 去重。同一天同服务同时出现在新旧平台时归入新平台，旧平台明细中剔除；跨日汇总也按相同 key 去重。
- C/B 端按 CMDB 服务树的 ProductLine 判断：`消费者体验` 为 C端，其它产线为 B端。CMDB 未匹配服务可暂归 B端，但必须在报告里列出未匹配清单，避免误当作已确认产线。
- 用户特别指定某服务归属时，以用户最新口径为准，并在报告备注中说明，例如“某服务虽然旧平台文本出现，但因新平台发布成功，最终归入新平台”。

脚本输出：
- `YYYYMMDD_new_platform_services.csv`：某日新平台成功发布唯一服务明细。
- `YYYYMMDD_old_platform_services.csv`：某日旧平台去掉 `0`、`-gray` 归一并剔除新平台重叠后的唯一服务明细。
- `combined_new_platform_services.csv` / `combined_old_platform_services.csv`：跨日期去重后的新旧平台明细。
- `qingzhou_release_platform_summary.json`：分日、分平台、C/B 端、CMDB 未匹配、旧平台剔除重叠等机器可读汇总。
- `qingzhou_release_platform_report.md`：可直接合入周报的 Markdown 汇总。

需要 Excel 表格时：
- 用 spreadsheet skill 读取脚本输出 CSV，生成一个工作簿。
- 推荐 sheet：`分日汇总`、`13新平台`、`13老平台`、`14新平台`、`14老平台`；如果日期不是 13/14，按 `DD新平台` / `DD老平台` 命名。
- `分日汇总` 至少包含：日期、平台、唯一服务数、C端服务数、B端服务数、C端占比、B端占比、成功记录数；另给分日新/老平台服务数和占比。
- 交付前做公式错误扫描和截图预览；最终只给用户 `.xlsx` 成品链接，支撑 CSV/脚本路径只有在用户要求时再展开。

### 场景 7d：从发布单附件回写飞书多维表的轻舟发版日期

当用户提供一个或多个 `query_release_orders` / `getReleaseOrderList` 原始 JSON 附件，并要求把“使用平台发布的服务”回写到飞书 Wiki/多维表的“使用轻舟发版日期”字段时，使用 `lark-cli` 的用户身份完成读取、匹配、最小更新和回读校验。

#### 处理口径

- 发布单附件先解析 `data.list`，按发布单 `id` 去重；重复粘贴的附件不能重复计数。
- 只统计 `service_list.status = 2` 的成功服务。保留发布单 ID、发布单名称、服务原名、namespace、状态和发布日期，便于审计。
- 发布日期优先从发布单 `name` 中提取 `YYYYMMDD`；名称没有日期时才回退到 `created_at`，并统一按 Asia/Shanghai 转换。
- 如果用户要求生产发布，只纳入发布单 `related_env` / `environment` 为 `prod` 或 `prod-gray` 的订单；服务条目的 `cluster_type` 经常仍为 `prod`，不能用它判断灰度。
- 服务匹配键固定为 `namespace + 归一化服务名`。归一化只删除服务名末尾的 `-gray`（大小写不敏感），例如 `erp-pos-gray` → `erp-pos`；不要删除中间的 `gray`，也不要只按服务名跨 namespace 匹配。
- “使用轻舟发版日期”是单值字段时，默认取该服务成功发布记录中的最早日期，表示首次使用平台发版日期。若用户明确要求最近一次发版，再切换为最大日期并写明口径。

#### 飞书 Base 读取

先使用 `lark-shared` 完成用户授权，再使用 `lark-sheets` / `lark-cli base`。至少需要 Wiki 节点读取、Base 记录读取/更新和字段读取权限；只有在日期选项缺失时才需要字段更新权限。不要把 PAT、token、app secret 或授权输出写入脚本、日志、CSV 或 skill。

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  lark-cli base +url-resolve --url '<wiki-or-base-url>' --format json

LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  lark-cli base +field-list \
    --base-token '<base-token>' --table-id '<table-id>' --format json

LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  lark-cli base +record-list \
    --base-token '<base-token>' --table-id '<table-id>' \
    --field-id '服务名称' --field-id 'NameSpace' \
    --field-id '使用轻舟发版日期' \
    --limit 200 --offset 0 --format json
```

- 先通过 `+field-list` 确认真实字段名、字段类型和字段 ID；“使用轻舟发版日期”通常是 `select` 且 `multiple=false`，不能按普通文本字段处理。
- `+record-list` 必须分页读取到 `has_more=false`，不要只看首页。解析时以返回的 `fields` 顺序映射每行数组，并把 `record_id_list` 与行数据一一对应。
- 写入前保存目标表的完整读取快照；匹配只使用精确 `namespace + normalized_service_name`，未匹配服务不自动创建新行，单独输出待确认清单。

#### 更新决策

先生成计划表，逐条包含 `record_id`、namespace、服务名、当前日期、附件首次成功发布日期、目标日期和决策状态：

| 状态 | 处理 |
|---|---|
| `already_same` | 当前值与附件结果一致，不写入 |
| `preserve_existing_earlier` | 当前已有日期早于附件首次成功发布日期，保留当前值，不覆盖 |
| `will_update` | 当前为空、非日期选项或晚于附件结果，更新为附件首次成功发布日期 |
| `unmatched` | 目标表没有精确匹配行，只报告，不自动新增记录 |
| `duplicate_table_key` | 目标表同一 namespace+服务名有多行，先报告并逐条确认，不依赖模糊匹配 |

只更新用户指定的“使用轻舟发版日期”字段，不顺带修改“已使用轻舟发过版本”、负责人、产线或其它字段。更新前统计各状态数量，并在最终报告中说明附件服务总数、表内匹配数、实际更新数和未匹配数。

#### 单选日期选项

如果目标日期不在当前 `select` 选项中，先读取完整字段定义，再只追加缺失的日期选项；保留字段原有名称、类型、`multiple`、已有选项和颜色。`+field-update` 是完整 PUT 语义，不能只提交新增选项，否则可能覆盖已有选项。

```bash
lark-cli base +field-update \
  --base-token '<base-token>' --table-id '<table-id>' \
  --field-id '<date-field-id>' \
  --json '<完整字段定义 JSON>' --dry-run

lark-cli base +field-update \
  --base-token '<base-token>' --table-id '<table-id>' \
  --field-id '<date-field-id>' \
  --json '<确认后的完整字段定义 JSON>' --yes
```

只有用户已明确要求回写且目标字段、表和新增选项均已核对时，才可使用 `--yes`。补选项只补本次实际需要的日期，不要把附件中所有历史日期无差别加入字段。

#### 批量写入与验证

记录批量更新最多 200 条/次；使用计划表生成 `update_records`，单选字段的 CellValue 使用字符串选项名，不使用猜测的 option ID：

```bash
lark-cli base +record-batch-update \
  --base-token '<base-token>' --table-id '<table-id>' \
  --json '{"update_records":{"rec_x":{"使用轻舟发版日期":"7月28日"}}}' \
  --format json
```

- 先用 `--dry-run` 核对 API、表、字段、record ID 和日期值；再执行实际写入。
- 写入返回成功不等于单元格已落库。写入后重新分页读取目标表，按 `record_id` 比较每条预期值，输出 `missing_records`、`mismatch` 和 `verified`。
- 推荐在 `/private/tmp` 保存 `attachment_release_orders.csv`、`attachment_successful_services.csv`、`table_update_plan.csv` 和 `analysis_summary.json`；临时报告不提交到仓库。
- 最终汇报必须说明：去重后的发布单数、成功归一化服务数、表内匹配数、已更新数、已一致数、保留更早日期数、未匹配数、日期选项新增数，以及回读是否全部通过。

常见错误：把重复附件重复计数、把 `cluster_type` 当成环境、把 `-gray` 中间字符串删除、用服务名跨 namespace 匹配、把单选字段当文本写入、覆盖已有更早日期、只更新第一页记录、写入成功后不回读验证。

可复用脚本：`skills/scripts/sync_qingzhou_release_services_to_feishu.py`

```bash
# 默认只读取并生成 CSV/JSON 计划，不写飞书
python3 skills/scripts/sync_qingzhou_release_services_to_feishu.py \
  --release-order-json /path/to/release-page-1.json \
  --release-order-json /path/to/release-page-2.json \
  --base-url '<feishu-wiki-or-base-url-with-table-query>' \
  --mode both \
  --output-dir /private/tmp/qingzhou_release_feishu_sync_YYYYMMDD

# 仅更新“已使用轻舟发过版本”和“未使用轻舟发版原因”
python3 skills/scripts/sync_qingzhou_release_services_to_feishu.py \
  --release-order-json /path/to/release-orders.json \
  --base-url '<feishu-wiki-or-base-url-with-table-query>' \
  --mode flags --apply \
  --output-dir /private/tmp/qingzhou_release_feishu_flags_YYYYMMDD

# 更新“使用轻舟发版日期”；若缺少日期单选项，显式允许补充选项
python3 skills/scripts/sync_qingzhou_release_services_to_feishu.py \
  --release-order-json /path/to/release-orders.json \
  --base-url '<feishu-wiki-or-base-url-with-table-query>' \
  --mode date --allow-field-option-update --apply \
  --output-dir /private/tmp/qingzhou_release_feishu_dates_YYYYMMDD
```

脚本也支持用 `--table-page` 重放已保存的 `+record-list` JSON 和 `--field-list-json` 重放字段快照，适合先离线审阅计划；只有显式传入 `--apply` 才执行 `record-batch-update`，日期选项更新还必须显式传入 `--allow-field-option-update`。
生产发布口径运行时追加 `--environment prod --environment prod-gray`；不传该参数时表示按附件全量服务处理。

### 场景 7e：汇总飞书推广表进度和卡点服务

当用户要求“总结推广进度”“统计未使用轻舟发版原因”“列出轻舟有卡点服务”时，先通过 `lark-cli base +record-list` 分页读取推广表全量记录，再本地统计，不要只读取当前页面。核心字段为：`服务名称`、`NameSpace`、`服务负责人`、`系统`、`模块`、`是否创建迭代分支`、`已使用轻舟发过版本`、`使用轻舟发版日期`、`是否前端`、`轻舟卡点`、`备注(说明延期，卡点，需要协同事项等)`、`未使用轻舟发版原因`。

统计口径：

- 总服务数 = 表格记录数；迭代分支覆盖率 = `是否创建迭代分支=是` / 总服务数。
- 轻舟发版覆盖率 = `已使用轻舟发过版本=是` / 总服务数。`使用轻舟发版日期` 可能包含计划日期、`待定`、`系统下线` 等状态，不能直接当作实际发版数。
- 同时给出四类交叉统计：已创建分支且已发版、仅创建分支、仅已发版、两项均未完成；“已发版但未标分支”可能是分支已归档，报告中标为待核对，不直接判定数据错误。
- 原因筛选必须使用单选字段的精确值，例如 `未使用轻舟发版原因 == 轻舟有卡点`；不要用服务名或卡点文本反推原因。
- 卡点服务明细至少包含服务名、namespace、负责人、系统、模块、迭代分支状态、是否前端、轻舟卡点和备注。统计卡点分类时保留原始文本，同时可按以下规则归类：共享仓库多产线且上线节奏不同、多个服务共仓且接入收益不高、等待上线后再拉起迭代、单服务多产线开发；无法归类时保留原文。
- 当前表没有 `CMDB产线` 字段时，不能从本表可靠拆分 C/B 端；`是否前端` 不是 C/B 端判断条件。需要 C/B 时另行读取 AppService/CMDB 产线信息，并按 `消费者体验=C端，其它=B端`。

可复用脚本：`skills/scripts/summarize_qingzhou_promotion_table.py`

```bash
# 在线只读汇总；默认筛选“轻舟有卡点”并生成 Markdown/CSV/JSON
python3 skills/scripts/summarize_qingzhou_promotion_table.py \
  --base-url '<feishu-wiki-or-base-url-with-table-query>' \
  --reason '轻舟有卡点' \
  --output-dir /private/tmp/qingzhou_promotion_table_YYYYMMDD

# 使用已保存的 record-list 页面离线重放
python3 skills/scripts/summarize_qingzhou_promotion_table.py \
  --table-page /private/tmp/table_page_0.json \
  --table-page /private/tmp/table_page_200.json \
  --reason '轻舟有卡点' \
  --output-dir /private/tmp/qingzhou_promotion_table_YYYYMMDD
```

脚本只读，不提供写入开关；输出 `promotion_table_report.md`、`promotion_table_summary.json`、`promotion_table_records.csv` 和按原因命名的服务明细 CSV。若要统计全部原因，传空字符串 `--reason ''`；表格分页必须读取到 `has_more=false`。

### 场景 8：CMDB 建模与数据维护

查询模型/属性：

```
query_cmdb_citypes(keyword="...") → 定位 type_id
  → get_cmdb_citype(type_id=...)
  → query_cmdb_citype_attributes(type_id=...)
  → query_cmdb_citype_relations(type_id=...)
```

创建或更新 CI：

```
query_cmdb_citypes(keyword="...") → 获取 type_id
  → query_cmdb_citype_attributes(type_id=...) → 确认 required / unique / default_show / order
  → query_cmdb_cis(type_id=..., query="..." 或 filters_json=...) → 查重
  → create_cmdb_ci(type_id=..., values_json="{...}", exist_policy="reject") 或 update_cmdb_ci(ci_id=..., values_json="{...}")
  → confirm_action
  → get_cmdb_ci(ci_id=...) 验证
```

维护属性和绑定：

```
query_cmdb_attributes(keyword="...") / get_cmdb_attribute(...)
  → create_cmdb_attribute(...) 或 update_cmdb_attribute(...) → confirm_action
  → bind_cmdb_citype_attribute(type_id=..., attribute_id=..., mode="add|update") → confirm_action
  → query_cmdb_citype_attributes(type_id=...) 验证
```

检查服务树/关系视图：

```
query_cmdb_relation_views
get_cmdb_service_tree_full(root_type="..." 或 root_type_id=...)
get_cmdb_service_tree_flat(product_line="...", product="...", environment="...", search="...")
```

落地服务树绑定：

```
get_cmdb_ci(ci_id=应用ID/目标父节点ID) → 校验名称和类型
  → preview_cmdb_service_tree_bindings(bindings_json="[...]") → 查看将新增/删除的关系
  → batch_bind_cmdb_service_tree_applications(bindings_json="[...]") 或 bind_cmdb_service_tree_application(...)
  → confirm_action
  → get_cmdb_service_tree_full(root_type="ProductLine") 或 query_cmdb_ci_relations(ci_id=应用ID, direction="parents") 验证
```

#### 场景 8a：CMDB 产品线/系统组织结构迁移

当用户要求按新组织结构调整 `ProductLine -> System`，按两阶段处理：

```
get_cmdb_service_tree_full(root_type="ProductLine") → 记录现状和目标节点是否存在
query_cmdb_citypes(keyword="ProductLine/System") + query_cmdb_citype_attributes + query_cmdb_citype_relations
query_cmdb_cis(type_id=目标类型, query=目标名称) → 精确查重
create_cmdb_ci(...) → 只生成缺失 ProductLine/System 的 pending action
用户确认 → confirm_action → 记录新 CI ID
replace_cmdb_ci_parent_relations(child_id=SystemID, relation_type_id=contains, parent_ids_json="[TargetProductLineID]", parent_type_names_json="[\"ProductLine\"]")
用户确认 → confirm_action
query_cmdb_ci_relations(ci_id=SystemID, direction="parents") → 验证 DB 关系
get_cmdb_service_tree_full(root_type="ProductLine") → 验证服务树
```

要点：
- 新父节点不存在时，先创建 CI，再迁移关系；不要在不知道新 CI ID 时手写关系。
- `ProductLine/System/Module` 常用属性是 `workitem_id`、`workitem_name`、`display_name`、`parent`、`order`、`disabled`。先查属性元数据，创建时用属性 `name`，`exist_policy="reject"`，`no_attribute_policy="reject"`。
- `workitem_id` 是唯一键，人工创建治理节点时生成新的唯一值，不复用旧节点；`display_name` 用用户可见名称。
- 迁移系统父级用 `replace_cmdb_ci_parent_relations`，并传 `parent_type_names_json=["ProductLine"]`，只替换 ProductLine 父关系，避免误删 System 下的 Module/Application 或 Repository/Pipeline 关系。
- 只操作用户明确确认的产线；例如用户说“其他产线先不操作”，不要清理或迁移 `其他` 下的系统。
- `get_cmdb_service_tree_full` 有 `service_tree:full:<root_type_id>` Redis 缓存，TTL 约 10 分钟。关系查询已正确但服务树仍旧时，看 `updated_at`；等待缓存过期或服务端异步刷新后复查。

#### 场景 8b：给系统补默认模块

当用户要求“为系统增加默认模块”，按 `System -> Module` 落地：

```
query_cmdb_citypes(keyword="Module") + query_cmdb_citype_attributes(type_id=ModuleID)
query_cmdb_citype_relations(type_id=ModuleID) → 确认 System -> Module relation_type
query_cmdb_ci_relations(ci_id=SystemID, direction="children", level=1) → 看现有模块
query_cmdb_cis(type_id=ModuleID, query=候选英文名) → 全局查重
get_cmdb_ci(ci_id=SystemID) → 取父 System 的 workitem_id，填入新 Module 的 parent 属性
create_cmdb_ci(type_id=ModuleID, values_json=..., exist_policy="reject", no_attribute_policy="reject")
用户确认 → confirm_action → 记录 Module CI ID
batch_create_cmdb_ci_relations(relations_json="[{parent_id:SystemID,child_id:ModuleID,relation_type_id:contains}]")
用户确认 → confirm_action
query_cmdb_ci_relations(ci_id=SystemID, direction="children") + get_cmdb_service_tree_full(root_type="ProductLine") → 验证
```

默认模块命名：
- 模块 `workitem_name/display_name` 优先用短英文，不要太长；可用常见缩写，如 `app-base`、`pc-base`、`fe-material`、`data-viz`。
- 如果已生成了不合适的长名称 pending action，不要 confirm；重新查重并生成新的 pending action，让旧 action 自然过期。
- 系统已有业务模块时不要删除；按需求新增默认模块即可。例如已有 `RN组件库/react-native-component`，可保留并新增 `fe-material`。
- 关系创建可用 `batch_create_cmdb_ci_relations`，精确已有关系会幂等返回；仍需用户确认后再执行。

### 场景 9：Hotfix

Phase 1 通过 Web UI 创建 hotfix 分支，后续 resume_workflow / submit_approval / retry_workflow 可通过 MCP 操作。

### 场景 10：迭代分支推进到生产环境（全流水线自动化）

**目标**：将迭代分支从 dev 推进到 prod（dev → test → staging → prod-gray → prod）

**约束**：
- 30 秒轮询间隔
- 自动审批所有符合条件的待审批节点
- 使用 cd_refresh 加速 dev/staging CD（当 CD 超过 1 分钟时）
- 恢复暂停的工作流节点（release_test、release_publish）
- cd_refresh 仅支持 dev/test/staging，不支持 prod-gray/prod

**完整流程**：

```
1. 检查分支状态：
   query_workflow_status(branch_id=...) → 确认 current_environment + current_node

2. dev 环境：
   - check_promotion_gate ✅ 后自动触发 CI
   - CI ✅ → cd 自动触发
   - cd 超过 1 分钟 → cd_refresh(branch_id=..., environment="dev") → confirm_action
   - cd ✅ → release-test 节点（suspended）→ resume_workflow(branch_id=..., environment="dev") → confirm_action
   - release-test ✅ → 自动推进到 test

3. test 环境：
   - retag-image ✅ → cd 触发
   - cd 超过 1 分钟 → cd_refresh(branch_id=..., environment="test") → confirm_action
   - cd ✅ → smoke_test（pending）→ submit_approval(approved=true, node_name="smoke_test") → confirm_action
   - smoke_test ✅ → apply_transfer（pending）→ submit_approval(approved=true, node_name="apply_transfer") → confirm_action
   - apply_transfer ✅ → test_verification（pending）→ submit_approval(approved=true, node_name="test_verification") → confirm_action
   - test_verification ✅ → 自动推进到 staging

4. staging 环境：
   - init-environment-lock ✅ → retag-image ✅ → cd 触发
   - cd 超过 1 分钟 → cd_refresh(branch_id=..., environment="staging") → confirm_action
   - cd ✅ → product_verification（pending）→ submit_approval(approved=true, node_name="product_verification") → confirm_action
   - product_verification ✅ → 自动推进到 prod-gray

5. prod-gray 环境：
   - init-environment-lock ✅ → retag-image ✅ → check-image ✅
   - release-publish（suspended）→ resume_workflow(branch_id=..., environment="prod-gray") → confirm_action
   - release-publish ✅ → cd 触发（注意：cd_refresh 不支持 prod-gray）
   - cd ✅ → product_verification（pending）→ submit_approval(approved=true, node_name="product_verification") → confirm_action
   - product_verification ✅ → 自动推进到 prod

6. prod 环境：
   - init-environment-lock ✅ → retag-image ✅
   - wait-release-approval（pending）→ submit_approval(approved=true, node_name="wait_release_approval") → confirm_action
   - wait-release-approval ✅ → cd 触发（注意：cd_refresh 不支持 prod）
   - cd ✅ → product_verification（pending）→ submit_approval(approved=true, node_name="product_verification") → confirm_action
   - product_verification ✅ → merge-to-prod → completed ✅

7. 轮询策略：
   - 每次操作后等待 30 秒再查询状态
   - 检查 current_environment 和 current_node
   - 发现 pending approval → 立即 submit_approval → confirm_action
   - 发现 suspended → 立即 resume_workflow → confirm_action
   - 发现 cd running 超过 1 分钟 → cd_refresh（仅 dev/test/staging）→ confirm_action

8. 异常处理：
   - 工作流失败 → retry_workflow → confirm_action
   - 状态异常/长时间卡住 → reset_branch_state → 等用户手动确认（禁止自动确认）
   - 发布单审批失败 → 检查 query_release_orders 状态
```

**关键节点类型总结**：
- `Succeeded`：自动推进到下一环境
- `Pending` + `availableActions: ["approve"]`：需要 submit_approval
- `Suspended` + `canResume: true`：需要 resume_workflow
- `Running` + cd 节点：超过 1 分钟可 cd_refresh（仅 dev/test/staging）
- `Running` + 其他节点：继续轮询

**排除规则**：
- 分支 317 不参与自动化推进（用户明确要求排除）

## 分支模型

**类型**：iteration（系统自动创建）→ requirement / defect（开发者创建）→ hotfix（独立流程）

**合并方向**：requirement/defect → iteration → 各环境部署（dev→test→staging→prod）

**唯一性约束**：sprint_id + service_id = 唯一迭代分支。首次创建 requirement/defect 时系统自动创建迭代分支。

**命名模式**：`{prefix}-{ServiceName}-{TypeCode}{ID}-{MMDD}`
- TypeCode：`I`=迭代、`R`=需求、`D`=缺陷/热修复
- prefix：`feature`（迭代/需求/缺陷）、`hotfix`（热修复）

**merge_status 状态机**：`pending → merged → reopened → merged`（循环）

**代码任务关系**：飞书工单 → 代码任务（task_id，按微服务拆分）→ 分支（branch_id）

## 编排工作流

开发者**不直接部署到环境**。代码推送或 reset_branch_state 触发编排工作流：

```
编排工作流（parent）→ dev → test → staging → prod-gray → prod
```

- **suspend 节点**：需 resume_workflow（如 dev 的 release_test）
- **approval 节点**：需 submit_approval（如 test/staging 的人工审批）
- **CD 节点**：ArgoCD 部署，可 cd_refresh 加速（仅 dev/test/staging）
- **失败节点**：可 retry_workflow 重试

## 发布单状态码

0=待发布 1=进行中 2=成功 3=失败 4=超时 5=待审批 6=审批拒绝 7=等待发布 8=预提交 9=结单

## 服务树层级

产品线 → 产品 → 系统/模块 → 应用（以 CMDB `/api/v1/service-tree/full` 为准，ID 可直接用于 create_app_service）

## OwnerSide 枚举值

PC, APP, MINIAPP, POSQT, DW, BACKEND

## CMDB 参考数据

### value_type 枚举（属性字段类型）

`string` / `integer` / `float` / `bool` / `datetime` / `date` / `time` / `json` / `password` / `link` / `list`

`password` 自动加密存储；`link` 渲染为超链接；`list` 表示属性值是数组。

### CITypeAttributeRequest 字段语义

- `is_required`：CI 实例必须填该属性，否则 `create_cmdb_ci` 报错
- `default_show`：列表展示时默认显示该字段
- `order`：列表排序，越小越靠前

### CIRelation direction（query_cmdb_ci_relations）

- `children`：当前 CI 是父，找它的下游（被依赖、被聚合）
- `parents`：当前 CI 是子，找它的上游（依赖谁、归属谁）
- `level`：遍历层数，默认 1，> 1 时返回多级孙子/祖先

### exist_policy / no_attribute_policy（CreateCIRequest）

- `replace`（默认）：unique 字段冲突时覆盖原值
- `reject`：冲突时报错
- `ignore`：冲突时跳过新值，保留原值

### CMDB 内置层级

```
产品线 (ProductLine) → 系统 (System) → 模块 (Module) → 应用 (Application)
```

每层都有独立的 CI 类型 id，可通过 `query_cmdb_citypes` + `query_cmdb_citype_relations` 反查。`get_service_tree` 与 `get_cmdb_service_tree_full(root_type="ProductLine")` 都基于此层级，前者面向 `create_app_service` 工作流，后者面向通用 CMDB 视角。

## 与 efficiency 工作流工具的边界

| 需求 | 用谁 |
|---|---|
| 应用聚合视角（产品线 → 系统 → 模块 → 应用），用于创建服务/部署 | `get_service_tree`（CMDB `/service-tree/full` 同源） |
| 微服务发布 / 分支 / 工单 | `query_app_services` / `create_branch` / `merge_branch` 等 |
| 应用元数据搜索（关键字模糊） | `search_application` |
| 原始 CMDB 视角（任意 CI 类型为根、含 attribute/relation 元数据） | `get_cmdb_service_tree_full` |
| 自定义视图、属性 / 类型管理、CI 实例增删 | `query_cmdb_*` / `create_cmdb_*` 等 CMDB 工具 |

## CMDB 常见坑

1. **`bind_cmdb_citype_attribute` mode=add 重复绑会报错**：先 `query_cmdb_citype_attributes` 检查；要改属性元信息用 `mode=update`。
2. **`create_cmdb_ci` 报 `CI type must have unique attribute`**：CI 类型没设 unique_id；需要在管理界面先指定唯一字段（MCP 当前未暴露该写操作）。
3. **`create_cmdb_relation_view` 报 view name already exists**：name 在用户作用域内必须唯一；改 name 或 `is_public=true` 切换全局视图。
4. **`update_cmdb_attribute` 不能改 name**：name 是属性的内部标识，改了会破坏所有 CI 的属性引用；只能改 alias / description / is_* 这些元信息。
5. **`query_cmdb_cis` 翻页拿不到数据**：默认 page_size=20，超大数据集（>200）建议加 `filters_json`（按 type_id + 关键属性过滤）而不是无脑翻页。
6. **JSON 字符串参数**：`values_json` / `filters_json` / `cr_ids_json` / `option_json` / `bindings_json` / `relations_json` / `parent_ids_json` / `parent_type_names_json` 都是字符串形态的 JSON，不是嵌套对象。务必序列化后再传。
7. **CMDB 写工具被 `permission denied` 拒绝**：服务端 `mcp.cmdb-write-authority-names` 没列出当前用户的角色名；`query_*` / `get_*` / `preview_*` 不受此限制。
8. **`replace_cmdb_ci_parent_relations` 与 `bind_cmdb_service_tree_application` 是 Critical 写操作**：会先删后建，preview 必须人工核对将删除/新增的关系列表，避免误删生产服务树绑定。
