# 分析服务接入指南与契约

这份文档面向把 BioWorkflowManage 接入自己项目的开发者和 AI。它同时是**使用指南和双方契约**：
不仅说明如何调用，还规定平台保证什么、接入方必须怎样处理，以及后续升级不能改变什么。
OKbox、血液肿瘤项目及未来业务系统使用同一套约定，不按客户名称分叉核心协议。

优先场景是把平台作为独立分析服务，随业务项目一起部署；连接已有在线分析平台使用相同接口。
不需要先有正式软件 Release。本指南不表示所有生信流程已经验收可用。

## 1. 接入边界与稳定性承诺

业务后端通过 `/api/v1/integration` 调用分析服务，浏览器只访问业务后端。平台自带管理页面不是
业务用户的必需入口；用户、订单、样本业务和结果展示继续留在业务系统。

| 边界 | 平台承诺 | 接入方必须遵守 |
| --- | --- | --- |
| 身份 | Service Account 隔离任务，按 Token scope 授权 | 每个系统/环境独立账号；Token 只放后端，不复用网页登录 Cookie |
| 流程 | 固定 `analysis_code + contract_version`，运行保存契约和源码证据 | 固定已确认的产品版本，不自动选最新版、不提交任意脚本/镜像 |
| 数据 | 校验受管文件/对象及资源，领取任务时复核 | 提供平台可读取的不可变引用；提交路径不等于上传文件 |
| 投递 | 相同幂等请求复用同一任务，冲突明确拒绝 | 先持久化外部执行 ID、幂等键及请求，再发送；超时不换 ID 重投 |
| 状态 | 异步任务有明确状态及递增的 `status_version` | 后端持久轮询或消费通知，不能用 HTTP 超时判断流程失败 |
| 结果 | 只公开契约内结果与下载证据；执行和交付状态分开 | 按语义键消费并校验结果，独立记录下载/解析/入库状态 |
| 演进 | 不静默破坏旧契约；新能力优先显式可选 | 不依赖 ORM、数据库表、内部目录或管理页面私有接口 |

MiniWDL 与 Nextflow 使用同一投递和结果契约；具体引擎由已发布产品决定。引擎兼容不意味着
WDL 和 Nextflow 脚本可以互换，也不意味着任意流程的输入字段相同。

本指南定义接入行为，完整字段由 [OpenAPI](../schemas/integration-openapi-v1.json) 定义，
具体流程参数和结果由该产品契约定义。[接口详细说明](14-integration-api-and-mcp.md)覆盖扩展能力；
[兼容开发约定](22-integration-contract-governance.md)规定平台怎样维护这些承诺。
如果三者冲突，应作为缺陷核实并修正，不能让接入方猜测或静默改变旧行为。

## 2. 部署与配置交接

### 2.1 随业务项目部署（优先）

保持两个独立服务：业务系统有自己的数据库和页面；分析平台有自己的数据库、API、后台任务和
执行 Worker。通过受控网络通信，不把 Django 模块嵌入业务进程，也不共用数据库。
Service Account 的任务隔离不等于任意流程的安全沙箱或原始数据的物理租户隔离。
只运行受信任的已发布流程；不同客户的数据隔离要求须在目录授权和部署边界中单独落实。

