# v1.0.0 稳定版本发布与升级

## 发布边界

本稳定版本固定以下产品主链路：WDL 资产与工具包维护、多人评审与行级讨论、不可变版本
发布、原始数据后台索引、资源清单、运行分析、工具单测和运行结果审计。此后小版本只做
修复与打磨；新增核心模型、一级菜单或执行模式必须单独立项并提供迁移和回滚方案。

## 升级顺序

升级包含数据库迁移 `0023` 至 `0029`，新增 WDL 协作、原始数据索引、WDL 发布证据表、
共享登录限速桶、稳定分析产品契约和 Webhook Outbox/投递审计表，并约束同一用户在同一资产上
只能保留一个待解决源码冲突。
迁移只新增表与约束，不改写已有 WDL revision、运行记录或原始数据。
迁移不会自动把历史 WorkflowVersion 暴露为分析产品；管理员必须按
[`14-integration-api-and-mcp.md`](14-integration-api-and-mcp.md) 显式发布产品契约。

```bash
# 1. 备份 PostgreSQL；备份文件必须落在项目数据目录之外
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-bioworkflow}" \
  -d "${POSTGRES_DB:-bioworkflow}" -Fc > /path/to/backup/bioworkflow-before-stable.dump

# 2. 停止会创建运行记录或索引记录的 worker
docker compose stop analysis-worker rawdata-indexer webhook-dispatcher
docker compose --profile wdl-host-runtime stop analysis-worker-host

# worker 停止后确认没有 queued/preparing 运行；有则通过运行页/API 取消，升级后重新投递
# 旧输入清单缺少 ctime 或目录身份摘要，升级后的 worker 会失败关闭而不会继续执行

# 3. 拉取稳定 tag 后构建并执行迁移
docker compose build backend frontend
docker compose up -d db backend
docker compose exec backend python backend/manage.py migrate

# 4. 在挂载可写 runs 目录的一次性 worker 中回填（先计数，再执行）
# 普通 wdl-runtime 部署：
docker compose --profile wdl-runtime run --rm --no-deps analysis-worker \
  python backend/manage.py backfill_analysis_output_manifests --dry-run
docker compose --profile wdl-runtime run --rm --no-deps analysis-worker \
  python backend/manage.py backfill_analysis_output_manifests --actor stable-upgrade

# 宿主 Docker 部署改用 analysis-worker-host：
# docker compose --profile wdl-host-runtime run --rm --no-deps analysis-worker-host \
#   python backend/manage.py backfill_analysis_output_manifests --actor stable-upgrade

# 5. 启动页面、后台索引、Webhook dispatcher 和所选执行 worker
docker compose up -d frontend gateway rawdata-indexer webhook-dispatcher
docker compose --profile wdl-host-runtime up -d analysis-worker-host
```

`--dry-run` 只统计候选和输出游标，不读取或校验历史输出。正式回填会先校验旧 File SHA-256；
不一致时非零退出且不覆盖原证据。旧 Directory 没有摘要时会在
provenance 中记录为升级时点基线。可用 `--limit N` 分批扫描；命令输出的“最后 ID”可作为下一批
`--after-id` 的游标，但只有命令零退出时才能推进游标；任一条失败时先修复原因，再从上一批
游标幂等重跑，不能跳过失败 ID。未完成回填的历史输出返回 409，不退回未校验下载。

运行完成时，worker 会把每个 File 输出复制到运行目录下的内容地址快照
`.verified-outputs/<sha256>`，因此最坏情况需要接近原 File 输出总量的额外空间。固化时校验内容与文件身份；
下载请求只对源文件和快照做 O(1) 身份校验，流式返回快照并使用 SHA-256 作 ETag。backend
继续只读挂载 runs；成功后不得原地修改源输出或 `.verified-outputs`。快照项数、总字节、目录累计
条目数、总耗时、最小剩余空间和 manifest 嵌套深度由 `.env.example` 中的 `ANALYSIS_OUTPUT_*`
参数限制；重复引用同一目录会复用本次完成阶段的已确认摘要，超限会将输出标记为 incomplete。
内联 JSON 输出另受 `ANALYSIS_OUTPUT_VALUE_MAX_BYTES` 限制；单项超限只将该项标记为
`unverifiable`，同一 v2 清单中已经固化且身份有效的 File 仍可下载。列表接口不返回输出正文，
调用方应通过详情或输出清单接口读取完整结果。
miniwdl `result.json` 还受 `ANALYSIS_RESULT_JSON_MAX_BYTES` 限制，解析前先检查并稳定读取文件，
避免超大结果 JSON 在 manifest 预算生效前耗尽 worker 内存。
FASTQ/压缩 FASTA 会在交给 gzip 解压器前先按 `ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES` 验证
首个 gzip member 的可选字段，避免无终止 FNAME/FCOMMENT 绕过请求时间预算。

