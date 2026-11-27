# AGENTS.md

XLB 零售平台代码仓的 Agent 指引。本文件会被 CLI 作为常驻 memory 自动加载，请保持精简。

## 云资源与生产环境：只读

**默认严禁执行任何写操作。** 已配置的云 CLI 凭据**可能持有写权限**，其能力边界不可假定为只读。因此这条约束必须由流程保证，不能依赖权限边界。唯一的放宽条件见第 7 条的**单次授权**（评估 → 明确授权 → 限范围执行）；未取得该授权时，一切写操作一律禁止。

1. **只允许只读动词**：`Describe*` / `List*` / `Get*` / `Query*` / `Search*`。
2. **禁止一切变更动词**：`Create*` / `Delete*` / `Modify*` / `Update*` / `Restart*` / `Reboot*` / `Stop*` / `Start*` / `Release*` / `Reset*` / `Switch*` / `Migrate*` / `Scale*` / `Attach*` / `Detach*` / `Set*`。
3. **同样适用于其他通道**：`kubectl apply/edit/delete/scale/rollout`、`pulumi up/destroy/refresh`、`mongosh`/`redis-cli` 的写命令、数据库 DML/DDL。
   - 特别注意：Pulumi state 与线上已存在漂移（MongoDB 分片规格被手工扩容过），`pulumi up` 可能把生产资源缩回，属高危。
4. **需要写操作时**：只输出命令并说明影响，交由人执行（提示用户用 `!` 前缀），不得自行运行。**唯一例外是第 7 条的单次授权，且必须先走评估 → 授权流程。**
5. **权限探测**：只能用官方 `--DryRun`，或把目标指向**不存在的资源 ID**；严禁拿真实资源试探。
6. **凭据**：不得打印或写入 AK/SK、STS Token、`oauth_*`、数据库密码；只描述「存在该凭据」。
7. **单次授权例外**：写操作**仅在**同时满足以下三条时可执行——
   - **(a) 先评估**：我先把评估结论交你确认，内容至少含：目标资源与环境、将执行的确切命令、预期影响、回滚/失败处置；
   - **(b) 明确授权**：你对该评估明确回复**认可并授权本次写入**；
   - **(c) 严格限范围**：实际执行只限该次评估覆盖的命令与资源，不得顺手扩大到同类资源、其他环境或「顺便修一下」的项。

   授权是**单次且不可推断**的：沉默、模糊表态（「看着办」「你决定」「应该可以」）**不构成授权**；不跨命令、不跨资源、不跨会话累积；授权项执行完毕、执行失败、或评估对象/环境发生变化后即失效，后续写操作必须重新走 (a)→(b)。执行前先复述将运行的确切命令，执行后保留原始输出供复核。
   本例外**不适用于**下方 GitOps 的「禁止提交与推送」及「发布一律交人执行」。

## GitOps 工作副本 `repos/gitops/apps/`：可改、不可提交推送

`repos/gitops/apps/` 是 `gitops/apps.git` 的本地工作副本（当前分支 `test`）。

1. **允许改**：`<group>/artifacts.libsonnet`、`<group>/*.app.yaml`、`templates/*` 等文件可自由修改；可运行 `repos/gitops/preview.sh`、`repos/gitops/apps/eval.sh` 做渲染预检（产物 `_.jsonnet` 已在 `.gitignore`）。
2. **禁止提交与推送**：不得执行 `git commit` / `git push`（含 `--force`、`--tags`）；亦不得用 `merge` / `rebase` / `reset` / `checkout` 去制造或改写待推送的提交。
3. **禁止改走别的通道把本机改动送出去**：CodeUp OpenAPI 文件写入、artifacts-webhook、建 MR 都只接受**约定好的正式变更**，不得用来上传本机工作副本的改动。
4. **只读操作不受限**：`git status` / `diff` / `log` / `show` / `fetch` 正常使用。
5. **发布一律交人执行**：真实 GitOps 变更走 artifacts-webhook 或 `artifacts-<group> → prod` 发布 MR，只输出命令 + 影响说明，提示用户用 `!` 前缀自行运行。改动留在工作区即视为「未发布」。
6. **本地改动不必清理**：不要自动 `checkout --` / `stash` 丢弃，保留供人工比对去留。

## CodeUp 源码：按需浅克隆到 `repos/`、只读

**所有代码仓工作副本统一放在根目录 `repos/` 下**（一个仓一个子目录，目录名 = 仓库名；GitOps 工作副本在 `repos/gitops/apps/`，见上一节）。根目录只保留非代码仓资产：`docs/`、`skills/`、`.qoder/`、`tmp-images/` 等；不要在根目录再散放代码仓或 GitOps 工作副本。

需要阅读/检索**任何 CodeUp 仓库**（组织 `5f9a23913a5188f27f3f344b`；已见组：`quality/`、`bff/`、`gitops/`、`marketing/`、`smartisan/`、`fe-ai/`、`datawarehouse/` 等）的代码时，按本约定执行：

1. **克隆位置**：本工作区根目录下的 `repos/`，目录名 = 仓库名，目录已存在时先确认是否同一仓库，不要覆盖他人工作副本。
2. **克隆命令**（浅克隆、只取目标分支；默认分支不是 `prod` 时以 `git ls-remote --heads` 为准；在本工作区根目录执行）：
   `git clone --depth 1 --single-branch --branch prod ssh://git@codeup.aliyun.com/5f9a23913a5188f27f3f344b/<group>/<repo>.git repos/<repo>`
