# Reference Connector 与 MES 兼容性套件

## 1. 定位与责任边界

Reference Connector 是 Analysis Node 与 MES、LIMS、运营平台之间的适配层。它把客户业务结构映射为
稳定 Analysis Request，并把运行状态和经验证的结果证据交还给原提交系统。平台核心只理解分析产品、
样本执行标识、受管输入、资源引用和输出契约，不理解某一家客户的订单表、患者字段或内部状态码。

```text
MES / 运营平台
  | 业务订单 + external_run_id
  v
Reference Connector
  | 标准 Analysis Request + Idempotency-Key
  v
Analysis Node
  | 签名终态/交付 Webhook
  v
Reference Connector
  | 状态查询、小结果回执，或 Artifact Export 清单
  v
原 MES / 运营平台
```

各层所有权：

| 能力 | MES/运营平台 | Connector | Analysis Node |
| --- | --- | --- | --- |
| 业务订单、患者身份、客户状态码 | 主数据 | 只按白名单读取 | 不接收 |
| 客户字段到分析输入的映射 | 提供字段 | 负责并版本化 | 不包含客户分支 |
| Workflow、分析产品、运行状态 | 引用稳定产品 | 固定并核对契约 | 权威来源 |
| 幂等关系 | 生成稳定业务执行 ID | 持久化映射与回执 | 强制幂等键/外部引用 |
| 结果文件 | 决定业务入库 | 校验并形成回执 | 生成不可变输出清单 |
| 大结果交付凭据 | 不放入订单 | 只引用 profile | 部署侧保存并执行 export |

Connector 不读取或写入客户业务数据库。MES 通过 HTTP 拉取状态/结果，或者把验证后的回执写入自己的
事务；大结果可以由 Analysis Node 直接导出到客户预配置的 S3/MinIO 或受管目录。若客户需要反向
调用自己的消息总线，应在客户 Connector 分支中基于已持久化的 `event_id` 和 `result_digest` 增加
outbox，不能在 Webhook 请求事务中直接做不可恢复的业务入库。

## 2. 三种部署方式

### 2.1 平台集中部署

Connector 靠近 MES 部署，调用中心 BioWorkflowManage。适用于网络可达、分析平台集中运维的场景。
Connector 的入站地址由 MES 网关保护，出站只允许固定 Analysis Node origin。

### 2.2 与 Analysis Node 同服务器部署

第三方安装离线 Analysis Node 后，从交付包的 `reference-connector/` 构建独立容器。两者仍是独立
进程、独立身份和独立状态目录，通过稳定 Integration API 通信，不共享 PostgreSQL。该方式最适合
“分析平台作为组件嵌入客户平台”的交付。

离线包已经带有固定 Python 3.12 runtime 镜像。执行 Analysis Node `load-images` 后，在
`reference-connector/` 中准备 `config.json` 和三个环境变量。Linux 同机部署使用 host network，
因此 Connector 可通过宿主 loopback 访问默认安全配置下的 Analysis Node：

```json
{
  "listen": {"host": "127.0.0.1", "port": 8090},
  "analysis_api": {
    "base_url": "http://127.0.0.1:8082/api/v1/integration"
  }
}
```

端口应替换成 `.env` 中的 `ANALYSIS_NODE_API_PORT`，然后运行：

```bash
docker compose --env-file ../images.env -f compose.offline.yml up -d
```

该命令不会访问镜像仓库；源码场景可改用 `compose.example.yml` 构建独立 Connector 镜像。离线
Compose 不加入 Analysis Node 的私有 Compose 网络，也不共享数据库或文件系统；两者只通过宿主
loopback 上的 Integration API 通信。跨主机部署必须改用 HTTPS 地址。

### 2.3 客户自有 Connector

客户可以复用 Python 核心或按 OpenAPI 重新实现 Java/.NET 适配器。必须保留本文的幂等、超时找回、
状态版本、Webhook 去重和结果摘要语义。客户模型只存在自有适配器；不得向 Analysis Node 核心模型
加入私有字段。

当前参考实现使用本机 SQLite，面向单个活动实例。不要把同一 SQLite 文件放到 NFS，也不要同时运行
多个副本。需要 HA 时，应把 `ConnectorStore` 替换成支持事务、唯一约束和行锁的共享数据库，并保持
现有表语义和测试套件；不能仅启动多个容器共享本地状态。

## 3. 上线前准备

### 3.1 Analysis Node 身份与产品

为 Connector 创建独立 Service Account，建议 scope：

```text
workflow:read
analysis:submit
analysis:read
analysis:download
analysis:export
analysis:acknowledge
```

