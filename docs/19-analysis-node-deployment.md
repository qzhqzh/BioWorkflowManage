# Analysis Node 独立交付与第三方部署

## 定位与边界

Analysis Node 是 BioWorkflowManage 面向 MES、运营平台和第三方服务器的独立分析组件。上游系统
只通过 Integration API 提交稳定 `analysis_product`，运行结果通过查询 API、Artifact Export 和
签名 Webhook 交付；双方不共享数据库，也不允许上游直接指定任意内部 WorkflowVersion。

首个交付基线为 `linux/amd64`，支持两组正交 profile：

| 维度 | Profile | 用途 |
| --- | --- | --- |
| 界面 | `headless` | 只暴露 API，不创建 frontend 容器；推荐嵌入第三方平台 |
| 界面 | `console` | 同时提供管理控制台和 API |
| 运行时 | `isolated-runtime` | Docker-in-Docker，任务与主机 Docker 隔离；默认推荐 |
| 运行时 | `host-runtime` | 复用主机 Docker Socket；性能更直接但权限边界更大 |

安装工具要求恰好选择一个界面模式和一个运行时模式。产品 Compose 与开发 Compose 独立，所有
镜像使用本地固定 tag、`pull_policy: never` 和完整 `images.lock.json`；离线启动不会隐式拉取镜像。

本版本不提供 Kubernetes Operator，不自动删除客户数据、Docker volume 或备份，不把任意第三方
WDL 当作可信交付内容。Analysis Node 只执行部署者显式发布、契约快照未漂移且已有可信包证明的
Analysis Product；历史上未绑定 Analysis Product 的任务也不能借 retry 绕过该边界。

## 交付物与信任链

正式 Release 包含：

- `analysis-node-<version>-linux-amd64.tar.gz`、SHA-256 和 Sigstore bundle；
- 后端、可选控制台、PostgreSQL、gateway、DIND 和可信 smoke task 的离线 `images.tar`；
- `images.lock.json`，记录每个角色的上游 digest、本地 tag 和加载后 Image ID；
- 每个镜像的 SPDX JSON SBOM；
- 内部 `SHA256SUMS` 及其 Sigstore bundle；
- Compose、配置 Schema、安装/诊断 CLI 和操作文档。

发布工作流只从 `analysis-node-v*` tag 创建公开 GitHub Release。Workflow Dispatch 可生成候选制品，
但不会发布 Release。实际创建 tag/Release 属于独立发布动作，不是普通 PR 合并的一部分。
发布工作流在上传制品前会从最终 tar 包解压到干净目录，删除构建阶段的本地交付 tag，再依次执行
`verify-bundle → init → preflight → load-images → migrate → up → doctor --smoke`，随后做一次真实
backup/restore 和第二次 smoke；任何一步失败都不会产出可发布 Release。

下载后先在未解包状态验证制品。`VERSION` 替换为实际版本：

```bash
VERSION=1.1.0
ARCHIVE="analysis-node-${VERSION}-linux-amd64.tar.gz"

cosign verify-blob --offline \
  --bundle "${ARCHIVE}.sigstore.json" \
  --certificate-identity-regexp \
  '^https://github\.com/qzhqzh/BioWorkflowManage/\.github/workflows/release-analysis-node\.yml@refs/tags/analysis-node-v[0-9].*$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$ARCHIVE"
sha256sum -c "${ARCHIVE}.sha256"
tar -xzf "$ARCHIVE"
cd "analysis-node-${VERSION}"
```

使用近期受支持版本的 Cosign。Sigstore bundle 包含签名、证书和透明日志证明，`--offline` 禁止验证
过程临时访问网络。解包后的 `bin/analysis-node verify-bundle` 会再次验证发布者身份、内部清单、
每个文件摘要与镜像锁，任一项失败都禁止安装。

## 全离线安装

### 1. 主机前置条件

- Linux amd64；Docker Engine 可由当前管理员访问；Docker Compose `>= 2.27`；
- Python 3 和近期 Cosign；
- 默认至少 8 GiB 可用内存和 50 GiB 可用数据盘，真实生信任务应按 WDL 峰值另行容量评估；
- 数据、备份、原始数据/NAS、数据库资源和密钥目录使用绝对路径；
- 安装前确认所选监听端口、DIND 子网和企业网络不冲突。

### 2. 配置与预检

```bash
cp .env.example .env
chmod 0600 .env

# 将输出分别填入 POSTGRES_PASSWORD、DJANGO_SECRET_KEY、WEBHOOK_SIGNING_KEY；不要复用
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32

vi .env
bin/analysis-node validate-config
bin/analysis-node verify-bundle
bin/analysis-node init
bin/analysis-node preflight
```

`.env` 至少要确认：

