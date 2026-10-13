---
name: acr-image-retag
description: 用于把 ACR 镜像 tag 从一个仓库精确复制到另一个仓库（服务改名、建 mart-* 镜像、跨仓库同步同一个 tag），或排查 retag 后 digest 与源不一致、buildx imagetools 把单 manifest 包成 manifest list、VPC 镜像域名在办公网不可达、跨仓库复制慢/重复上传等问题。
---

# ACR 镜像 tag 精确复制（retag）

## 核心事实（实测）

- **digest 一致性要求**：源 tag 是单 manifest（`application/vnd.docker.distribution.manifest.v2+json`）时，`docker buildx imagetools create -t <dst> <src>` 会把目标写成 **manifest list**，digest 与 media type 都变。实测同一镜像：源 `sha256:b6d33af9…`（manifest.v2）→ 目标 `sha256:5dbfeda4…`（manifest.list.v2，内层才是 `b6d33af9…`）。任何「mart-* 与源 digest 对得上」的校验都会失败。
- **要 digest 相等，只能复刻 manifest 原字节**：先把引用到的 blob 落到目标仓库（跨仓库 `mount`，失败则本机中转），再把源 manifest **原字节 + 原 media type** `PUT` 到目标 tag。
- **VPC 域名在办公网/本机不可达**：`*-vpc.cn-hangzhou.cr.aliyuncs.com` 解析常被本地代理劫持成假 IP（`198.18.x.x`）并 TLS 握手失败。本地操作必须用**公网域名** `xlb-acr-registry.cn-hangzhou.cr.aliyuncs.com`；两域名指向同一企业版实例、同一仓库路径，部署侧继续用 VPC 域名拉取不受影响。
- **同 registry 跨仓库 blob mount 通常被 ACR 拒绝**，会回落到经本机中转：约 1.1 GB/服务、~45 s（≈7–25 MB/s）。同一 base 的多服务之间有层复用，实际总量常远小于「服务数 × 镜像大小」。
- **幂等**：目标已存在同一 digest 时重复执行只是重写 manifest；blob 已存在则跳过上传。目标仓库不存在时首次 push 会创建它。
- **平台自带的 ACR retag 不能跨仓库**：`efficiency/pkg/acr/image_service.go` 的 `CreateRepoTag(NamespaceName, RepoName, FromTag, ToTag)` 只支持同仓库换 tag（另有 `RetagImageDirect` 处理非标准 tag）。跨仓库复制必须用本 skill 的脚本。
- **改名不等于换代码**：跨仓库复制出来的镜像内容与**源仓库该 tag 完全相同**。若目标组应当跑新代码，必须先有对应构建产物，不能靠 retag 顶替。
- **凭据**：`docker login` 成功不等于有 push 权限。用 token 探针看 ACR 实际授予的 scope：请求 `scope=repository:<repo>:push,pull` 后解码 token 的 `.access[].actions`。

## 工具选择

| 工具 | 结论 |
| --- | --- |
| 本 skill 的 `scripts/acr_exact_copy.py` | **推荐**。纯 registry API，不依赖 docker daemon；复制与校验一体；`--dry-run`/`--verify-only` |
| `crane copy` / `skopeo copy` | 可精确复制（保留 media type），但本机通常未安装；装了也可用 |
| `docker buildx imagetools create` | 只做 blob 搬运时可用，但**必须再补一次原字节 manifest `PUT` 归一化**，否则目标被 index 化 |
| `docker pull` + `tag` + `push` | 需要 daemon，且会按当前平台重写 manifest；本 skill 不采用 |

## 输入与凭据

计划文件（`--plan`，两种形态都支持）：

```json
[
  { "src": "zc_xlb/review-b", "dst": "zc_xlb/mart-review-b",
    "tags": ["dev-a0cd2d2f…-2026-08-07-11-21-28", "prod-gray-a0cd2d2f…-2026-08-07-11-21-28"] },
  { "src": "zc_xlb/risk-worker", "dst": "zc_xlb/mart-risk-worker", "tag": "prod-6cb9a58a…" }
]
```

凭据是 **ACR 企业版访问凭证**（用户名形如 `acr-image-user@<实例ID>` + 口令），与集群里同名 pull secret 是同一套；它**不是** `efficiency/config-pri-test.yaml` 里 `acr-list` 的 AK/SK（那是阿里云 OpenAPI 凭据，本工具用不到）。来源二选一：