不用取消或重跑时不要授予对应 scope。先通过 `/analysis-products` 找到要固定的
`analysis_code + contract_version + contract_digest`，把三者写入 Connector 配置。参考配置中的
全零摘要是失败关闭占位符，不能用于生产。

### 3.2 Webhook

注册 Connector 的签名接收地址，`client-id` 必须与 Connector `mapping.client_id` 和 Service
Account 一致：

```bash
docker compose exec backend python backend/manage.py manage_webhook_endpoint \
  --client-id mes-reference \
  --name reference-connector \
  --url https://connector.example.com/v1/webhooks/bioworkflow \
  --event analysis.run.terminal \
  --event analysis.artifact_export.completed \
  --actor deployment
```

只显示一次的 `SIGNING_SECRET` 放入 Connector 的 `BIOWORKFLOW_WEBHOOK_SECRET`。外层代理必须保留
原始 body 和所有 `X-BioWorkflow-*` headers。服务器需要 NTP；默认只接受前后 300 秒的事件。

### 3.3 Connector 密钥

| 环境变量 | 用途 | 要求 |
| --- | --- | --- |
| `BIOWORKFLOW_TOKEN` | Connector 调用 Analysis Node | 独立最小权限 Service Token |
| `BIOWORKFLOW_WEBHOOK_SECRET` | 验证 Analysis Node 回调 | endpoint 输出的 32-byte URL-safe Base64 secret |
| `REFERENCE_CONNECTOR_INBOUND_TOKEN` | MES 调用 Connector | 独立随机值，至少 32 字符，不含空白 |

三者都不得进入配置 JSON、镜像、Git、URL 或日志。通过客户密钥管理系统、容器 secret 或受限环境变量
注入。轮换上游 Token 时先部署新 Token；轮换 Webhook secret 时按 Analysis Node 的 endpoint 轮换
窗口处理队列中的旧版本事件。

## 4. 配置与映射

可运行模板位于
[`examples/reference_connector/config.example.json`](../examples/reference_connector/config.example.json)。
配置只允许已知字段，启动时会验证普通文件、1 MiB 上限、URL、数值范围、密钥格式和映射版本。
该模板用于直接运行 Python，状态路径相对配置文件解析；源码 Compose 使用容器内监听
`0.0.0.0` 的 `config.compose.example.json`，离线 host-network Compose 使用只监听 loopback 的
`config.offline.example.json`。两个容器模板都把状态固定到持久卷 `/var/lib/reference-connector`。

映射规则：

- `fields.external_run_id` 必须来自 MES 的稳定执行 ID；它同时作为 Analysis Node 幂等键；
- `fields.sample_id` 只传分析所需的非身份样本编码；
- `inputs` 是允许进入平台的输入白名单；受管文件只能使用 `rawdata`/`database` 相对路径，拒绝
  绝对路径和 `..`；
- `reference` 输入会递归拒绝 endpoint、Access Key、Secret、Token 和预签名 URL；
- `metadata` 只转发显式列出的标量；患者姓名、机构、医生和客户私有字段不会因为出现在订单中就
  被转发；
- `database` 只允许 `reference_id` 和 `panel_id`；
- 产品版本和预期 `contract_digest` 由部署者固定，不能由每个订单动态选择。

新增客户只改 Connector 配置或客户适配代码，并用脱敏订单补一条 mapping test。不要给映射引擎增加
任意表达式执行能力，也不要把原始订单整体放入 `metadata`。

## 5. 端到端时序与恢复规则

1. MES 为一次业务执行生成不可复用的 `external_run_id`，提交完整订单。
2. Connector 映射规范请求并计算规范 JSON SHA-256。
3. Connector 先读取无副作用的 OpenAPI，确认 Integration API 是带 `request_digest` 能力的 1.5.0+
   1.x 契约；通过后提交 SQLite 订单记录，再读取固定 Analysis Product 并执行 preflight。
4. `submission_allowed=true` 后，使用 `external_run_id` 作为 `Idempotency-Key` 提交。
5. 如果提交响应超时，Connector 先按 external reference 找回；找不到时才用同一个幂等键重试。
6. Analysis Node 返回 run ID 后，Connector 绑定唯一的 `external_run_id -> run_id`。
7. Analysis Node 发送签名终态事件。Connector 先验签，再按 `event_id` 去重，按 `status_version`
   忽略乱序旧事件。
8. MES 可以查询 Connector 状态。成功后调用小结果收取，或请求大结果 export。
9. MES 只在本地事务中确认一个新的 `result_digest`/`manifest_digest`；相同摘要重放不重复入库，
   相同任务出现不同摘要必须报警并停止。

关键不变量：