- `ANALYSIS_NODE_MODE=headless|console`；
- `ANALYSIS_NODE_RUNTIME=isolated|host`；
- `ANALYSIS_NODE_PUBLIC_BASE_URL`、可信域名、CORS/CSRF 来源和可信代理层数；
- 所有数据、备份、工作区、NAS 和 secret 绝对路径；
- 两个 miniwdl 子网与现有 Docker/VPN/机房网段不重叠；
- `INTEGRATION_REQUIRE_ANALYSIS_PRODUCT=1` 与
  `INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE=1` 保持开启。

`DJANGO_ALLOWED_HOSTS` 中应保留 `localhost,127.0.0.1` 供容器内 readiness 探针使用，并加入真实
外部域名；这两个 loopback 名称不会使服务对外监听，外部暴露仍由 Bind Address 和防火墙决定。
`DJANGO_TRUSTED_PROXY_COUNT` 必须包含产品 gateway；直接访问 gateway 时为 1，前面另有一层 TLS
反向代理时通常为 2，必须按真实可信链路填写。

`init` 只创建目录、收紧权限并写入初始化标记，重复执行不会删除或清空已有文件。备份目录必须与
数据目录隔离；对象存储和 Artifact Export 凭据目录不能位于数据根目录中。

`preflight` 会阻断以下问题：可用内存或磁盘不足、受管目录缺失/为符号链接、私有目录权限过宽、
`MINIWDL_UID:MINIWDL_GID` 无法读取输入或写入运行目录、host runtime 的 Docker Socket GID
不匹配，以及隔离子网和已有 Docker 网络重叠。原始数据和数据库放在 NAS 时，应先挂载到最终
绝对路径再执行 `init/preflight`，不能只检查一个尚未挂载的空目录。

`host-runtime` 必须把四个 `*_EXECUTION_ROOT` 设置成对应的绝对 `*_HOST_PATH`，确保 worker 和任务
容器看到完全相同的路径。`isolated-runtime` 则固定使用 `/analysis/*` 容器路径。

### 3. 加载、迁移和真实冒烟

```bash
bin/analysis-node load-images
bin/analysis-node migrate
bin/analysis-node up
bin/analysis-node doctor --smoke --timeout 600
```

`load-images` 先校验签名和摘要，再加载 `images.tar`，逐一比对 Image ID。隔离模式还会把可信
smoke task 镜像加载到 DIND 内部 Engine。`doctor --smoke` 会检查服务、镜像、readiness、Headless
不含 frontend、隔离 Engine，并通过临时 Service Token 经 API 提交 `analysis-node-smoke@1.0.0`，
读取一份临时受管 rawdata 输入，轮询到结果完整后立即吊销 Token 并删除临时输入。

`migrate`、`up`、`backup` 和 `restore` 每次执行前都会重新验证发布者签名、内部 SHA-256、镜像锁
和本机 Image ID。这样即使本地 tag 后来被替换，也不能借运维命令执行或迁移数据库。

停止服务使用：

```bash
bin/analysis-node down
```

该命令不传 `-v`，不会删除 bind mount、Docker volume、DIND 镜像缓存或客户数据。

## 网络、TLS 与代理

默认只监听 `127.0.0.1`，由客户已有的 Nginx、Ingress 或负载均衡器终止 TLS。只在确有防火墙和
访问控制时改为 `0.0.0.0`。外层代理必须保留 `Host`、`X-Forwarded-For` 和
`X-Forwarded-Proto=https`。产品 gateway 只接受精确的 `https` 值并传给 backend，其他值回退到
gateway 自身 scheme；`DJANGO_TRUSTED_PROXY_COUNT` 必须等于 backend 前的真实可信代理层数。

Analysis Node 不直接管理客户 TLS 私钥。生产环境保持 Secure Cookie，精确配置 Allowed Hosts、
CSRF 和 CORS，且不要使用 `*`。Headless 模式同样需要认证；健康与 readiness 端点除外。

DIND 服务需要 `privileged`，但只连接独立 control/egress 网络，不挂主机 Docker Socket。
`host-runtime` 会把 `/var/run/docker.sock` 暴露给 worker，等价于较高主机权限，仅适用于客户明确
接受该边界、Docker GID 正确且主机上没有不受信任务的场景。

## 数据、NAS、S3 与容量

- 原始数据和参考数据库默认只读挂载，可指向 NAS/NFS 的绝对路径；运行、输入暂存、缓存和导出目录
  必须可由 `MINIWDL_UID:MINIWDL_GID` 写入。
- S3/MinIO profile 放入对象存储 secret 目录，按 Service Account 限定 bucket/prefix，只授予所需
  的只读/version 权限；任务请求必须固定 VersionId/ETag、size 和 SHA-256。
- 结果交付 profile 放入 Artifact Export secret 目录，凭据不进入 API payload、WDL、日志或 `.env`。
- PostgreSQL、运行目录、输入暂存、DIND 镜像缓存和 Artifact Export 分别监控容量。生产阈值应按
  并发数、最大输入、输出快照和结果保留期计算，不能只依赖安装时 50 GiB 基线。
