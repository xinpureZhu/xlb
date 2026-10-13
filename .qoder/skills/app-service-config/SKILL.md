---
name: app-service-config
description: Read and update 轻舟 (Efficiency platform) app-service configuration over HTTP with a browser cookie — tech stack, CI template type, base branch, module binding, and CodeUp repository members — and diagnose why a service is not ready for branch creation or CI/CD. Use when asked 查服务配置 / 补技术栈 / 改基线分支 / 服务配置不完整 / app_service_configuration_incomplete / missingBindings / 服务管理里那个字段是空的 / 查仓库成员 / 加仓库权限 / 补仓库权限 / 某人没有代码仓库权限 / clone 不下来代码, or when 拉分支 fails with 服务配置不完整.
---

# 应用服务配置读写

## Overview

`app_services` 表保存服务的技术栈、CI 模板、基线分支等元数据，是拉分支和流水线的前置
条件。Efficiency MCP 只提供读（`get_app_service_detail`），改配置只能走 HTTP。

## Contract

```
POST {base}/api/v1/app-services/detail   {"serviceId":153} 或 {"appCode":"dfp-sync"}
POST {base}/api/v1/app-services/list     {"page":1,"pageSize":20,"keyword":"pc-bff","namespace":"goods","lifecycle":""}
POST {base}/api/v1/ci-templates/tech-stacks  {}
POST {base}/api/v1/app-services/update   {"serviceId":153,"techStack":"node"}
POST {base}/api/v1/app-services/codeup/repo-permissions/query   {"serviceId":153,"repositoryId":"7088554"}
POST {base}/api/v1/app-services/codeup/repo-permissions/add     +{"userIds":[...],"roleType":"member"}
POST {base}/api/v1/app-services/codeup/repo-permissions/remove  +{"userIds":[...],"roleType":"member"}
base = https://z.xlbsoft.com/api/efficiency
```

`update` 是**指针式部分更新**：只传要改的字段，未传的字段不动。可改字段：
`name` / `description` / `techStack` / `templateType` / `baseBranch` / `moduleCiId` /
`repositoryCiIds` / `pipelineCiIds`。

- 只传 `techStack` 时，后端自动补该技术栈的默认 `templateType`（node → `node-bff`，
  java → `java-service`），不用手填。
- `repositoryCiIds` / `pipelineCiIds` 是**整体替换** CMDB 绑定，不是追加。除非用户明确
  要求重绑，不要传这两个字段。
- `list` 的响应体在 `data.list`（不是 `data.items`），每项是 `{service, organization,
  microServiceId}` 三段结构，字段在 `item.service` 里。`keyword` 走
  `app_code LIKE ? OR name LIKE ?`，可以用来按前缀捞一组服务。
- `detail` 用 `appCode` 可能命中错服务：同一 appCode 在多个 namespace 下都存在（如
  `goods` / `goods-wanquan` / `goods-wanchi`）。**优先用 `serviceId`**，用 appCode 时必须
  核对返回里的 `namespace`。
- `detail` 的 `repositories[]` / `pipelines[]` 给出仓库权限操作要用的 `repoId` /
  `pipelineId`，`permissions.canEdit` 决定能不能写。

## 拉分支的三个前置条件

`create-from-task` 失败返回 `app_service_configuration_incomplete` 时，
`missingBindings` 只会是这三个值之一或多个：

| missingBindings | 含义 | 修法 |
|---|---|---|
| `technology_stack` | `app_services.tech_stack` 为空 | 本 skill `update --tech-stack` |
| `repository` | Application CI 没有有效 `uses_code` → Repository（要有 `repo_id`/`name`/`http_url`）| 补 CMDB 关系，不是改这张表 |
| `pipeline` | 没有有效 `ci_cd` → Pipeline（要有 `pipeline_id`）| 补 CMDB 关系 / 建流水线 |

只有第一个是 app-service 表字段，后两个属于 CMDB 关系治理，别用 `update` 硬塞。

## 代码仓库成员权限

`repo-permissions/*` 操作的是**服务绑定的那个 CodeUp 仓库**的成员表，走服务维度鉴权
（`canEdit`），不是直接调 CodeUp API。角色映射到 CodeUp accessLevel：

| roleType | accessLevel | CodeUp 角色 |
|---|---|---|
| `owner` / `admin` | 40 | Maintainer |
| `member` | 30 | Developer（研发默认档）|
| `viewer` | 20 | Reporter（只读）|

```bash
S=.qoder/skills/app-service-config/scripts/app_service.py
python3 $S list --keyword agent-square                 # 按前缀捞一组服务
python3 $S repo-perms query --service-id 41872         # 现有成员：role / name / codeupUserId
python3 $S repo-perms add --service-id 41872 --role member --user-id <轻舟 sys_user id>
python3 $S repo-perms add --service-id 41872 --role member --user-id <id> --yes
python3 $S repo-perms remove --service-id 41872 --role member --user-id <codeupUserId> --yes
```