- 相同 `external_run_id + 相同规范请求` 安全复用；相同 ID 对应不同请求返回冲突；
- 超时未知时不能换 ID、换引擎或修改请求；
- 状态只接受更高 `status_version`，同版本不同状态视为冲突；
- 相同 `event_id` 相同 payload 是重放，相同 `event_id` 不同 payload 是完整性冲突；
- 结果入库以经校验清单摘要为业务幂等键，不以“收到一次 HTTP 200”为依据。

## 6. 结果交付

### 6.1 小结果下载

`POST /v1/orders/{id}/results` 会主动对账，要求 `status=succeeded` 且
`output_status=complete`。每个 file 必须有 size 和 SHA-256；Directory 输出必须先在分析流程中打包为
File（例如 tar 归档）再交付。Connector 在受限目录中原子落盘，
复算证据，再持久化规范 `result_digest`。默认单文件上限 512 MiB，可降低；实现会把文件读入内存，
大文件不要走该路径。

响应会同时返回完整规范 `manifest/results`（包括 value）和每个 file 的 `download_url`。MES 使用相同
Bearer Token 调用 `GET /v1/orders/{id}/outputs/{key}`；Connector 会在发送前再次核对落盘文件的大小
和 SHA-256，并通过 `X-Checksum-SHA256` 返回证据。`local_path` 只是 Connector 内部审计位置，容器
部署时 MES 不应依赖该路径。Python/Java 示例均提供 `download` 命令并在落盘前验证响应证据。

MES 获取 `result_digest` 和本地文件回执后，在自己的数据库事务中执行：

```text
if no receipt(external_run_id):
    ingest business result
    insert unique receipt(external_run_id, result_digest)
else if receipt.digest == result_digest:
    no-op
else:
    stop and alert integrity conflict
```

### 6.2 Artifact Export

大结果使用 `POST /v1/orders/{id}/exports`，只传部署侧 profile 名。endpoint、bucket 凭据和目录根
不来自 MES 请求。收到交付完成事件后调用 `complete-export`：Connector 会读取 export 详情，核对
canonical manifest digest、逐文件 size/SHA-256 证据，并在 `requires_ack=true` 时以确定性回执确认
一次。传输目标的文件内容由客户存储协议核验；manifest 是业务入库和审计的权威证据。

## 7. 错误处理矩阵

| HTTP/错误码 | 含义 | MES/运维动作 |
| --- | --- | --- |
| `401 CONNECTOR_INBOUND_AUTH_INVALID` | MES 到 Connector 的 Token 无效 | 不重试；修复密钥或网关配置 |
| `400 CONNECTOR_MAPPING_INVALID` | 订单缺字段、路径不安全或含禁用凭据 | 不重试；修复映射/业务数据 |
| `400 CONNECTOR_INTEGRATION_API_INCOMPATIBLE` | Integration API 低于 1.5.0、主版本不兼容或缺少 `request_digest` | 提交尚未发生；升级 Analysis Node 或使用匹配版本 Connector |
| `400 CONNECTOR_ANALYSIS_PRODUCT_NOT_READY` | 固定产品当前不可投递 | 检查产品 blockers；不要改投“最新版” |
| `400 CONNECTOR_PREFLIGHT_BLOCKED` | 输入、数据库或资源预检拒绝 | 按 `details.checks` 修复后，用同一业务 ID 重交相同语义请求 |
| `409 CONNECTOR_IDEMPOTENCY_CONFLICT` | 同一业务 ID 对应不同请求/run/结果 | 停止自动处理并人工核对，不能换 ID 掩盖冲突 |
| `503 CONNECTOR_UPSTREAM_UNAVAILABLE` | Analysis Node 网络/5xx 暂时不可用 | 指数退避，原样重试 |
| `503 CONNECTOR_SUBMISSION_UNCERTAIN` | 提交可能已成功但暂时无法找回 | 只调用 reconcile 或原样重试，绝不创建新 ID |
| `400 CONNECTOR_WEBHOOK_SIGNATURE_INVALID` | 签名、密钥版本或 header 无效 | 检查 endpoint secret/代理；不接受事件 |
| `400 CONNECTOR_WEBHOOK_TIMESTAMP_OUT_OF_RANGE` | 时钟偏差或延迟超过窗口 | 校时并由 Analysis Node 重放同一 delivery |
| `400 CONNECTOR_UPSTREAM_INTEGRITY_FAILED` | run/product 归属、清单、size、SHA-256 或 export 摘要不一致 | 禁止入库；隔离结果并告警 |
| `400 CONNECTOR_RUN_NOT_SUCCEEDED`（retryable） | 分析尚未终态成功 | 等待 Webhook 或按退避 reconcile |
| `400 CONNECTOR_EXPORT_NOT_READY`（retryable） | export 仍在执行 | 等待交付 Webhook后重试 |
| 上游稳定错误码 | Analysis Node 拒绝请求 | 按响应 `retryable` 分支，不解析中文 message |

