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

清单固定以下证据：

- 流程仓库完整 40 位 commit；
- 明确列出的 32 个源码/脚本/二进制资源；
- Nextflow 25.04.8；
- LC103 固定参数；
- 任务容器 `repo@sha256`；
- 输入适配器与输出 glob。

生产环境若启用 `INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE=1`，必须先对新建的 `WorkflowVersion` 完成现有 Sigstore attestation，再用 `manage_analysis_product` 发布产品版本。不要为绕过校验伪造 bundled attestation。

## 运行节点

Nextflow worker 使用宿主 Docker，并只领取 Nextflow 任务：

```bash
docker compose --profile nextflow-runtime up -d --build \
  nextflow-docker analysis-worker-nextflow
```

必须满足：

1. `ANALYSIS_RAWDATA_HOST_PATH`、`ANALYSIS_DATABASE_HOST_PATH`、`ANALYSIS_RUN_HOST_PATH` 和 `ANALYSIS_INPUT_STAGING_HOST_PATH` 都是宿主绝对路径，并以相同绝对路径挂载到 worker 与隔离 Docker daemon。
2. `ANALYSIS_DATABASE_HOST_PATH` 的根目录直接包含 `genome/`、`vep/`、`cosmic/`、`clinvar/`、`region/` 等 LC103 资源。
3. Docker daemon 已存在清单固定的容器 digest，或有权限从私有 registry 拉取。
4. API/worker 已应用数据库迁移。

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

## 回滚

1. 停用 `lc103-amp` Analysis Product，阻止新投递。
2. 等待或取消在途 Nextflow 任务。
3. 停止 `analysis-worker-nextflow` 与 `nextflow-docker`；保留隔离 daemon 的镜像缓存。
4. 保留不可变 WorkflowVersion、AnalysisRun、日志与输出证据，不删除迁移或运行目录。

MiniWDL worker 与已有 WDL 产品无需切换或回滚。