服务只绑一个仓库时省略 `--repository-id`，脚本自己从 `detail` 取；绑多个会报错并列出
候选，必须显式指定。

**`userIds` 在 add 和 remove 里语义不同**，搞混会静默不生效：

- `add`：先按 ObjectID 去 Mongo `sys_users` 查，命中就用该用户的 `nickname`/`email` 去
  CodeUp 匹配；查不到才当成 CodeUp userId 直接用。所以传**轻舟用户 ID**最稳。
- `remove`：不查 `sys_users`，`userIds` 直接当 CodeUp userId 用。要从
  `repo-perms query` 输出里拿第三列。
- `add` 幂等：先 `AddRepositoryMember`，冲突就 fallback `UpdateRepositoryMember`，所以对
  已有成员执行 add 等于**改角色**（member → admin 会提权），不是空操作。
- 用户在 CodeUp 侧无账号或 `sys_users` 缺 `email` 时会匹配失败，返回体
  `results[].user_results[].error` 里有原因，`code=0` 也可能全部失败，必须看
  `failed_count`。

## Workflow

1. **看现状**：

   ```bash
   python3 .qoder/skills/app-service-config/scripts/app_service.py detail --service-id 153
   ```

   关注 `techStack` / `templateType` / `baseBranch` / `canEdit`。`canEdit=false` 就别往下走。
2. **定值，不要猜**。技术栈按优先级取：
   1. 同 namespace、同代码仓库的兄弟服务现有值（最可靠）；
   2. 运行镜像名/仓库特征（如 `pc-bff:*` + `xlb_retail_bff_pc` → node）；
   3. `stacks` 子命令列出的合法枚举 + 由用户确认。

   ```bash
   python3 .qoder/skills/app-service-config/scripts/app_service.py stacks
   python3 .qoder/skills/app-service-config/scripts/app_service.py detail --app-code op-store-product-relation
   ```
3. **dry-run 给用户看 payload**（不带 `--yes`）：

   ```bash
   python3 .qoder/skills/app-service-config/scripts/app_service.py update \
     --service-id 153 --tech-stack node
   ```
4. **确认后加 `--yes`**，脚本会自动回读 detail 验证落库。
5. 若目的是解阻塞，回到原动作重试（拉分支见 `code-task-create` skill）。

## Cookie

从 `/private/tmp/z_cookie.txt` 读取（`Z_COOKIE_FILE` 覆盖路径，`Z_COOKIE` 直接给值）。
`z.xlbsoft.com` 的浏览器会话。**不要打印、复述或提交 cookie 值**。
`Z_BASE_URL` 可切换环境。

## 安全边界

- 这是**生产服务配置写入**：先 dry-run，取得用户确认再 `--yes`，一次只改被点名的字段。
- 技术栈/模板会影响后续 CI 构建行为。值拿不准就问，不要"先填一个能过校验的"。
- 不要用 `update` 绕过 `repository` / `pipeline` 缺失——那是 CMDB 关系问题，硬改只会把
  错误状态藏起来。
- `repositoryCiIds` / `pipelineCiIds` 是替换语义，误传会解绑现有仓库/流水线。
- 遇到 `canEdit=false` 或 403，说明当前身份不是服务负责人，换人操作，不要提权。
- 加仓库权限前先 `repo-perms query` 对比现状：已经在列表里的人不要重复 add（会改角色）。
- 授权对象和角色由用户点名，不要按"同组其他人都有"自行推断补齐；`admin` 只给明确要求的人，
  研发默认 `member`。
- `remove` 是从生产代码仓库摘人，只在用户明确点名时执行，先 query 拿准 CodeUp userId。

## 代码出处

- 路由：`efficiency/router/appservice/appservice.go:24`（detail）、`:47`（update）、
  `:23`（list）、`:31`-`:33`（repo-permissions query/add/remove）、
  `efficiency/router/citemplate/router.go:15`（tech-stacks）
- 更新入参（指针语义 + 自定义 UnmarshalJSON）：
  `efficiency/model/appservice/models.go:300`
- 仓库权限入参：`efficiency/model/appservice/codeup_permission.go:4`（query）、`:16`（add/remove）
- 仓库权限编排（sys_users 解析、add/remove 语义差异）：
  `efficiency/pkg/appservice/codeup_permission.go:21`、`:48`、`:118`、`:173`
- roleType → accessLevel 映射：`efficiency/pkg/cmdb/service/repo_permission_service.go:44`、`:58`
- add 幂等 fallback（Add 失败转 Update）：
  `efficiency/pkg/cmdb/service/repo_permission_service.go:361`
- list keyword 过滤：`efficiency/pkg/appservice/service.go:454`
- 拉分支前置校验（tech_stack / uses_code / ci_cd）：
  `efficiency/pkg/workitem/service/branch_creation_readiness.go:124`、`:136`、`:205`
- missingBindings 常量：`efficiency/model/workitem/response/branch_creation_event.go:39`
- 技术栈与默认模板：`efficiency/pkg/citemplate/builtin.go:122`
- 外部地址规则：`docs/17-efficiency-api-manual.html:81`
