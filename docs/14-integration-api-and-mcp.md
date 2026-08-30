# 第三方分析投递 API 与 MCP

## 1. 责任边界

BioWorkflowManage 作为通用分析执行控制面，负责固定 Workflow/Tool 版本、受管资源预检、
排队、miniwdl 执行、状态、事件、取消、重跑和语义化输出。OKB 等报告系统继续负责患者、
样本业务、QC/SNV/CNV 入库和报告，不共享数据库，也不直接写 `AnalysisRun`。

外部系统只通过 `/api/v1/integration/` 接口访问。在线 OpenAPI 位于：

```text
GET /api/v1/integration/openapi
```

仓库中的同一份契约为
[`schemas/integration-openapi-v1.json`](../schemas/integration-openapi-v1.json)。

## 2. Service Account 与 Token

Token 原文只显示一次，数据库只保存 SHA-256 摘要。Token 可过期、轮换和立即吊销；
Service Account 默认只能看到自己提交的任务。

创建 OKB 机器身份并签发一个 90 天 Token：

```bash
docker compose exec backend python backend/manage.py manage_service_account \
  --client-id okb \
  --name "OKB report system" \
  --scope analysis:submit \
  --scope analysis:read \
  --scope analysis:cancel \
  --scope analysis:retry \
  --scope analysis:download \
  --scope workflow:read \
  --token-name production-01 \
  --expires-days 90 \
  --issue-token \
  --actor zhuqin
```

把输出的 `TOKEN` 立即存入 OKB 的密钥管理或受限环境变量，不写入 Git、日志或任务
metadata。轮换时先签发新 Token，完成调用方切换后按 prefix 吊销旧 Token：

```bash
docker compose exec backend python backend/manage.py manage_service_account \
  --client-id okb \
  --revoke-prefix <TOKEN_PREFIX> \
  --actor zhuqin
```

权限最小化建议：

| 调用方 | 建议 scope |
| --- | --- |
| 只读目录/状态 | `workflow:read`、`library:read`、`analysis:read` |
| AI 小数据 Task 测试 | 上述只读权限 + `task:test` |
| 报告系统投递 | `workflow:read`、`analysis:submit`、`analysis:read`、`analysis:download` |
| 人工运维代理 | 按需额外增加 `analysis:cancel`、`analysis:retry` |

不要默认给 AI Agent 取消、重跑权限。Service Account 的 scope 发生变化时，该账户的所有
Token 会立即按新 scope 生效。

## 3. 稳定分析产品与固定版本投递

MES、运营平台等业务调用方优先使用稳定的 `analysis_code + contract_version`，不直接依赖
内部 WorkflowVersion 数字 ID。管理员把一个外部契约固定发布到不可变 WorkflowVersion：

```bash
docker compose exec backend python backend/manage.py manage_analysis_product \
  --code dna-panel \
  --name "DNA Panel Analysis" \
  --contract-version 1.0.0 \
  --workflow-version-id 123 \
  --actor zhuqin
```

相同产品契约重复执行命令会安全复用；相同 `analysis_code + contract_version` 不能改绑其他
WorkflowVersion。需要阻止新任务时使用 `--deactivate`，已存在的运行与证据不受影响。

外部调用方读取产品目录和固定契约：

```bash
curl -sS -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  http://localhost:8082/api/v1/integration/analysis-products

curl -sS -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  http://localhost:8082/api/v1/integration/analysis-products/dna-panel/versions/1.0.0
```

投递时使用：

```json
{
  "analysis_product": {
    "analysis_code": "dna-panel",
    "contract_version": "1.0.0"
  }
}
```

平台在接收任务时解析并固化实际 WorkflowVersion、source digest、contract digest 与接口契约；
不会动态选择“最新版”。

原有固定 WorkflowVersion 请求继续作为兼容接口。管理工具或尚未迁移的调用方可以先读取：

先读取可运行的固定版本：

```bash
curl -sS -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  http://localhost:8082/api/v1/integration/workflow-versions
```

只选择 `ready=true` 的版本，并同时保存 `id` 与 `source_digest`。兼容投递不接受“最新版本”；
每次请求都必须携带固定 `version_id` 和 `expected_source_digest`。

