# Nextflow LC103 Analysis Product

## 首期边界

- 流程：`01_amp_pipeline.nf`
- Panel：`LC103`
- 输入：一对受管 FASTQ（R1/R2）
- 输出：QC Excel、过滤后变异 TSV
- 外部调用：复用 Integration API / Analysis Product
- 前端：首期不实现

Nextflow 与 MiniWDL 共享任务状态、幂等、租约、输入完整性、取消、输出清单和 webhook；二者不共享 DSL 或进程启动参数。

## 导入固定执行包

仓库只保存产品清单，不复制私有流程源码。部署时从已审核且工作区干净的源码提交导入：

```bash
python backend/manage.py import_nextflow_product \
  --manifest examples/nextflow-lc103/product-manifest.json \
  --source-dir /data/06_project/okb/Workflow/amp_pipeline \
  --dry-run

python backend/manage.py import_nextflow_product \
  --manifest examples/nextflow-lc103/product-manifest.json \
  --source-dir /data/06_project/okb/Workflow/amp_pipeline \
  --publish-product \
  --actor deployment
```

Docker Compose 部署使用带 Git 的 Nextflow worker 镜像执行同一导入命令：

```bash
docker compose --profile nextflow-runtime run --rm --no-deps \
  -v /data/06_project/okb/Workflow/amp_pipeline:/source:ro \
  analysis-worker-nextflow \
  python backend/manage.py import_nextflow_product \
  --manifest examples/nextflow-lc103/product-manifest.json \
  --source-dir /source \
  --publish-product \
  --actor deployment
```

清单固定以下证据：

- 流程仓库完整 40 位 commit；
- 明确列出的 32 个源码/脚本/二进制资源；
- Nextflow 25.04.8；
- LC103 固定参数；
- 任务容器 `repo@sha256`；
- 输入适配器与输出 glob。

生产环境若启用 `INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE=1`，必须先对新建的 `WorkflowVersion` 完成现有 Sigstore attestation，再用 `manage_analysis_product` 发布产品版本。不要为绕过校验伪造 bundled attestation。

## 运行节点

Nextflow worker 使用独立的 TLS Docker-in-Docker daemon，并只领取 Nextflow 任务：

```bash
docker compose --profile nextflow-runtime up -d --build \
  nextflow-docker analysis-worker-nextflow
```

必须满足：

1. `ANALYSIS_RAWDATA_HOST_PATH`、`ANALYSIS_DATABASE_HOST_PATH`、`ANALYSIS_RUN_HOST_PATH` 和 `ANALYSIS_INPUT_STAGING_HOST_PATH` 都是宿主绝对路径，并以相同绝对路径挂载到 worker 与隔离 Docker daemon。
2. `ANALYSIS_DATABASE_HOST_PATH` 的根目录直接包含 `genome/`、`vep/`、`cosmic/`、`clinvar/`、`region/` 等 LC103 资源。
3. Docker daemon 已存在清单固定的容器 digest，或有权限从私有 registry 拉取。
4. API/worker 已应用数据库迁移。

`nextflow-registry` 是只发布到宿主回环地址的隔离镜像 mirror。当上游 registry 不可用、但宿主
已缓存清单固定的镜像时，可以按原 manifest 将镜像引入隔离 daemon；推送结果的 digest 必须与
产品清单完全一致：

```bash
docker compose --profile nextflow-runtime up -d nextflow-registry nextflow-docker
docker tag \
  dev.zgbio.net:38083/dr-pipeline-zg@sha256:e9f0c27bf09a5493c85e47adfe88780cada2ae04bcae078da353291793afe2ab \
  127.0.0.1:38084/dr-pipeline-zg:lc103-bootstrap
docker push 127.0.0.1:38084/dr-pipeline-zg:lc103-bootstrap
docker compose --profile nextflow-runtime run --rm --no-deps \
  analysis-worker-nextflow docker pull \
  dev.zgbio.net:38083/dr-pipeline-zg@sha256:e9f0c27bf09a5493c85e47adfe88780cada2ae04bcae078da353291793afe2ab
```

mirror 数据保存在已忽略的 `data/nextflow-registry/`，不接受局域网连接；DIND 到 mirror 使用隔离
网络内的 HTTP，但任务镜像仍必须匹配 `repo@sha256`，不允许用 tag 代替 digest。

Nextflow worker 会生成运行级 `fastq-list.csv` 和只读执行参数，不加载源码仓库的 `nextflow.config`，也不接受调用方传入任意 Nextflow 参数、配置文件、源码路径或容器镜像。Nextflow 子进程只继承 Java、Docker TLS 与基础 locale 所需的环境变量，不继承应用密钥。

## 验证

```bash
python -m pytest backend/tests/test_nextflow_runtime.py -q
python -m pytest backend/tests/test_analysis_runs.py backend/tests/test_integration_api.py -q
python backend/manage.py makemigrations --check --dry-run
docker compose config --quiet
```

真实 LC103 smoke 还应检查：

- okbox 预检与提交不包含执行引擎选择；
- MiniWDL worker 不领取 Nextflow 任务；
- 取消后没有保留带本次运行 label 的容器；
- QC Excel 与变异 TSV 均进入带 sha256 的输出清单；
- 重跑创建新任务且原任务证据不变。

## OKB 联合部署

同机部署时先创建专用网络，再用 override 只把 Analysis API 接入该网络：

```bash
docker network create bioworkflow-integration
docker compose \
  -f deploy/analysis-node/compose.yml \
  -f deploy/analysis-node/compose.okb.yml \
  --profile headless \
  --profile nextflow-runtime \
  up -d
```

OKB 通过 `http://bioworkflow-api:8000` 访问，不需要对客户局域网发布 Analysis API 端口。
两端的 `ANALYSIS_RAWDATA_HOST_PATH` 与 `DJANGO_DATA_HOST_DIR` 必须指向同一宿主机目录，
但数据库、运行目录和结果目录保持隔离。

为 OKB 签发最小权限 token 时直接写入新建的 0600 文件，避免 token 出现在终端日志：

```bash
docker compose \
  -f deploy/analysis-node/compose.yml \
  run --rm \
  -v /srv/okbox/secrets:/token-output \
  backend \
  python backend/manage.py manage_service_account \
  --client-id okb \
  --name OKB \
  --scope workflow:read \
  --scope analysis:submit \
  --scope analysis:read \
  --scope analysis:download \
  --scope analysis:cancel \
  --issue-token \
  --expires-days 90 \
  --token-name okb-primary \
  --token-output-file /token-output/bioworkflow-api-token.new
```

宿主机 `/srv/okbox/secrets` 需提前创建为仅部署账号可读写的目录。输出文件已存在时命令拒绝覆盖
并立即吊销本次新 token。轮换应写新文件、重启 OKB 并验证，
再通过旧 `TOKEN_PREFIX` 吊销旧 token。

## 回滚

1. 停用 `lc103-amp` Analysis Product，阻止新投递。
2. 等待或取消在途 Nextflow 任务。
3. 停止 `analysis-worker-nextflow` 与 `nextflow-docker`；保留隔离 daemon 的镜像缓存。
4. 保留不可变 WorkflowVersion、AnalysisRun、日志与输出证据，不删除迁移或运行目录。

MiniWDL worker 与已有 WDL 产品无需切换或回滚。
