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

## 3. 固定版本投递

先读取可运行的固定版本：

```bash
curl -sS -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  http://localhost:8082/api/v1/integration/workflow-versions
```

只选择 `ready=true` 的版本，并同时保存 `id` 与 `source_digest`。投递不接受“最新版本”；
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
空文件和类型错误，并检查 gzip FASTQ/FASTA 首条记录与 R1/R2 read ID。

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
  "workflow": {
    "source_type": "workflow_version",
    "version_id": 123,
    "expected_source_digest": "sha256:..."
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
  "workflow": {
    "source_type": "workflow_version",
    "version_id": 123,
    "expected_source_digest": "sha256:..."
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
下载地址。下载前会再次验证文件身份与 SHA-256。miniwdl 成功但缺少必需输出时：

```text
execution_status = succeeded
output_status = incomplete
error.code = REQUIRED_OUTPUT_MISSING
```

OKB 后续业务入库失败不得反向改写 BioWorkflowManage 的执行状态。

## 6. AI Agent MCP

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

- 查询固定 WorkflowVersion、ToolVersion 和软件知识库；
- 对固定 WorkflowVersion 做受管输入预检和幂等小数据测试；
- 对不可变 ToolVersion 做独立 Task 预检和小数据测试；
- 查询运行、按外部 ID 找回、增量读取事件和语义输出；
- 仅在 scope 明确授权时取消或重跑。

刻意不开放：编辑/发布 WDL 或 Tool、修改软件知识、管理用户/Token、任意绝对路径、宿主
命令、Docker 管理和直接数据库访问。AI 测试应使用小数据，先 preflight，再 submit。

## 7. 升级与验证

迁移到包含 `0021_serviceaccount_servicetoken_and_more` 的版本前先停止两种 Worker，避免
代码与数据库 Schema 短暂不一致：

```bash
docker compose --profile wdl-runtime stop analysis-worker
docker compose --profile wdl-host-runtime stop analysis-worker-host
docker compose up -d --build backend
docker compose --profile wdl-host-runtime up -d --build analysis-worker-host
docker compose restart gateway
```

backend entrypoint 会自动执行 migration。验证：

```bash
docker compose exec backend python backend/manage.py showmigrations workflows
curl -fsS http://127.0.0.1:8082/api/v1/health
curl -fsS http://127.0.0.1:8082/api/v1/integration/openapi >/dev/null
```

Integration API 不增加新的环境变量；沿用现有
`ANALYSIS_RAWDATA_*`、`ANALYSIS_DATABASE_*`、`ANALYSIS_RUN_*` 和 Worker 配置。