Integration API 不直接执行任意历史 WDL 草稿。需要对外投递的历史 WDL 应先发布为
`WorkflowVersion`，以同时冻结完整源码包、输入契约、数据库约束和语义输出契约。

受管文件不能使用宿主绝对路径，只能引用已配置目录中的相对路径：

```json
{
  "root_alias": "rawdata",
  "relative_path": "project/S001_R1.fastq.gz"
}
```

`root_alias` 仅支持 `rawdata` 和 `database`。预检会阻止绝对路径、`..`、符号链接逃逸、
空文件和类型错误，并在有界行长和共享请求时间预算内检查 gzip FASTQ/FASTA 首条记录与
R1/R2 read ID。普通 File 记录包含 ctime、device、inode 的 `identity_v2`；调用方显式携带
`sha256` 时，预检会同步读取完整文件，清单标记 `verification=sha256`，并受总时间和单文件
字节上限约束。Directory 使用 `identity_digest`（`sha256-tree-identity-v1`）；历史
Directory `sha256` 仅兼容接收并忽略，不会被误当成目录身份摘要。

受管根目录在 backend/worker 中必须只读挂载，并由平台控制写入。File 打开会逐级使用
`openat` + `O_NOFOLLOW` 校验祖先目录，排队后任一祖先被替换为 symlink 都会失败关闭；只读挂载
同时收窄“校验完成到 miniwdl 打开文件”之间的外部替换窗口。Directory 身份扫描运行在独立、
可终止且每个服务进程单并发的子进程中；HTTP 预检使用 `ANALYSIS_RESOURCE_MANIFEST_TIMEOUT_SECONDS`，worker
复核使用 `ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS`。

File 输入也可以使用 S3/MinIO 兼容对象引用；Directory 仍只接受受管路径：

```json
{
  "type": "s3_object",
  "profile": "production-minio",
  "bucket": "validated-inputs",
  "key": "project/S001_R1.fastq.gz",
  "version_id": "3Lg...",
  "etag": "d41d8cd98f00b204e9800998ecf8427e",
  "size": 123456789,
  "sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

`version_id` 与 `etag` 至少提供一个，`size` 和 `sha256` 必填。API 只接受部署侧已注册的
`profile` 和该 profile 的 bucket 白名单；调用方不能提交 endpoint、Access Key、Secret Key
或预签名 URL。预检通过 `HeadObject + If-Match/VersionId` 核对身份，不下载大文件。Worker 在
`PREPARING` 阶段使用条件 `GetObject` 下载到 SHA-256 内容寻址的只读 staging，完整校验大小和
摘要后才把路径交给 miniwdl。若对象被覆盖、版本消失、ETag 改变、下载内容不符或 staging 被
篡改，任务失败关闭，不回退到“最新对象”。

每个 profile 是 `${ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH}/<profile>.json`，示例：

```json
{
  "endpoint_url": "https://minio.example.internal",
  "region": "us-east-1",
  "allowed_buckets": ["validated-inputs"],
  "client_grants": {
    "mes-production": {"validated-inputs": ["tenant-a/"]}
  },
  "allowed_cidrs": ["10.20.0.0/16"],
  "allow_private_network": true,
  "access_key_id": "<read-only access key>",
  "secret_access_key": "<read-only secret key>"
}
```

profile 文件不得提交到 Git；默认目录 `./secrets/object-storage` 已忽略。每个 profile 必须通过
`client_grants` 把 Service Account、bucket 与 key prefix 绑定为同一条授权，不能把多租户账号与
前缀分别配置成两个全局白名单。HTTP endpoint
默认拒绝，私网地址必须显式 `allow_private_network=true`，loopback/link-local/site-local/multicast/
unspecified/reserved 地址始终拒绝；`allowed_cidrs` 可进一步收窄出口。应用会把每次请求固定到本次已审核的
解析地址，拒绝跳转到其他 origin；HEAD 在可终止的隔离子进程中执行，超时后先回收子进程再释放
并发槽。生产环境仍应在主机防火墙或
容器网络策略中做第二层出口白名单。profile 只挂载给 backend 与 analysis-worker，不挂载给 miniwdl task 容器，凭据不会
进入 `AnalysisRun.request_payload`、日志、API 响应或 Webhook。

对象输入使用稳定错误码：引用/profile 无效为 `OBJECT_INPUT_REFERENCE_INVALID` /
`OBJECT_INPUT_PROFILE_INVALID`，bucket 或 endpoint 越权为 `OBJECT_INPUT_BUCKET_FORBIDDEN` /
`OBJECT_INPUT_ENDPOINT_FORBIDDEN`，身份变化为 `OBJECT_INPUT_CHANGED`，摘要不符为
`OBJECT_INPUT_DIGEST_MISMATCH`，暂存篡改为 `OBJECT_INPUT_STAGING_CHANGED`，并发/容量等待超时为
`OBJECT_INPUT_STAGE_BUSY` / `OBJECT_INPUT_STAGE_CAPACITY`，网络或硬超时为可重试的
`OBJECT_INPUT_UNAVAILABLE` / `OBJECT_INPUT_STAGE_TIMEOUT`。profile/前缀授权失败分别为
`OBJECT_INPUT_PROFILE_FORBIDDEN` / `OBJECT_INPUT_KEY_FORBIDDEN`，预检并发槽耗尽为
`OBJECT_INPUT_HEAD_BUSY`，预检硬超时为 `OBJECT_INPUT_HEAD_TIMEOUT`。外部系统只按 `code`、`category` 和
`retryable` 分支，不解析中文 message。

### 3.2 运行列表摘要

`GET /api/v1/integration/analysis-runs` 最多返回 200 条，响应带 `view=summary`。列表项中的
`outputs=[]`，错误只保留稳定 code/category/retryable，task timing 不展开；读取完整输出、错误详情
和 task timing 必须调用 `GET /api/v1/integration/analysis-runs/{run_id}` 或输出清单接口。

### 3.1 预检

预检不创建任务：

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8082/api/v1/integration/analysis-runs/preflight \
  -d @preflight.json
```