平台运维按根目录 [启动说明](../README.md#本地运行)和 [.env.example](../.env.example)配置。
仅启动 API 不会执行分析；MiniWDL 必须再选择 `wdl-runtime` 或 `wdl-host-runtime`，详见
[执行环境说明](12-miniwdl-execution.md)。不需要 Nextflow 时不用启用 `nextflow-runtime`。
不要求把管理控制台暴露给终端用户。需要无界面组件交付时使用
[Analysis Node 部署说明](19-analysis-node-deployment.md)；该交付目录需要对应镜像清单，不能当作
未准备镜像的源码启动捷径。

平台侧与对接有关的现有配置主要是：

| 平台配置 | 部署时确认 |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | 接入方实际调用的域名/服务别名；不要用关闭认证代替配置 |
| `ANALYSIS_RAWDATA_HOST_PATH` / `ANALYSIS_RAWDATA_EXECUTION_ROOT` | 原始数据宿主目录与执行路径；API、索引器和 Worker 必须看到同一批文件 |
| `ANALYSIS_DATABASE_HOST_PATH` / `ANALYSIS_DATABASE_EXECUTION_ROOT` | 已发布产品需要的参考库/Panel |
| `ANALYSIS_RUN_HOST_PATH` / `ANALYSIS_RUN_EXECUTION_ROOT` | 平台专用结果持久目录，不供上游直接遍历 |
| `ANALYSIS_INPUT_STAGING_HOST_PATH` / `ANALYSIS_INPUT_STAGING_EXECUTION_ROOT` | 使用对象输入时的校验暂存目录 |
| `ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH` | 可选 S3/MinIO profile，凭据由部署侧管理，不进入任务 JSON |

宿主 Docker 模式的路径一致性要求见 [.env.host.example](../.env.host.example)；不要直接把另一台
服务器的 `.env` 覆盖进来。业务侧可以写入数据投递目录，平台对原始数据和参考库只读挂载。
共享挂载的路径映射由部署方确认，不能让业务请求自选宿主目录。

### 2.2 连接已有在线平台

只替换分析 API 地址、该环境的服务凭据和受管数据映射，不改业务投递/结果协议。
跨服务器必须事先有共享存储，或启用受支持的 S3/MinIO 对象输入；接口不能读取调用方电脑上的路径。
生产连接使用 HTTPS。受控内网 HTTP 仅用于明确接受明文风险的调试，不能据此关闭认证或全局安全设置。
Reference Connector 自身要求 HTTPS 或 loopback 地址，部署时也要遵守其限制。

### 2.3 接入前必须取得的信息

以下是**建议在上游后端使用的配置名**，不是平台自动读取的一组新环境变量：

| 上游配置 | 示例/含义 |
| --- | --- |
| `BIOWORKFLOW_API_BASE_URL` | `https://analysis.example.test/api/v1/integration`，包含整个前缀，无末尾 `/` |
| `BIOWORKFLOW_TOKEN` | 由平台管理员签发的服务 Token，从密钥管理或受限环境变量读取 |
| `BIOWORKFLOW_ANALYSIS_CODE` | 已发布的产品代码，不是 WDL 文件名 |
| `BIOWORKFLOW_CONTRACT_VERSION` | 固定产品版本，不填 `latest` |
| `BIOWORKFLOW_CONTRACT_DIGEST` | 对接确认的产品契约摘要，用于核对固定契约是否一致 |

另需交接：数据目录相对路径约定或对象 profile、输入字段及基数、必需参考资源、输出语义键/格式、
结果保留周期和故障联系人。保留策略是部署约定，API 不承诺结果永不清理。
一个业务系统可以配置多个产品，例如单样本和配对分别固定版本，不需要多个私有 API。

最小闭环需要 `workflow:read`、`analysis:submit`、`analysis:read`、`analysis:download` 四个 scope。
取消、重跑、导出、交付确认按需额外授权。账号创建与轮换由平台管理员按
[Service Account 说明](14-integration-api-and-mcp.md#2-service-account-与-token)完成；不要把创建账号
作为业务请求的一部分。Token 过期/轮换不应改变任务与业务记录的对应关系。

下面的 curl 均在业务后端环境执行，先安全注入上述变量；不要把真实 Token 写进脚本、日志或对话。
示例不自动跟随重定向，客户端也不能把 Authorization 转发到未知地址。

## 3. 最小接入闭环

### 3.1 读取并固定产品契约

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-products"

curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-products/$BIOWORKFLOW_ANALYSIS_CODE/versions/$BIOWORKFLOW_CONTRACT_VERSION"
```

读取固定产品的 `input_contract`、`output_contract`、`contract_digest`、`ready` 和 `blockers`。
核对摘要与接入配置一致，只对已具备运行条件的产品继续；未发布/未就绪时请平台侧补齐，
不能绕过产品契约调用内部运行接口。任务接受后还应保存响应中的实际产品与源码证据。

### 3.2 准备平台能够读取的输入

受管文件用逻辑根与相对路径引用，不传宿主绝对路径：

```json
{"root_alias": "rawdata", "relative_path": "run-001/S001_R1.fastq.gz"}
```

这个引用不上传文件。先完成复制并确认文件不再变化，再预检投递；不能边复制边运行。
通过 `GET /rawdata-datasets` 可查询已索引的配对数据，但**配对齐全不等于复制完成**。
人工指定输入路径也可以，不强制所有业务使用自动发现。

如果采用现有复制完成协议，在复制结束后由数据投递方建立 `_READY.done`，调用：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  "$BIOWORKFLOW_API_BASE_URL/rawdata-datasets/readiness" \
  --data '{"files":["run-001/S001_R1.fastq.gz","run-001/S001_R2.fastq.gz"]}'
```

只有响应 `submission_allowed=true` 才可按该协议提交；把返回的 `snapshot` 原样放入后续预检和
投递的顶层 `rawdata_readiness`。不要自己构造摘要或修改快照。`waiting/unknown/issue/changed/unbound`
不是可投递状态。`include_unready=true` 可用于目录页展示这些信息；省略参数保持旧目录响应。
标记、索引、快照复核及批量查询详见 [复制完成协议](rawdata-ready-marker.md)。
此能力显式可选，没有携带快照的历史请求仍保持原规则；标记不是 FASTQ 全量内容校验的替代品。

S3/MinIO 输入遵守 [对象引用协议](14-integration-api-and-mcp.md)：使用部署侧 profile、bucket/key、
版本或 ETag、大小和 SHA-256；不能提交任意下载 URL、云密钥或自动退回最新对象。

### 3.3 预检，不创建任务

仓库提供可修改的 [预检样例](../examples/integration-quickstart/lc103-preflight.json)和
[投递样例](../examples/integration-quickstart/lc103-submission.json)。在仓库根目录执行：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/preflight" \
  --data-binary @examples/integration-quickstart/lc103-preflight.json
```

样例使用仓库已有的 `lc103-amp / 1.2.0` 契约形状，部署仍须事先发布该产品并准备真实测试数据。
路径和样本 ID 是占位数据，不会因运行这个命令而自动生成文件。
LC103 的 `read1/read2` 是顺序配对的 `Array[File]`，单 lane 也必须是数组；其他产品按自己的
`input_contract` 填写，不能照搬 LC103 字段到血液单样本/配对流程。

预检成功读取 `submission_allowed`、`ready`、`waiting_for` 和 `checks`。
`ready=false` 但 `submission_allowed=true` 可以表示资源暂不足、允许排队，不要误判为输入错误。
预检不锁住文件，也不预留执行资源；提交和 Worker 领取时还会复核。

### 3.4 持久化业务映射，再幂等提交

业务后端先创建本地执行记录，至少保存：本地任务 ID、`external_run_id`、幂等键、固定产品及摘要、
完整投递请求或可重建的不可变快照。一次实际分析一个执行 ID；网络重试不能重新生成。
示例 JSON 中的 `demo-execution-001` 只用于同一测试请求，多次独立运行必须换新 ID。

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-execution-001" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs" \
  --data-binary @examples/integration-quickstart/lc103-submission.json
```

首次成功为 HTTP `201`，同一请求重放为 `200` 并带 `Idempotency-Replayed: true`。
保存响应 `id` 为平台运行 ID；接收成功只代表已经创建异步任务，不代表流程完成。
`external_ref.client_id` 可省略并由服务身份决定，不允许伪装其他系统。
仅传执行必需的脱敏样本标识，不传患者姓名等临床身份字段。

同一幂等键或外部执行 ID 改了请求，平台返回 `409 IDEMPOTENCY_CONFLICT`。接入方应找回并核对
原任务，不能自动换 ID 掩盖冲突或改投另一个引擎。

### 3.5 超时找回与状态同步

投递请求超时或响应丢失后，先按原外部执行 ID 查询：

```bash
curl --fail-with-body -sS --get \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/by-external-ref" \
  --data-urlencode 'external_run_id=demo-execution-001'
```

查询仍可能与首次提交并发；找不到时只用**同一幂等键和同一请求内容**重试。
找回后用 `GET /analysis-runs/{run_id}` 持续同步状态。轮询放在业务后端的持久任务中，
设置请求超时和有上限的退避，服务重启后能恢复；不让浏览器持有几小时的 HTTP 连接。

| 平台状态 | 接入方展示与处理 |
| --- | --- |
| `queued`、`preparing`、`running` | 等待/准备/运行中，继续同步 |
| `cancel_requested` | 取消处理中，不当作已经取消 |
| `succeeded` | 执行成功，继续检查输出是否完整并接收结果 |
| `failed`、`canceled` | 分析终态，记录错误/取消信息；不自动创建新分析 |

保存 `status_version`，防止晚到的响应覆盖较新状态。遇到未知状态保留原值并告警，不猜成成功。
`progress` 用于展示，不能替代状态判定。`GET /analysis-runs` 是最多 200 条的摘要列表，
其中 `outputs=[]` 不代表没有结果；恢复单个任务应读详情而不是仅遍历摘要列表。

首期轮询就足够。需要通知时按[签名 Webhook 约定](14-integration-api-and-mcp.md)验签并处理
重复/乱序，通知用于驱动同步，不把业务入库成功与消息送达混为一谈。

### 3.6 取得、校验并展示结果

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $BIOWORKFLOW_TOKEN" \
  "$BIOWORKFLOW_API_BASE_URL/analysis-runs/$BIOWORKFLOW_RUN_ID/outputs"
```

`BIOWORKFLOW_RUN_ID` 是上一步响应的 `id`。响应包括 `execution_status`、`output_status`、`error`
及 `results`。必须区分以下状态：

| 输出状态 | 处理约定 |
| --- | --- |
| `pending` | 尚未形成终态输出，继续同步 |
| `complete` | 按必需语义输出进行下载、校验和业务解析 |
| `incomplete` | 分析执行可能已成功，但结果不完整；标记缺失，已验证文件仍可按单项接收 |
| `unavailable` | 结果不可用，记录原因并联系运维，不遍历内部路径绕过接口 |

每项按 `key`、`semantic_type`、`kind` 和产品 `output_contract` 选择处理器；不要按目录位置猜测。
只有可下载的 File 项使用 `download_url`，值类型直接按契约解析，目录项不能当作文件下载。
相对下载地址相对于 API 所在 origin 解析（不是再次拼接 Integration 前缀），只允许受信任的同源
Integration 地址。下载时仍需服务 Token，不跟随可能泄露凭据的跨域重定向。
接收后核对 `size` 和 `sha256`，校验失败不能作为成功结果入库。

LC103 的语义输出包括 `report.qc_xlsx`、`report.variants_tsv`、`report.qc_tsv`、`report.snv_tsv`，
具体字段/列按该版本[产品说明](21-nextflow-lc103.md)消费。通用 API 不替业务解释变异或给出临床结论。
前端下载由业务后端鉴权代理或使用业务系统自己的受控存储，不向浏览器暴露平台 Token。

业务侧分别记录“分析执行”“结果接收/解析”“业务入库”状态。解析失败不能把平台 `succeeded`
改成执行失败；重复轮询/回调不得重复插入结果。大结果可选用 Artifact Export，导出状态和确认规则见
[详细接口](14-integration-api-and-mcp.md)，最小闭环不强制配置它。

## 4. 错误、升级与兼容约定

- 按 HTTP 状态、`error.code` 和存在时的 `category/retryable` 分支，不解析中文 `message`。
  代理错误可能不是 JSON；记录可诊断信息但不记录 Token 或敏感数据。
- `401/403` 先检查 Token 有效期、scope 和目标环境，不无限重试；不关闭认证。
- 输入/产品/幂等冲突先修复原因；网络/资源暂时不可用可以有界退避，但不得因此重复执行。
  `retryable=true` 不是自动授权新建重跑；重跑要有业务决定、新 ID 和所需权限，保留旧记录。
- 忽略开放响应对象中的未知可选字段；闭合对象按 OpenAPI 严格处理。请求不发送未声明字段，
  未知状态和错误采取可诊断的兜底处理，不能无条件重试。
- 产品版本、API 文档修订号和软件 Release 互不等价。平台不能原地改变旧产品的输入、默认算法、
  结果含义或绑定流程；产品升级由接入方明确选择新版本。
- 平台继续支持旧契约；增加新接口、上游或基线不自动废弃旧行为。无法保持兼容时先并行提供新契约、
  明确迁移和退场计划。严重安全/结果正确性事件可以紧急阻断，但必须说明影响和恢复办法并保留证据。

## 5. 给开发者或 AI 的实施清单

把本页、当前 OpenAPI 和选定产品契约一起交给实现者。可直接使用以下任务描述：

> 为本业务项目接入 BioWorkflowManage，遵守《分析服务接入指南与契约》和 Integration OpenAPI。
> 默认把它作为随项目部署的独立服务，也支持通过同一协议连接已有在线服务。
> 在后端实现产品版本配置、输入映射、持久任务记录、幂等投递、超时找回、状态同步与结果校验入库；
> 前端只展示本系统的数据投递和分析结果。凭据不进前端，不共享平台数据库，不调用管理页面私有接口。
> 未发布产品或缺少流程/存储配置时明确报告缺口，不臆造字段、版本或成功结果。首期采用轮询，
> 可选能力只在明确配置后启用。修改 API 前先在平台契约中定义兼容行为。

最小验收必须覆盖：

1. 独立机器账号以最小 scope 读取并核对固定产品；无效/越权凭据被拒绝。
2. 脱敏或合成小数据预检、投递、状态同步；重复请求不重复运行，响应丢失能找回。
3. 成功、失败、输出不完整和业务解析失败分别展示；下载校验并幂等入库。
4. 业务服务重启后恢复未完成任务；平台内部升级后同一旧请求仍能通过回归。
5. 使用复制完成协议、回调或导出时，额外验证其等待/变更、重复/乱序、失败/重试情形。

可复用 [Reference Connector](20-reference-connector.md)及其
[Python/Java/curl/Postman 示例](../examples/reference_connector/README.md)。它是可选的业务适配器，
它自己的 `/v1/orders` 等接口不是平台的 Integration API，不要混用两套地址或 Token。

当前已有 LC103 产品清单可作形状参考；血液肿瘤单样本/配对已有 WDL 源码，但
[资源与输入适配仍待完成](15-analysis-resource-catalog.md#下一阶段)，不能声称已可直接上线。
本页和契约测试通过只表示接入约束有据可查，不替代具体流程的小数据实跑、结果审核和部署验收。

## 可选：独立数据管理基础服务

需要多个业务系统共用数据管理时，按[数据服务接入与迁移](24-data-service-extraction.md)切换。BWM 继续提供现有 rawdata 兼容接口，业务方也可直接调用独立数据 API；分析投递、状态和产物仍使用本文的 BWM 契约。
