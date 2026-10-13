---
name: codeup-repo-permissions
description: 给 CodeUp 代码仓增删查成员权限（走轻舟 CMDB 维度接口 /api/v1/codeup/repo-permissions/*，只需登录态，不经 app-service 归属校验）。适用于"给某个代码仓加/删/查成员或权限""某人要能推这个仓的代码""改某人在仓库里的角色（owner/admin/member/viewer）"，尤其当该仓库未绑定任何轻舟应用服务、无法用 app-service-config 的 repo-perms 时。用 repo_id 或 CMDB Repository CI id 直接定位仓库。
---

# CodeUp 代码仓成员权限（CMDB 维度）

## Overview

按 `repo_id`（或 CMDB Repository CI id）直接给 CodeUp 仓库增删查成员权限。后端路由
`/api/v1/codeup/repo-permissions/{query,add,remove}` **只校验登录态**，不做 app-service
归属/`canEdit` 校验，所以对**未绑定应用服务的游离仓库**也能操作。

先判断走哪条路：

- **仓库已绑定应用服务、且你是服务负责人**（`canEdit=true`）→ 用 `app-service-config`
  skill 的 `repo-perms`（服务维度鉴权，更贴合权责）。
- **仓库未绑服务，或需要按 `repo_id` 直接操作** → 用本 skill。

## Contract

```
POST {base}/api/v1/codeup/repo-permissions/query    {"repo_ids":["7293135"]} 或 {"cmdb_ids":[6409]}
POST {base}/api/v1/codeup/repo-permissions/add       {"rules":[{...}]}
POST {base}/api/v1/codeup/repo-permissions/remove    {"rules":[{...}]}
base = https://z.xlbsoft.com/api/efficiency
成功判据：顶层 code == 200；写操作还要逐条看 data.results[].failed_count
```

`rule` 字段（`RepoPermissionRule`）：

| 字段 | 说明 |
|---|---|
| `repo_ids` | CodeUp 数字仓库 ID 列表，**verbatim** 透传给 CodeUp OpenAPI |
| `cmdb_ids` | CMDB Repository CI id 列表，后端按 CI 关系解析出 repo_id |
| `user_ids` | 用户 ID（`add`/`remove` 语义不同，见下） |
| `user_names` / `user_emails` | 按 CodeUp 用户名/显示名 / 邮箱匹配 |
| `role_type` | `owner`/`admin`/`member`/`viewer` |

`query` 请求只认 `cmdb_ids` / `repo_ids`，两者不能同时为空。

## role_type → CodeUp accessLevel

| role_type | accessLevel | CodeUp 角色 |
|---|---|---|
| `owner` / `admin` | 40 | Maintainer |
| `member` | 30 | Developer（研发默认档，可推代码）|
| `viewer` | 20 | Reporter（只读）|

## 脚本

```bash
S=.qoder/skills/codeup-repo-permissions/scripts/repo_perms.py
python3 $S query  --repo-id 7293135                              # 现有成员：role/name/codeup_user_id/username
python3 $S add    --repo-id 7293135 --user-id <轻舟 sys_user id> --role member          # dry-run
python3 $S add    --repo-id 7293135 --user-id <轻舟 sys_user id> --role member --yes     # 落库
python3 $S remove --repo-id 7293135 --user-id <CodeUp userId> --yes                     # CodeUp userId 取自 query 第三列
python3 $S query  --cmdb-id 6409                                 # 也可用 CMDB Repository CI id
```

`--repo-id` / `--user-id` / `--user-name` / `--user-email` 均可重复传多个；写操作默认
dry-run，加 `--yes` 才真正提交，之后建议再 `query` 回读核对。

## 关键语义（搞混会静默不生效）

- **`user_ids` 在 add 和 remove 里含义不同**：
  - `add`：值若是合法 Mongo ObjectID hex，先按它去 `sys_users` 查，命中就用该用户
    `email`/`nickname` 去 CodeUp 匹配；不是 ObjectID 才当成 **CodeUp userId 直接用**。
    → 传**轻舟用户 ID 最稳**。
  - `remove`：**不查 `sys_users`**，`user_ids` 直接当 CodeUp userId 用。必须从
    `query` 输出的第三列（`user_id`）取。
- **`add` 幂等 = 改角色**：先 `AddRepositoryMember`，成员已存在则 fallback
  `UpdateRepositoryMember`。所以对已有成员执行 add 会**改角色**（member→admin 提权、
  admin→member 降权），不是空操作。执行前先 `query` 核对现状。
- **失败可能藏在 code=200 里**：顶层 `code=200` 也可能全部失败。必须看
  `data.results[].failed_count` 和 `user_results[].error`（用户在 CodeUp 侧无账号、
  `sys_users` 缺 email 等会匹配失败）。脚本已按 failed_count 返回非零退出码。

## 如何拿到 repo_id

`repo_id` 是 CodeUp 数字仓库 ID，git URL（如 `.../smartisan/retail-performace-sse.git`）
里**没有**。解析方式：

1. **efficiency MCP**（首选，若可用）：`query_cmdb_cis` 按仓库名搜 Repository CI
   （`type_id=8`），返回 `values.repo_id` 与 CI `id`（即 `--cmdb-id`）。
2. **本脚本 `--cmdb-id`**：拿到 CMDB Repository CI id 后直接传，让后端解析 repo_id。
3. CodeUp 仓库设置页 / URL 也能看到数字 repo ID。

## Workflow

1. **定位仓库**：拿到 `repo_id`（或 `cmdb_id`）。
2. **看现状**：`query`，确认目标用户是否已在列表、当前角色是什么。
3. **定角色**：研发默认 `member`；`admin` 只给用户明确点名的人，不按"同组都有"推断补齐。
4. **dry-run 给用户看 payload**（不带 `--yes`）。
5. **确认后加 `--yes`**，再 `query` 回读核对角色已落库。

## Cookie

从 `/private/tmp/z_cookie.txt` 读取（`Z_COOKIE_FILE` 覆盖路径，`Z_COOKIE` 直接给值），
`z.xlbsoft.com` 浏览器会话。`Z_BASE_URL` 可切环境。**不要打印、复述或提交 cookie 值。**

## 安全边界

- 这是**对生产代码仓库写权限**：先 `query` + dry-run，取得用户确认再 `--yes`。
- 授权对象和角色由用户点名，不要自行推断补齐；`admin`/`owner`（Maintainer）只给明确要求的人。
- `add` 对已有成员是改角色，动手前务必 `query` 核对，避免误把 admin 降成 member。
- `remove` 是从生产仓库摘人，只在用户明确点名时执行，先 `query` 拿准 CodeUp userId。
- 本接口不经 app-service `canEdit`，权责边界更松——更要靠"用户明确点名 + 回读核对"兜底，
  不要拿它绕过本应由服务负责人处理的授权。
- 不打印任何 AK/SK、Token、Cookie、用户敏感信息。

## 代码出处

- 路由（仅 `AuthMiddleware`，无 Casbin/canEdit）：`efficiency/router/cmdb/router.go:689`
  （`codeupGroup`）、`:707` add、`:708` remove、`:709` query
- Handler：`efficiency/api/v1/cmdb/repo_permission_handler.go:26`（add）、`:53`（remove）、`:80`（query）
- 入参结构：`efficiency/model/cmdb/repo_permission.go:4`（`RepoPermissionRule`）、
  `:15`（`BatchRepoPermissionRequest`）、`:20`（`QueryRepoPermissionRequest`）
- 编排（无 app-service 依赖；repo_id 透传 / cmdb_id 解析 / add 幂等 fallback / user_ids 解析）：
  `efficiency/pkg/cmdb/service/repo_permission_service.go:72`（AddPermissions）、`:87`（Remove）、
  `:102`（Query）、`:357`（applyUserRepoPermission）、`:361`-`:377`（Add→Update fallback）、
  `:410`（getRepoIDsByCMDBIDs）、`:44`（roleType→accessLevel）
- 对比：app-service 维度（走 `canEdit`）在 `efficiency/router/appservice/appservice.go:31`-`:33`
  与 `efficiency/pkg/appservice/codeup_permission.go:61`