`preflight.json` 示例：

```json
{
  "analysis_product": {
    "analysis_code": "dna-panel",
    "contract_version": "1.0.0"
  },
  "inputs": {
    "read1": {"root_alias": "rawdata", "relative_path": "project/S001_R1.fastq.gz"},
    "read2": {"root_alias": "rawdata", "relative_path": "project/S001_R2.fastq.gz"}
  },
  "database": {"reference_id": "hg19", "panel_id": "tumor-120-v4"},
  "metadata": {"product_code": "PANEL001"}
}
```

当 Workflow 接口声明参考数据库依赖时，必须提供 `database.reference_id`；需要 Panel 时再提供
`panel_id`。服务端按 `workspace/databases/catalog.json` 校验每个必需文件，并把 Catalog、
参考库和 Panel 的资源清单固化到任务快照；Worker 领取时会再次校验。缺失项通过
`ANALYSIS_DATABASE_INCOMPLETE` 的 `details.missing` 返回。

预检的 `ready=false` 且 `submission_allowed=true` 表示输入与数据库已经通过，但当前内存不足；
任务仍可排队并在资源满足后执行，`waiting_for` 会包含 `execution_memory`。

### 3.2 幂等投递

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 019-okb-execution-uuid" \
  -H "X-Request-ID: 019-okb-request-uuid" \
  http://localhost:8082/api/v1/integration/analysis-runs \
  -d @submission.json
```

`submission.json` 在预检内容之外增加：

```json
{
  "external_ref": {
    "client_id": "okb",
    "external_run_id": "019-okb-execution-uuid",
    "external_analysis_id": "019-okb-analysis-uuid"
  },
  "analysis_product": {
    "analysis_code": "dna-panel",
    "contract_version": "1.0.0"
  },
  "subject": {"sample_id": "S001"},
  "inputs": {
    "read1": {"root_alias": "rawdata", "relative_path": "project/S001_R1.fastq.gz"},
    "read2": {"root_alias": "rawdata", "relative_path": "project/S001_R2.fastq.gz"}
  },
  "database": {"reference_id": "hg19", "panel_id": "tumor-120-v4"},
  "metadata": {"product_code": "PANEL001"}
}
```

相同 Idempotency-Key 和相同请求返回原任务，并带
`Idempotency-Replayed: true`；相同 key 或 `external_run_id` 对应不同请求返回
`409 IDEMPOTENCY_CONFLICT`。如果客户端在收到响应前超时，不得改投其他引擎，应先找回：

```text
GET /api/v1/integration/analysis-runs/by-external-ref?external_run_id=...
```

请求中不要放患者姓名、医院、医生等执行不需要的临床身份信息；服务端会拒绝这些字段。

## 4. 状态、事件、取消与重跑

状态机：

```text
queued -> preparing -> running -> succeeded
                           |  \-> failed
                           \----> cancel_requested -> canceled