3. **仓库发现**：效能平台 MCP `query_codeup_repo_options(mode="bind", search="<关键字>")` 列仓库；分支/权限用 `git ls-remote --heads ssh://git@codeup.aliyun.com/5f9a23913a5188f27f3f344b/<group>/<repo>.git` 探。
4. **只读约束**：不修改、不 `commit`、不 `push`、不装依赖、不跑构建；仅用于阅读与 grep。确需改动这些仓库时走各自正常的分支/发布流程，不在工作区直接改。
5. **权限边界**：无读权限的库返回「找不到代码库，请确认是否有权限」。

## 代码变更：一律 TDD

所有代码变更（新功能、修 bug、重构、性能优化）都按 TDD 顺序推进，不可颠倒：

1. **先写会失败的测试**：用最小用例把目标行为/缺陷钉住，必须**亲眼看到它失败**（红），并确认失败原因就是预期的那条（不是编译错误、不是环境缺失）。
2. **再写最小实现**：只让这条测试变绿，不顺手扩展范围。
3. **绿了才重构**：结构清理期间每步都要保持测试通过。
4. **提交前跑相关测试**：按仓库约定跑（`cargo test` / `npm test` / `pytest` 等），红着不提交。

细则：

- 测试要能捕捉**真实消费方会踩到的缺陷**；禁止「断言函数被调用过」「断言源码文本」「断言默认值」这类永远不会失败的测试。
- 修 bug 必须先有能复现该 bug 的测试；没有复现用例就不算修完。
- 纯配置、文档、注释、Dockerfile 等无运行时行为的改动不需要测试，但要在提交信息里写明为何不适用。
- 测试覆盖不到的部分（外部服务、k8s 行为、构建产物）必须给出可执行的验证命令与期望输出，并保留原始输出。
- 严禁为了通过而缩小断言、跳过用例（`#[ignore]` / `test.skip`）、或改测试去迁就实现。

## 代码风格：不写注释

- 交付的代码里**不写注释**：不写分节横幅（`# ---- xxx ----`）、不复述代码在做什么、不写作者/日期/变更记录、不写"为什么改这里"的说明。
- 唯一例外：不解释就会被人误改的外部契约（如跨版本兼容的字段格式、第三方协议约束），允许一行，且只写"约束是什么"。

## 总结类文档规范

在本仓生成/更新任何 **总结类文档**（架构总览、服务清单、业务能力、技术栈、部署拓扑、集成分析、专题总结等）时，必须遵守：

1. **统一存放**：所有总结类文档放在仓库根目录的 `docs/` 目录。
2. **一律 HTML + 自托管**：交付格式为 `.html`；样式引用 `docs/assets/style.css`，图表引用 `docs/assets/mermaid.min.js` + `docs/assets/diagram-init.js`。**不引用外部 CDN**，保证离线可开。
3. **必须有架构图**：**每篇文档至少一张架构图**（优先 Mermaid `pre.mermaid`，或规范提供的纯 CSS 泳道图 `.arch`）。纯文字/表格不合规。
4. **不泄露密钥**：严禁把 `.env`/配置中的密码、密钥、Token、Cookie 等写入文档，只描述「存在该依赖」。
5. **完整格式规范见** [`docs/文档格式规范.md`](docs/文档格式规范.md) —— 目录结构、页面骨架模板、架构图规范（§7）、组件 class 约定、内容规范与更新 checklist 均以该文件为准。编写前请先阅读它。

> 说明：`docs/文档格式规范.md` 是规范正文（Markdown），其余 `docs/*.html` 是受该规范约束的产物。

## 仓库级 Skill

根目录 `skills/` 放置跨子项目复用的操作型 skill（每个 skill 一个目录，含 `SKILL.md` 与可选 `scripts/`）。涉及下列操作时**先读对应 SKILL.md 再动手**：

- [`skills/acr-image-retag/SKILL.md`](skills/acr-image-retag/SKILL.md)：ACR 镜像 tag 跨仓库精确复制（retag / 建 `mart-*` 镜像），要求目标 digest 与源一致时的做法与校验口径。**retag 完成后必须用 artifacts-webhook API 更新目标组 `artifacts.libsonnet` 记录**（写 GitOps 属发布动作，命令交人执行）。
- [`skills/artifacts-libsonnet-update/SKILL.md`](skills/artifacts-libsonnet-update/SKILL.md)：写入/更新 GitOps `<group>/artifacts.libsonnet`（新组补镜像、按环境发布），含 artifacts-webhook 契约、分支语义与发布前的中间件耦合检查。
- [`skills/cloudflare-docs-deploy/SKILL.md`](skills/cloudflare-docs-deploy/SKILL.md)：把根目录 `docs/` 静态文档站部署到 Cloudflare Workers（`xlb-docs`，带 Basic Auth 访问口令）。**docs/ 更新后必须跑 `skills/cloudflare-docs-deploy/scripts/deploy_docs.sh` 同步部署**，否则线上仍是旧版。
- [`skills/call-sentinel/SKILL.md`](skills/call-sentinel/SKILL.md)：用户说 `call sentinel`（或「问问 Sentinel / 让 Sentinel 查一下」）时，以本人飞书身份在群里 @Sentinel 提问并取回结论卡片。

索引与通用约定见 [`skills/README.md`](skills/README.md)；`repos/efficiency/skills/` 是效能平台专项 skill，与本目录互不重叠。

## 备注

- 各子项目（`repos/oms-plateform/`、`repos/xlb_retail_bff_pc/`、`repos/xlb_retail_bff_pos/`）下已有各自的 `agents.md`（GitNexus 代码智能配置），与本文件互不冲突，按目录就近生效。
