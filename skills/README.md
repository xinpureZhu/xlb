# XLB 仓库级 skill

跨子项目复用的操作型 skill。与 `efficiency/skills/`（效能平台专项）互不重叠：这里放**面向 ACR/GitOps 等基础设施的确定性操作**。

| Skill | 何时使用 | 入口 |
| --- | --- | --- |
| `acr-image-retag` | 把 ACR 镜像 tag 跨仓库精确复制（服务改名、建 `mart-*` 镜像、同 tag 跨仓同步）；或排查 retag 后 digest 与源不一致、`imagetools` 把单 manifest 包成 manifest list、VPC 镜像域名连不通、跨仓库复制慢 | [acr-image-retag/SKILL.md](acr-image-retag/SKILL.md) + `scripts/acr_exact_copy.py` |
| `artifacts-libsonnet-update` | 写入/更新某 GitOps 组的 `artifacts.libsonnet`（新组补镜像、按环境发 prod/prod-gray、泳道条目增删）；或排查写入后 ArgoCD 不渲染、队列 `done` 但远端未变、`artifacts-<group>` 与 `prod` 分支不一致 | [artifacts-libsonnet-update/SKILL.md](artifacts-libsonnet-update/SKILL.md) + `scripts/publish_artifact.py` |
| `cloudflare-docs-deploy` | 把根目录 `docs/` 的静态文档站部署到 Cloudflare Workers（`xlb-docs`，Basic Auth 访问口令）；**docs/ 更新后同步部署**；或排查 wrangler 未登录、缺 `DOCS_PASSWORD` secret、本地 `wrangler dev` 起不来、站点仍是旧内容 | [cloudflare-docs-deploy/SKILL.md](cloudflare-docs-deploy/SKILL.md) + `scripts/deploy_docs.sh` |

前两个 skill 有先后关系：镜像先在 ACR 就位（`acr-image-retag`），再把 tag 写进 GitOps（`artifacts-libsonnet-update`）。`cloudflare-docs-deploy` 与它们无关。

## 通用约定

- 生产写操作（ACR push、GitOps 分支写入）都属变更操作：先只读预检 → 取得明确授权 → 执行 → 回读校验。
- 两个脚本都提供只读模式（`--preflight` / `--verify-only` / `--dry-run`），优先用它们取证。
- 凭据只从环境变量或 600 权限的临时文件读取，禁止写进仓库、日志或文档。