queued --------------------------------------------> canceled
```

每次状态变化递增 `status_version`，`progress` 不倒退。调用方应以
`status_version` 判断新旧状态，而不是依赖轮询返回顺序。

```text
GET  /api/v1/integration/analysis-runs/{run_id}
GET  /api/v1/integration/analysis-runs/{run_id}/events?after_id=123
POST /api/v1/integration/analysis-runs/batch-status
POST /api/v1/integration/analysis-runs/{run_id}/cancel
POST /api/v1/integration/analysis-runs/{run_id}/retry
```

取消是幂等的。运行中取消会终止 miniwdl 进程组，并按本次运行目录挂载精确清理残留的
miniwdl Swarm service；运行目录、日志和已经形成的证据不会被删除。重跑只允许失败或已取消
任务，必须使用新的 `external_run_id` 与 Idempotency-Key，
并创建新 `AnalysisRun`；原版本、输入、日志和输出保持不变。输入文件已被替换时重跑会
返回 `409 ANALYSIS_RESOURCE_CHANGED`。

常见稳定错误：

| code | category | 是否建议重试 |
| --- | --- | --- |
| `SERVICE_TOKEN_INVALID` | authentication | 否，先轮换凭据 |
| `SERVICE_SCOPE_REQUIRED` | authorization | 否，调整 scope |
| `IDEMPOTENCY_CONFLICT` | validation | 否，找回原任务或使用新外部 ID |
| `WORKFLOW_VERSION_CHANGED` | workflow | 否，重新读取固定版本 |
| `ANALYSIS_PRODUCT_VERSION_NOT_FOUND` | workflow | 否，重新读取产品目录 |
| `ANALYSIS_PRODUCT_INACTIVE` | workflow | 否，联系平台管理员或选择其他已发布契约 |
| `ANALYSIS_PRODUCT_SNAPSHOT_CHANGED` | workflow | 否，停止投递并审计产品发布数据 |
| `MANAGED_RESOURCE_*` / `ANALYSIS_RESOURCE_CHANGED` | input/resource | 否，修复资源后新建任务 |
| `ANALYSIS_INFRASTRUCTURE_ERROR` | infrastructure | 是，确认执行环境健康后重跑 |
| `ANALYSIS_WORKER_LEASE_LOST` | infrastructure | 是，确认旧实例已终止后重跑 |
| `ANALYSIS_TASK_FAILED` | application | 否，检查 Task 日志与参数 |
| `REQUIRED_OUTPUT_MISSING` | application | 否，执行成功但输出契约不完整 |
| `ANALYSIS_CANCELED` | cancellation | 否 |

所有 Integration API 错误均使用：

```json
{
  "error": {
    "code": "WORKFLOW_VERSION_CHANGED",
    "category": "workflow",
    "message": "...",
    "retryable": false,
    "details": {},
    "request_id": "..."
  }
}
```

## 5. 语义化输出

外部可运行 Workflow 必须为每个输出声明非空 `semantic_type`，例如
`report.qc_tsv`、`report.snv_tsv`。输出接口只公开契约内输出，不公开运行目录或宿主路径：

```text
GET /api/v1/integration/analysis-runs/{run_id}/outputs
GET /api/v1/integration/analysis-runs/{run_id}/outputs/download?key=...
```

文件清单包含 `key`、`semantic_type`、`kind`、`size`、`sha256`、`content_type` 和受保护
下载地址。worker 在完成时计算 SHA-256，写入内容地址快照，并固化源/快照文件身份。下载前做 O(1)
身份校验后流式读取快照，不在 HTTP 请求中重新哈希大文件。miniwdl 成功但缺少必需输出时：

```text
execution_status = succeeded
output_status = incomplete
error.code = REQUIRED_OUTPUT_MISSING
```

OKB 后续业务入库失败不得反向改写 BioWorkflowManage 的执行状态。
v2 输出清单按单项放行：若某个值、目录或文件无法固化，运行保持
`execution_status=succeeded`、`output_status=incomplete`，该项返回 `kind=unverifiable` 且下载返回
409 `ANALYSIS_OUTPUT_INCOMPLETE`；同一清单内其他完整 File 仍带下载地址并可正常下载。
对非 File 项调用下载端点返回 404。
升级前的历史清单只有 `schema_version=1`、没有 `integrity_version=2` 时不提供下载地址；下载
请求稳定返回 409 `ANALYSIS_OUTPUT_UNVERIFIED`，完成发布手册中的受控回填后才恢复下载。

## 6. 事务 Outbox 与签名 Webhook

Webhook 只通知 `succeeded`、`failed`、`canceled` 三种终态。状态更新、不可变
`IntegrationOutboxEvent` 和当时已激活订阅对应的 `WebhookDelivery` 在同一数据库事务中提交；
`analysis-worker` 和 HTTP 请求都不访问外部网络。独立 `webhook-dispatcher` 领取 delivery，
因此接收方故障不会改变 `AnalysisRun.status`、`output_status` 或错误结论。

### 6.1 注册与密钥

先在所有 backend/dispatcher 实例配置稳定且独立保存的 `WEBHOOK_SIGNING_KEY`。未显式配置时
会从 `DJANGO_SECRET_KEY` 做域隔离派生，以兼容现有部署；生产环境仍建议使用独立随机值。更换
部署主密钥会改变所有 endpoint 的派生密钥，必须按接收方迁移窗口协调，不能直接覆盖。

```bash
docker compose exec backend python backend/manage.py manage_webhook_endpoint \
  --client-id mes-prod \
  --name terminal \
  --url https://mes.example.com/hooks/bioworkflow \
  --event analysis.run.terminal \
  --actor deployment