- GC 与结果清理只通过 `maintenance` profile 的 dry-run 优先命令显式执行，规则见
  [第三方分析投递 API 与 MCP](14-integration-api-and-mcp.md)。

## 备份、升级与回滚演练

数据库备份：

```bash
bin/analysis-node backup
```

命令输出 PostgreSQL custom-format dump、独立 JSON 清单和 SHA-256，权限为 `0600`。它不复制可能
很大的 NAS 原始数据或运行目录；生产备份还必须由存储系统对以下内容做一致性快照：

- `ANALYSIS_NODE_DATA_ROOT`（尤其运行、暂存和 DIND 状态）；
- Artifact Export 目标；
- 对象存储/NAS 中由客户负责保留的输入和结果。

建议每个版本在隔离演练机执行一次完整流程：恢复最近备份、加载相同版本包、`migrate`、`up`、
`doctor --smoke`，并记录恢复时间和数据抽样摘要。只“生成了备份”不算通过恢复验收。

升级顺序：

```bash
# 在旧包目录
bin/analysis-node backup
bin/analysis-node down

# 保留旧包目录，在新包目录完成签名/配置/空间预检后
bin/analysis-node verify-bundle
bin/analysis-node load-images
bin/analysis-node migrate
bin/analysis-node up
bin/analysis-node doctor --smoke --timeout 600
```

正常回滚优先停止新版本并用保留的旧包启动，不自动反向 migration。只有 Release Notes 明确指出旧
代码不能读取新 schema，且已在演练机验证 dump 时，才执行显式数据库恢复：

```bash
bin/analysis-node restore /absolute/backup/analysis-node-....dump \
  --confirm-database-restore
```

恢复命令只接受配置的备份根目录内、清单与 SHA-256 一致的 dump。它先停止 gateway、worker、
dispatcher 等所有写入方，仅启动数据库，再生成覆盖前安全备份，避免安全备份完成后仍有新写入
被后续 `dropdb` 丢失；它不会恢复或删除文件存储。恢复后仍须恢复匹配的文件系统快照，再启动旧
版本并跑冒烟。

## 第三方 Workflow 包信任

内置 `analysis-node-smoke@1.0.0` 来自已签名且镜像锁定的后端交付物，安装器会自动写入不可变证明。
客户或集成商提供的 WorkflowVersion 必须先对下面的最小声明做 Sigstore 签名：

```json
{
  "schema_version": 1,
  "workflow_version_id": 123,
  "source_digest": "sha256:<WorkflowVersion compiled digest>"
}
```

签名并在 Analysis Node 主机离线验证、登记：

```bash
cosign sign-blob --yes \
  --bundle workflow-package.sigstore.json \
  workflow-package.json

bin/analysis-node attest-workflow-package \
  --manifest workflow-package.json \
  --signature-bundle workflow-package.sigstore.json \
  --certificate-identity \
  'https://github.com/customer/repository/.github/workflows/release.yml@refs/tags/workflow-v1.0.0'
```

安装器使用 `cosign verify-blob --offline` 验证精确证书 identity 和 OIDC issuer，再把声明摘要、
Sigstore bundle 摘要、签名身份和 WorkflowVersion 源码摘要写成不可变证明。不要绕过安装器直接
调用后端登记命令。证明完成后才能用 `manage_analysis_product` 发布该 WorkflowVersion；执行、
重试时还会再次确认源码摘要和证明一致。更换源码、签名声明或签名身份必须发布新的
WorkflowVersion，不能覆盖既有证明。

## 第三方系统接入

第三方平台应把 Analysis Node 当作有版本的外部组件：

1. 由部署管理员发布稳定 Analysis Product 并向上游分配最小 scope 的 Service Account；
2. 上游先调用 preflight，再用 Idempotency-Key 提交任务并持久化 Analysis Node run ID；
3. 上游通过详情/批量状态接口查询，或接收带签名和去重键的终态 Webhook；
4. 大结果通过 Artifact Export 交付，接收方核对 manifest digest 后显式确认；
5. 重试沿用稳定 Analysis Product 契约，不引用内部 WorkflowVersion ID。

完整请求、幂等、状态机、Webhook、结果清单和 MCP 契约见
[第三方分析投递 API 与 MCP](14-integration-api-and-mcp.md)。

离线交付包同时包含可选的 `reference-connector/`：它提供零第三方运行时依赖的 Python Connector、
独立容器配置、Python/Java 调用示例、curl/Postman 资产和契约兼容性说明。需要把客户 MES 字段映射、
超时找回、事件去重与结果摘要回执落到可运行组件时，按
[Reference Connector 与 MES 兼容性套件](20-reference-connector.md)部署；客户私有映射仍保留在
Connector，不进入 Analysis Node 核心。
