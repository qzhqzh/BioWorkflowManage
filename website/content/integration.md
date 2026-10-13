# 接入指南
业务后端通过固定产品契约提交分析，再独立接收和校验结果。

## 地址与认证 {#authentication}

API 基址为你部署环境的 `/api/v1/integration`，例如 `https://analysis.example.org/api/v1/integration`。官网不提供可执行分析的公共 API。

每个业务系统、每个环境使用独立 Service Account。管理员可签发最小闭环所需的 Token：

```bash
docker compose exec backend python backend/manage.py manage_service_account \
  --client-id evaluation-client \
  --name "Evaluation client" \
  --scope workflow:read \
  --scope analysis:submit \
  --scope analysis:read \
  --scope analysis:download \
  --expires-days 30 \
  --issue-token
```

命令会显示一次 Token，立即保存在业务后端的密钥配置中。不要写入网页、源码、日志或 AI 对话。取消、重跑等能力按需额外授权。

以下命令假设业务后端已安全注入 `BIOWORKFLOW_TOKEN`，并配置 `BIOWORKFLOW_API_BASE_URL`。不使用网页登录 Cookie，也不自动跟随跨域重定向。

## 1. 读取并固定产品 {#contract}

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-products"
```

读取选定产品的固定版本：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-products/$BIOWORKFLOW_ANALYSIS_CODE/versions/$BIOWORKFLOW_CONTRACT_VERSION"
```

持久化 `analysis_code`、`contract_version`、`contract_digest`，检查 `input_contract`、`output_contract`、`ready` 和 `blockers`。不要自动使用 latest。执行引擎由已发布产品决定，不由业务请求任选。

## 2. 准备输入并预检 {#inputs}

受管文件使用逻辑根与相对路径。这只是引用，不会上传文件：

```json
{"root_alias":"rawdata","relative_path":"evaluation/sample_R1.fastq.gz"}
```

先完成数据复制并保持输入不变。文件、数组、配对字段及参考资源按实际产品契约填写；S3/MinIO 输入使用部署侧 profile 与不可变对象标识，不能传任意下载地址或云凭据。

[预检模板](/downloads/preflight-template.json) 只提供请求结构。替换产品代码、契约版本和 inputs，保存为 `preflight.json`；模板本身不是预装的分析产品。

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/preflight" \
  --data-binary @preflight.json
```

读取 `submission_allowed`、`ready`、`waiting_for` 和 `checks`。`submission_allowed=true` 而 `ready=false` 可能表示资源等待、允许排队；预检不锁住输入、不预留资源。

若采用复制完成协议，应由数据投递方生成就绪标记，通过 readiness API 获取快照后原样携带，不能伪造就绪状态。该扩展的完整字段见 OpenAPI。

## 3. 幂等提交 {#submit}

业务侧先保存自己的执行记录、外部执行 ID、幂等键、固定契约与请求快照。准备 [投递模板](/downloads/submission-template.json)，填入真实产品及输入后保存为 `submission.json`。

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $BIOWORKFLOW_IDEMPOTENCY_KEY" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs" \
  --data-binary @submission.json
```

幂等键、`external_ref.external_run_id` 与请求体由业务后端生成并持久化。同一次分析的网络重试保持完全一致；新的独立分析才使用新 ID。`subject.sample_id` 使用脱敏标识。

首次接受返回 201；相同请求重放返回 200 和 `Idempotency-Replayed: true`。保存响应的 `id` 为平台运行 ID。相同标识对应不同内容会返回 `409 IDEMPOTENCY_CONFLICT`，不能换 ID 掩盖冲突。

## 4. 找回与同步状态 {#status}

投递超时或响应丢失后，先按原外部执行 ID 找回：

```bash
curl --fail-with-body -sS --get \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/by-external-ref" \
  --data-urlencode "external_run_id=$BIOWORKFLOW_EXTERNAL_RUN_ID"
```

找不到时仍可能与首次提交并发，只能使用原幂等键、原请求重试。后续在业务后端通过 `GET /analysis-runs/{run_id}` 持久轮询，设置超时和有上限的退避，服务重启后继续同步。

保存递增的 `status_version`，避免旧响应覆盖新状态。`queued/preparing/running` 继续等待；`cancel_requested` 不等于已取消；`failed/canceled` 为终态。`succeeded` 后仍要校验输出。未知状态保留原值并记录问题。

列表只返回最近最多 200 条摘要，不能用于重建全部历史；列表中的空 outputs 不等于任务没有结果。可选 Webhook 需要验签、去重和乱序处理。

## 5. 校验并接收结果 {#outputs}

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/$BIOWORKFLOW_RUN_ID/outputs"
```

分别读取 `execution_status` 与 `output_status`。按产品的语义键、格式和必需项消费 `results`，不要猜测磁盘目录。

| output_status | 处理方式 |
| --- | --- |
| pending | 继续同步 |
| complete | 检查必需结果，下载并校验 |
| incomplete | 记录缺失项；仅接收已验证的可用项 |
| unavailable | 保留错误证据，不遍历内部路径绕过接口 |

File 项按 `download_url` 下载，核对 `size` 与 `sha256`；值类型按契约解析，目录项不能当作单个文件。相对下载链接基于 API 所在 origin 解析，不能再次拼接 Integration 前缀。只把 Token 发给可信的同源 Integration 地址。

业务侧分别记录分析执行、结果接收/解析和业务入库状态。重复同步不重复入库；业务解析失败也不能改写平台执行终态。

## 错误与兼容 {#errors}

按 HTTP 状态及 `error.code` 分支，不解析中文 message。401/403 检查 Token 与 scope；输入、产品与幂等冲突先修复原因；仅对可重试网络/资源问题进行有界重试。

新增能力显式启用；产品版本不可原地改绑。底座升级前验证旧请求和既有产品。完整接口定义为 [OpenAPI 1.7.0](/openapi.json)，该文件取自固定发布提交。