```

命令只在新建或 `--rotate-secret` 时显示一次 `SIGNING_SECRET`。数据库只保存 endpoint UUID、
随机 salt 和版本，签名密钥由部署主密钥派生，不保存明文。接收方切换新密钥后可执行：

```bash
docker compose exec backend python backend/manage.py manage_webhook_endpoint \
  --client-id mes-prod --name terminal --rotate-secret --actor security-rotation
```

每个 delivery 在终态事务中固化当时的目标 URL、salt 和密钥版本；之后修改 endpoint 只影响新事件，
不会把历史待投递事件静默改投其他地址。轮换期间接收方应短暂同时接受仍在队列中的旧版本密钥。

### 6.2 事件与验签

Webhook body 不包含输出正文或文件；接收方收到通知后使用现有详情/输出清单 API 拉取结果：

```json
{
  "schema_version": "1.0.0",
  "event_id": "0ca8c5a0-8856-4adb-8964-c40020fed36e",
  "event_type": "analysis.run.terminal",
  "occurred_at": "2026-08-30T06:00:00+00:00",
  "data": {
    "run_id": "d275913e-2102-44b2-9d0a-79fc8b48d37f",
    "run_kind": "workflow",
    "external_ref": {
      "client_id": "mes-prod",
      "external_run_id": "MES-20260830-001"
    },
    "analysis_product": {
      "analysis_code": "dna-panel",
      "contract_version": "1.0.0",
      "contract_digest": "sha256:..."
    },
    "status": "succeeded",
    "status_version": 4,
    "output_status": "complete",
    "finished_at": "2026-08-30T06:00:00+00:00",
    "error": null,
    "links": {
      "run": "/api/v1/integration/analysis-runs/d275913e-2102-44b2-9d0a-79fc8b48d37f",
      "outputs": "/api/v1/integration/analysis-runs/d275913e-2102-44b2-9d0a-79fc8b48d37f/outputs"
    }
  }
}
```

每次请求携带：

- `X-BioWorkflow-Delivery-ID`：同一 endpoint 的自动重试和人工重放保持不变；
- `X-BioWorkflow-Event-ID`：跨投递去重键，与 body `event_id` 相同；
- `X-BioWorkflow-Timestamp`：Unix 秒；
- `X-BioWorkflow-Secret-Version`：endpoint 密钥版本；
- `X-BioWorkflow-Signature`：`v1=<hex HMAC-SHA256>`。

签名原文是 UTF-8/bytes 拼接：

```text
delivery_id + "." + event_id + "." + timestamp + "." + canonical_json_body
```

`canonical_json_body` 使用 key 排序、无多余空白的 UTF-8 JSON。接收方必须先对原始 body 做
constant-time HMAC 比较，再检查 timestamp（建议 ±300 秒），持久化去重 `event_id`，最后仅在
`status_version` 不旧于本地已处理版本时推进状态。OpenAPI 3.1 顶层 `webhooks.analysisRunTerminal`
定义了完整 header 与 body 契约。

### 6.3 重试、死信、重放与指标

dispatcher 对网络错误和非 2xx 响应执行至少一次投递。间隔按
`WEBHOOK_BACKOFF_BASE_SECONDS * 2^(attempt-1)` 指数增长，并受
`WEBHOOK_BACKOFF_MAX_SECONDS` 限制；达到 `WEBHOOK_MAX_ATTEMPTS` 后进入 `dead_letter`。
`WEBHOOK_DELIVERY_TIMEOUT_SECONDS` 是覆盖 DNS、连接、TLS、响应 header 和有限响应 body 的
单次 wall-clock 总时限，不是可被慢速滴流持续续期的 idle timeout。worker 租约过期会以同一个
delivery ID 重试，因此接收方必须去重。

```bash
# 查看 pending/delivering/delivered/dead_letter 数量、到期数和最老 pending 年龄
docker compose exec backend python backend/manage.py webhook_delivery_stats