正式 HTTPS 环境必须保持 `DJANGO_SESSION_COOKIE_SECURE=1` 与
`DJANGO_CSRF_COOKIE_SECURE=1`；只有直接使用 HTTP 的本地开发环境才可显式改为 0。登录失败限速由
`DJANGO_LOGIN_THROTTLE_RATE` 控制。计数保存在数据库共享桶中，因此重启或切换 Web worker 不会
重置当前窗口；`DJANGO_LOGIN_THROTTLE_RETENTION_DAYS` 控制过期桶保留天数，登录请求会按索引清理。
`DJANGO_TRUSTED_PROXY_COUNT` 必须等于 backend 前可信代理层数：
默认 Compose gateway 为 1；外层再有 TLS 反向代理时通常为 2，部署者应按真实链路核对，避免信任
客户端伪造的 `X-Forwarded-For`。

生产 Webhook 部署应在升级前生成并安全保存独立 `WEBHOOK_SIGNING_KEY`，确保 backend 运维命令
与所有 `webhook-dispatcher` 实例使用同一个值。默认只允许公网 HTTPS；内网 endpoint 必须通过
`.env` 的 `WEBHOOK_PRIVATE_HOST_ALLOWLIST` 精确放行，不能使用通配符。迁移后先注册测试 endpoint，
完成一次终态运行并确认 `webhook_delivery_stats` 中 delivery 进入 `delivered`，再启用生产订阅。

启用 S3/MinIO 输入前，先创建并授权暂存目录与 profile secret 目录：

```bash
install -d -m 0750 ./data/input-staging ./secrets/object-storage
chown -R "${MINIWDL_UID:-1000}:${MINIWDL_GID:-1000}" ./data/input-staging
chmod 0640 ./secrets/object-storage/*.json
```

每个对象存储 profile 只授予白名单 bucket 的只读 object/version 权限，并配置精确的
按 Service Account 绑定 bucket/key prefix 的 `client_grants`；不要把凭据写入 `.env`、
任务 JSON 或 WDL。按实际部署验证 endpoint/CIDR 防火墙后，再用一个固定 VersionId/ETag、size 和
SHA-256 的小对象完成 preflight 与真实运行。
在 #32 的自动 retention/GC 合并前，还必须为 input staging 配置磁盘监控与人工排空窗口；
只可在停止所有 analysis worker 且确认没有排队/活跃任务后处理 `sha256/` 持久缓存，保留
`.leases/` 并且不得删除 Docker volume。

Workflow 列表现在默认每页 50、最大 100，并返回 `total`/`has_next`/`summary`。前端已显式翻页；
任何直接调用 `/api/v1/editor/workflows` 且假设无参数返回全集的旧客户端，必须在发布前改为传
`page`/`page_size` 并循环到 `has_next=false`。

运行列表 `GET /api/v1/analysis-runs`、`GET /api/v1/tool-test-runs` 和
`GET /api/v1/integration/analysis-runs` 返回 `view=summary`：保留状态、进度、时间和稳定错误码，
但 `outputs`、`request`、错误详情和 task timing 使用空摘要，避免历史大 JSON 放大列表响应。
需要这些字段的客户端必须按运行 ID 调用详情接口；这是一项升级时必须核对的客户端迁移。

首次启动 `rawdata-indexer` 会分批建立清单；原始文件只读挂载，不会被移动或修改。页面在
首次完整扫描前显示“正在建立清单”，之后始终优先展示最近一次完整成功快照。

## 验证清单

```bash
docker compose ps
docker compose exec backend python backend/manage.py showmigrations workflows
docker compose exec backend python backend/manage.py webhook_delivery_stats
curl -fsS http://127.0.0.1:${APP_PORT:-8082}/api/v1/ready
```

登录后确认：

1. 历史 WDL 详情可打开“协作”，能看到评审人、讨论和发布检查；
2. 原始数据页显示后台索引状态，点击“更新清单”后页面仍保留上次成功数据；
3. 运行分析能选择索引中状态为“可运行”的数据集；
4. `docker compose logs rawdata-indexer` 没有持续失败或权限错误；
5. 使用小数据完成一次运行后，WDL 发布检查能引用该运行并形成证据。

## 回滚

应用回滚前停止 `analysis-worker`、`analysis-worker-host`、`rawdata-indexer` 与
`webhook-dispatcher`。代码可回到
上一稳定 tag；新增表对旧代码无影响，因此正常回滚不执行逆向 migration，也不删除任何
表或 Docker volume。若必须恢复数据库，使用升级前的 `pg_dump` 在独立数据库中验证后再
切换，不能直接覆盖唯一生产副本。

## 发布检查契约

默认模板包含：语法与静态检查、imports 完整、工具包版本固定、当前 revision 已通过评审、
没有未解决讨论、当前 revision 有成功的小数据运行。管理员可调整启用项与小数据输入上限；
模板修改使用 `base_policy_version`，过期请求返回 409。

稳定发布必须携带 `base_version`、`base_digest` 与已通过的 `release_check_id`。服务端在事务
内复核源码和动态证据，任何变化都会拒绝发布；`WDLAssetRelease` 形成后不可修改或删除。
