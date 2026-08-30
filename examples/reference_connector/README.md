# BioWorkflowManage Reference Connector

这是一个零第三方运行时依赖的 Python 参考组件，用来把 MES/运营平台的业务订单映射为稳定的
BioWorkflowManage Analysis Request。客户字段映射、业务字段白名单和输入路径规则留在 Connector；
Analysis Node 核心接口不包含客户定制逻辑。

Connector 会完成产品发现与契约摘要核对、preflight、幂等提交、超时找回、状态同步、签名 Webhook
去重、结果 SHA-256 校验，以及大结果 Artifact Export 清单确认。SQLite 保存外部任务、状态版本、事件
去重键和结果回执；重复业务请求不会创建第二个 AnalysisRun。

## 快速运行

要求 Python 3.12。先复制配置，并把 `expected_contract_digest` 替换成产品目录返回的真实摘要：

```bash
cd examples/reference_connector
cp config.example.json config.json

export BIOWORKFLOW_TOKEN='<Analysis Node Service Token>'
export BIOWORKFLOW_WEBHOOK_SECRET='<manage_webhook_endpoint 输出的 SIGNING_SECRET>'
export REFERENCE_CONNECTOR_INBOUND_TOKEN='<至少 32 字符的独立随机值>'

python -m reference_connector --config config.json products
python -m reference_connector --config config.json serve
```

三个密钥只从环境变量读取，不得写入 JSON、镜像、Git 或日志。`config.example.json` 中的全零
`expected_contract_digest` 是故意设置的失败关闭占位符，必须在投产前替换。

交付验收可使用 `config.analysis-node-smoke.example.json` 和
`analysis-node-smoke-order.example.json` 对 Analysis Node 内置可信 smoke product 做真实闭环；订单中的
probe 路径必须先在部署的 `rawdata` 根下创建，不能直接照搬占位路径。

容器方式：

```bash
cp config.compose.example.json config.compose.json
docker compose -f compose.example.yml up --build -d
curl -fsS http://127.0.0.1:8090/health
```

`config.compose.example.json` 只把容器内监听地址设为 `0.0.0.0`，Compose 仍仅向宿主
`127.0.0.1:8090` 发布端口；直接运行 Python 时继续使用默认监听 loopback 的 `config.example.json`。

Analysis Node 离线交付包已经包含 Python runtime 镜像，不需要联网重建 Connector：

```bash
cd reference-connector
cp config.offline.example.json config.json
docker compose --env-file ../images.env -f compose.offline.yml up -d
```

运行前仍需在当前 shell/密钥系统注入三个环境变量，并把配置中的 Analysis Node URL 与产品摘要改成
真实值。离线方式必须使用状态路径固定到 `/var/lib/reference-connector` 的
`config.offline.example.json`；`compose.offline.yml` 复用交付包已校验和加载的 smoke-task Python 3.12 镜像；
`compose.example.yml` 则用于独立获取源码后的常规构建。离线 Compose 面向 Linux 同机部署，使用
host network 访问 Analysis Node 默认只绑定宿主 loopback 的 API；把 `analysis_api.base_url` 设置为
`http://127.0.0.1:<ANALYSIS_NODE_API_PORT>/api/v1/integration`，并保持 `listen.host=127.0.0.1`。

源码 Compose 默认只把端口发布到主机 `127.0.0.1`，离线 Compose 则直接只监听宿主 loopback。
跨主机调用时应由客户现有网关终止 TLS、限制来源网络，并使用 HTTPS；不要把 Bearer Token 发往
非 loopback 的明文 HTTP 地址。

## Connector HTTP 边界

除健康检查和 BioWorkflowManage 签名 Webhook 外，所有路由都要求：

```text
Authorization: Bearer <REFERENCE_CONNECTOR_INBOUND_TOKEN>
```

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 无鉴权存活检查，不探测上游 |
| `GET` | `/v1/products` | 查看 Analysis Node 产品目录 |
| `POST` | `/v1/orders` | 映射、预检并幂等提交 MES 订单 |
| `GET` | `/v1/orders/{external_run_id}` | 读取 Connector 本地状态与结果回执 |
| `POST` | `/v1/orders/{id}/reconcile` | 从 Analysis Node 对账并按版本推进状态 |
| `POST` | `/v1/orders/{id}/results` | 下载小结果并校验大小与 SHA-256 |
| `GET` | `/v1/orders/{id}/outputs/{key}` | 向 MES 流式交付已验证的小文件 |
| `POST` | `/v1/orders/{id}/exports` | 为大结果创建 Artifact Export |
| `POST` | `/v1/orders/{id}/complete-export` | 校验 export manifest 并按需确认 |
| `POST` | `/v1/webhooks/bioworkflow` | 接收 Analysis Node HMAC-SHA256 Webhook |

完整 curl 顺序见 [curl-flow.md](curl-flow.md)，Postman 集合位于
[`postman/Reference-Connector.postman_collection.json`](postman/Reference-Connector.postman_collection.json)。

## MES 调用示例

Python 3.12：

```bash
export REFERENCE_CONNECTOR_URL=http://127.0.0.1:8090
python clients/python_mes_client.py submit mes-order.example.json
python clients/python_mes_client.py status MES-20260830-001
python clients/python_mes_client.py reconcile MES-20260830-001
python clients/python_mes_client.py results MES-20260830-001
python clients/python_mes_client.py download MES-20260830-001 reference.result ./result.tsv
```

Java 17：

```bash
javac -d /tmp/reference-connector-java clients/MesConnectorClient.java
java -cp /tmp/reference-connector-java MesConnectorClient download \
  MES-20260830-001 reference.result ./result.tsv
java -cp /tmp/reference-connector-java MesConnectorClient submit mes-order.example.json
java -cp /tmp/reference-connector-java MesConnectorClient status MES-20260830-001
```

调用方可以在网络超时后重发完全相同的订单，但不能修改同一 `external_run_id` 的内容。Connector
不会自动跟随 HTTP 重定向，避免 Bearer Token 离开固定 origin。

## 定制规则

只修改 `mapping`：

- `fields` 指定 MES 的任务 ID、分析 ID 和样本 ID 来源；
- `inputs` 显式列出允许进入 Analysis Request 的输入；`managed_file` 只接受 `rawdata` 或
  `database` 下的安全相对路径；
- `metadata` 是向平台转发的业务字段白名单；未列出的患者和客户私有字段不会离开 Connector；
- `database` 只映射 `reference_id` 与 `panel_id`；
- `analysis_product` 固定 `analysis_code + contract_version + contract_digest`。

客户定制应新增或修改自己的 Connector 配置/适配层，不应向 BioWorkflowManage 核心模型加入 MES
字段。更完整的架构、错误矩阵、上线清单和故障恢复策略见仓库文档
`docs/20-reference-connector.md`；Analysis Node 离线交付包内同一文档名为
`reference-connector/INTEGRATION-GUIDE.md`。