# 查找指定 Service Account 最近的死信及其 delivery ID（最多返回 200 条）
docker compose exec backend python backend/manage.py webhook_delivery_stats \
  --state dead_letter --client-id mes-prod --limit 50

# 修复接收方后人工重放；保留原 delivery ID 和所有历史 attempt 审计
docker compose exec backend python backend/manage.py replay_webhook_delivery \
  --delivery-id <uuid> --actor operator@example.com
```

每次 attempt 固化开始/结束时间、HTTP 状态、有限响应摘要和稳定错误码；目标一旦解析成功，
签名 timestamp 与固定 IP 会在发送前落库，因此发送后进程崩溃也保留审计证据。DNS 在解析前
失败的 attempt 对应字段为空。重放只把 delivery 重新排队，不清空历史 attempt，不修改分析运行。
原有轮询、事件增量和结果接口始终可用。

### 6.4 SSRF 与网络边界

- 默认只允许 HTTPS，不跟随 3xx redirect；
- 注册和每次投递都解析全部 A/AAAA，任一地址为私网、环回、link-local、site-local、组播或保留地址即失败；
- 连接使用本次已验证的 IP，同时以原 hostname 做 Host/SNI 和证书校验，关闭 DNS 重绑定窗口；
- 私有化部署确需内网目标时，只能在 `WEBHOOK_PRIVATE_HOST_ALLOWLIST` 精确列出 hostname/IP；
- 仅本地受控测试才在 `WEBHOOK_ALLOWED_HTTP_HOSTS` 精确允许 HTTP hostname；该白名单不自动放行私网地址。

## 7. AI Agent MCP

MCP 是对 Integration API 的 stdio 适配层，不直接访问 ORM，也不能绕过 Service Token
scope。启动命令：

```bash
BIOWORKFLOW_URL=http://127.0.0.1:8082 \
BIOWORKFLOW_TOKEN=<service-token> \
uv run python backend/manage_mcp.py
```

通用 MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "bioworkflow-manage": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/BioWorkflowManage",
        "run",
        "python",
        "backend/manage_mcp.py"
      ],
      "env": {
        "BIOWORKFLOW_URL": "http://127.0.0.1:8082",
        "BIOWORKFLOW_TOKEN": "<service-token>"
      }
    }
  }
}
```

已开放能力：

- 查询稳定分析产品、固定 WorkflowVersion、ToolVersion 和软件知识库；
- 对稳定分析产品或固定 WorkflowVersion 做受管输入预检和幂等小数据测试；
- 对不可变 ToolVersion 做独立 Task 预检和小数据测试；
- 查询运行、按外部 ID 找回、增量读取事件和语义输出；
- 仅在 scope 明确授权时取消或重跑。