Connector 错误响应结构固定为 `error.code/message/retryable/details`。只有 `retryable=true` 或 HTTP
503 可以自动退避重试；409 和完整性错误必须人工介入。

## 8. 安全与运维

- Connector 本身不终止生产 TLS；放在客户 API Gateway/Ingress 后，限制 MES 来源网段；
- 除 `/health` 和签名 Webhook 外，所有路由要求独立 Bearer Token；
- 上游 URL 必须固定到 `/api/v1/integration`，Connector 不跟随任何 HTTP 重定向，也不访问响应中
  跨 origin/跨前缀的链接；
- SQLite 文件权限为 `0600`，状态和结果目录为 `0700`；容器使用非 root、只读根文件系统、无 Linux
  capabilities；
- `/health` 只是 liveness，不代表产品、输入或上游就绪。上线监控还应定期调用鉴权后的 `/v1/products`
  并对关键产品做合约摘要比较；
- 备份 SQLite、结果回执和客户 Artifact Export 目标。备份时停止写入或使用 SQLite 在线备份机制；
- 监控 401/409/503、Webhook 签名失败、乱序事件数、提交未知、结果完整性失败、磁盘容量和 NTP；
- 日志不得记录订单 body、Token、签名、患者字段或下载 URL。

## 9. 第三方接入检查表

### Analysis Node 管理员

- [ ] 发布固定 Analysis Product，并记录 code/version/contract digest；
- [ ] 创建最小权限 Service Account 和可轮换 Token；
- [ ] 配置只读输入根、资源 Catalog 和必要 Artifact Export profile；
- [ ] 注册两个事件类型的 Connector Webhook，安全交付 signing secret；
- [ ] 确认网关、DNS、TLS、出站限制和 NTP。

### Connector 实施方

- [ ] 只映射分析必需字段，脱敏 mapping fixture 通过；
- [ ] 固定真实 contract digest，删除全零占位值；
- [ ] 三个 secret 仅由密钥系统注入；
- [ ] 使用持久本地磁盘，单实例运行，备份/恢复演练完成；
- [ ] MES 超时重试始终复用原 external ID 和原请求；
- [ ] Webhook 验签、重放、乱序、时钟偏差演练通过；
- [ ] 小结果和大结果路径均验证摘要，MES 以 digest 唯一回执防重复入库；
- [ ] Postman/curl 在预生产真实网络跑通。

### MES/运营平台

- [ ] 每次业务执行产生稳定且唯一的 `external_run_id`；
- [ ] 同一 ID 的请求内容不可变；
- [ ] 仅把 Connector 回执写回自己的数据库，不直连 Analysis Node 数据库；
- [ ] 业务入库使用 `external_run_id + result_digest` 唯一约束；
- [ ] 对 409、完整性失败和摘要冲突设置人工告警。

## 10. 兼容性门禁

Reference Connector 在首个订单提交前还会读取 `/openapi`，确认运行节点支持 1.5.0+ 的 1.x 契约和
`AnalysisRun.request_digest`，不兼容时在创建 AnalysisRun 前失败关闭。它同时使用
[`contract-surface.json`](../examples/reference_connector/contract-surface.json) 固定它依赖的 OpenAPI
operations、parameters、schemas、webhooks 和规范投影摘要。`scripts/validate_contracts.py` 会在 CI
中重新计算投影；接口变更未同步 Connector 时直接失败。

兼容性套件覆盖：

- 相同订单重复提交只创建一个 AnalysisRun；
- 提交在服务端成功、客户端响应超时后的 external-ref 找回；
- Webhook 重放、乱序旧版本和相同 event ID 内容冲突；
- 小结果 size/SHA-256 校验和重复 MES 入库 no-op；
- Artifact Export manifest 摘要与只确认一次；
- 使用真实 Django Integration API、真实 Service Token、固定 Analysis Product 和输出下载的闭环；
- Analysis Node 正式交付门在空 Docker data-root 安装后，用包内 Connector 完成产品发现与摘要固定、
  重复提交、真实 worker 执行、轮询、重复结果收取和 Token 吊销；
- OpenAPI operation 漂移失败。

本地验证：

```bash
uv run pytest backend/tests/test_reference_connector.py
uv run python scripts/validate_contracts.py
uv run ruff check examples/reference_connector/reference_connector \
  examples/reference_connector/clients/python_mes_client.py
```

示例运行、Java 17 客户端、curl 和 Postman 入口见
[`examples/reference_connector/README.md`](../examples/reference_connector/README.md)。