```bash
export ACR_USER='acr-image-user@<实例ID>' ACR_PW='<口令>'
# 或（文件权限 600，用完即删；每行 KEY=VALUE）
printf 'ACR_USER=%s\nACR_PW=%s\n' "$USER" "$PASS" > /tmp/.acr_env && chmod 600 /tmp/.acr_env
./scripts/acr_exact_copy.py --plan plan.json --password-file /tmp/.acr_env
```

## 标准流程

1. **固定输入**：源仓库、目标仓库、tag 清单（逐条列出，不用通配）；tag 通常来自源组 `repos/gitops/apps/<src-group>/artifacts.libsonnet`，目标仓库名取自目标组 `app.yaml` 的 `image:`。
2. **只读预检**：`--preflight` 会打印源 digest/media type/层数/大小，以及目标仓库是否存在、同名 tag 是否已存在（digest 不同会提示将被覆盖）。
3. **确认授权**：写生产 registry 属于变更操作；把命令与影响（新建/覆盖哪些 tag、预计上传量、源仓不受影响）交用户确认后执行。
4. **dry-run**：`--dry-run` 打印逐条映射（不触网）。
5. **执行**：`--plan` 批量执行；单个 tag 用 `--src/--dst/--tag`。单条失败不会中断其余条目。
6. **校验**：`--verify-only` 回读目标 manifest 原字节算 sha256，与源比对 + media type 相同 + 引用 blob 全部在位。**不要用 PUT/POST 的回显 digest 当结论**（PUT 响应里的 `Docker-Content-Digest` 带 `sha256:` 前缀，直接比较会误判失败）。
7. **清理**：删除临时凭据文件与临时 blob 文件；禁止把口令写进日志、文档或提交。

## 命令

```bash
# 单条
./scripts/acr_exact_copy.py --src zc_xlb/review-b --dst zc_xlb/mart-review-b \
    --tag prod-a0cd2d2f...-2026-08-07-11-21-28

# 批量
./scripts/acr_exact_copy.py --plan plan.json --preflight     # 只读预检
./scripts/acr_exact_copy.py --plan plan.json --dry-run       # 只打印映射
./scripts/acr_exact_copy.py --plan plan.json                 # 复制并校验
./scripts/acr_exact_copy.py --plan plan.json --verify-only   # 只读复核
```

预期输出：预检每条 `[PRE ] <src>:<tag> -> <dst>:<tag> sha256=… type=manifest blobs=N xxMiB dst_exists=…`；执行每条 `[OK ] … sha256=… type=manifest blobs={'uploaded': N}`，结尾 `共处理 N 个 tag，跳过 M，失败 0，本机中转上传 X.XX GiB`。退出码：`0` 全成功、`1` 有失败、`2` 参数或凭据问题。

**已知限制**：源必须是单 manifest（源是 manifest list/index 时该条 `[SKIP]`，不影响其它条目）；源与目标必须同一个 registry 实例。

## 常见错误（红牌）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 目标 digest ≠ 源 digest，media type 变成 `manifest.list.v2` | 用了 `imagetools create` | 用本脚本重做，或对目标 tag 补一次原字节 `PUT` |
| `dial tcp`/`SSL_ERROR_SYSCALL` 连 `*-vpc` 域名 | VPC 域名出了 VPC | 换公网域名 |
| `NAME_UNKNOWN` 查目标仓库 | 目标仓库尚未创建 | 属正常；首次 push 即创建 |
| 校验显示失败但字节其实一致 | 拿 PUT 回显（带 `sha256:` 前缀）比裸 hex | 改用回读 manifest 原字节算 sha256 |
| 上传量远超预期 | 按「服务数 × 镜像大小」估算、忽略层复用 | 以实测/脚本输出的 `blobs=…` 为准 |
| push 403 / `insufficient_scope` | 凭据只有 pull，或用的是共享版 registry 的 secret | 换有 push 权限的凭据，先做 token scope 探针 |

## 安全边界

- 只新增/覆盖目标 tag，**不删除、不修改源仓库任何 tag**；源仓库 tag 数应在操作前后一致。
- 同名目标 tag 已存在且 digest 不同 → 必须先取得明确授权（属覆盖行为）。
- 不在日志、文档、提交里出现口令、token；`--password-file` 用完即删。
- 大批量前先跑 1 个服务做端到端验证（含校验），再放开剩余服务。

## 关联

- 镜像同步完后通常还需要把 tag 写进 GitOps：见 skill `artifacts-libsonnet-update`（`skills/artifacts-libsonnet-update/SKILL.md`）。
- 部署侧拉取用 VPC 域名、推送侧用公网域名的约定见 `agent-square/agent-square-server/docs/dev-quick-build.md`。