刻意不开放：编辑/发布 WDL 或 Tool、修改软件知识、管理用户/Token、任意绝对路径、宿主
命令、Docker 管理和直接数据库访问。AI 测试应使用小数据，先 preflight，再 submit。

## 8. 升级与验证

迁移到包含 `0021_serviceaccount_servicetoken_and_more` 至
`0030_inputstagingcoordinator_inputstaginglease` 的版本前先停止分析 Worker 与
Webhook dispatcher，避免代码与数据库 Schema 短暂不一致：

```bash
docker compose --profile wdl-runtime stop analysis-worker
docker compose --profile wdl-host-runtime stop analysis-worker-host
docker compose stop webhook-dispatcher
docker compose up -d --build backend
docker compose up -d --build webhook-dispatcher
docker compose --profile wdl-host-runtime up -d --build analysis-worker-host
docker compose restart gateway
```

backend entrypoint 会自动执行 migration。验证：

```bash
docker compose exec backend python backend/manage.py showmigrations workflows
curl -fsS http://127.0.0.1:8082/api/v1/ready
curl -fsS http://127.0.0.1:8082/api/v1/integration/openapi >/dev/null
```

资源快照的请求时间、目录条目数/深度、受管输入项数、文本行长、gzip header 和显式文件
SHA-256 字节上限由
`ANALYSIS_RESOURCE_MANIFEST_*`、`ANALYSIS_MANAGED_*` 和 `ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS` 配置。具体默认值见
`.env.example`。worker 会在执行前重新验证同一证据；缺少 ctime/目录摘要的旧在途任务以
`ANALYSIS_RESOURCE_MANIFEST_OUTDATED` 失败关闭，必须重新投递。显式 File SHA 在 worker 侧沿用
同一字节上限并响应 lease 取消。目录还受独立深度上限约束。这些限时为 cooperative：可防止
持续遍历，但无法中断单次卡住的 NAS syscall。当前数据库目录没有后台预计算索引，提供
`identity_digest` 也仍会在请求中扫描核对；高延迟存储必须在部署层设置 NAS 超时并隔离/监控
异常挂载，后台索引属于后续演进项。

对象输入容量与调度由 `ANALYSIS_OBJECT_STAGE_*` 配置：单对象、单任务、并发预留总字节、
最小剩余空间、跨 Worker 并发槽、等待时间、每对象硬超时和单任务总暂存时间都独立受限。并发槽由 PostgreSQL
singleton row 串行分配，Worker 崩溃后 lease 到期自动回收。`ANALYSIS_INPUT_STAGING_HOST_PATH`
必须由 Worker UID 可写，并同时以只读方式挂载给 backend 和 miniwdl Docker daemon；主机运行
profile 还应把 `ANALYSIS_INPUT_STAGING_EXECUTION_ROOT` 设置为同一个宿主绝对路径。
单任务字节上限按不同远端对象身份的实际下载量计算；磁盘预留则按尚未命中的不同内容寻址路径计算，
相同声明摘要不能把多次远端传输折叠出配额。
内容寻址文件是持久缓存，当前版本不会自动回收；安全 retention/GC 由 #32 跟踪。在该能力合并前，
部署方必须监控 staging 使用量。需要人工回收时，先确认没有 `queued/preparing/running/cancel_requested`
任务并停止所有 analysis worker，只清点和处理 `sha256/` 内容树，保留 `.leases/` 交给 lease
回收逻辑；不得删除 staging 根目录或 Docker volume。下次执行会从远端重新验证并重建缺失内容。
每个暂存 lease 使用独立的 `.leases/<lease-id>` 临时目录；过期 lease 的孤儿普通文件会在下一次
分配槽位时按数据库活动 lease 白名单清理。内容寻址文件每次执行仍会对对应对象身份做条件 GET
并完整计算 SHA-256，缓存命中不能绕过对象删除、撤权或“对象身份→摘要”的真实性校验；FASTQ/
FASTA 语义和 R1/R2 首条 read ID 会在暂存后、启动 miniwdl 前再次校验。
配对审计证据只持久化规范化 read ID 的 SHA-256，不保存原始 FASTQ header/read ID。
