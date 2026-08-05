# miniwdl 校验与隔离执行框架

## 目标与边界

本框架负责四件事：

1. 用 miniwdl 对编译产物和执行案例做语法、类型静态校验；
2. 真实启动一个 task 容器，验证 miniwdl 到 Docker 和持久化输出的完整链路；
3. 为后续 FASTQ/FASTA 数据准备可重复的 preflight、运行目录和结果记录。
4. 由独立 Worker 执行运行页提交的受管历史 WDL，并把进度、事件和输出记录到数据库。

miniwdl 不在 Django 请求内运行。开发/CI 案例仍使用独立脚本；产品运行通过数据库队列
交给 `analysis-worker`。当前不包含取消、暂停、资源配额和执行节点多租户隔离。

## 运行分析页面

```bash
docker compose --profile wdl-runtime up -d backend frontend gateway miniwdl-docker analysis-worker
```

- 页面入口：`/runs`；
- 原始数据：`workspace/rawdata`，自动识别完整的 gzip FASTQ R1/R2；
- 参考数据库和 Panel：`workspace/databases`，清单见该目录的 `catalog.json` 和
  `README.md`；
- 运行结果：`data/analysis-runs/<run UUID>`；
- 每次运行固定到提交当时的历史 WDL revision 与工具包版本，之后修改流程不会影响
  已排队或已完成运行。

页面只允许数据库清单、FASTQ 配对和 WDL 静态检查全部通过后提交。配对流程还要求选择
另一组对照 FASTQ。

## 一键命令

```bash
# 无 Docker task，仅做所有 WDL 的静态检查
./scripts/miniwdl.sh check

# 创建测试数据目录和 expected-inputs.json，不生成或覆盖数据
./scripts/miniwdl.sh prepare

# 检查某个案例的 WDL 和数据；数据未补齐时会明确失败
./scripts/miniwdl.sh preflight fastp
./scripts/miniwdl.sh preflight fastp-bwa

# 启动隔离执行引擎并检查环境
./scripts/miniwdl.sh doctor

# 不需要业务数据，真实运行一个容器 task
./scripts/miniwdl.sh smoke

# 数据就绪后执行真实案例
./scripts/miniwdl.sh run fastp
./scripts/miniwdl.sh run fastp-bwa

# 停止执行引擎；不会删除案例数据、运行结果或镜像缓存
./scripts/miniwdl.sh stop
```

`miniwdl run_self_test` 会下载官方测试数据和镜像，因此只作为联网诊断命令：

```bash
./scripts/miniwdl.sh self-test
```

## 测试数据位置

### 初始化应用测试数据

新环境不需要复制本地 PostgreSQL 或大型参考数据库即可初始化可运行的测试流程：

```bash
docker compose run --rm backend python backend/manage.py seed_test_data
```

该命令幂等地创建默认测试用户和 3 个 Phase 1 示例流程。若同时需要导入历史实体瘤/血液肿瘤 WDL，先把 WDL 源码目录挂载到新环境，再执行：

```bash
docker compose run --rm \
  -v /path/to/tumor_wdl:/mnt/tumor_wdl:ro \
  backend python backend/manage.py seed_test_data \
  --wdl-source-dir /mnt/tumor_wdl \
  --repository git@gitea.kindstarzhenyuan.cn:zhuqin/minwdl.git \
  --revision <git-revision> \
  --actor zhuqin
```

参考数据库和原始测序数据不属于测试数据命令的提交内容。生产环境将
`ANALYSIS_DATABASE_HOST_PATH` 设置为 NAS 上的数据库目录即可，例如
`/mnt/nas/databases`；未设置时仍使用 `./workspace/databases`。

运行 `prepare` 后补入以下文件：

```text
data/miniwdl/work/cases/
├── fastp/inputs/
│   ├── sample_R1.fastq.gz
│   └── sample_R2.fastq.gz
└── fastp-bwa/inputs/
    ├── sample_R1.fastq.gz
    ├── sample_R2.fastq.gz
    └── reference.fa
```

文件名限定为安全的 ASCII 路径片段。preflight 会拒绝目录穿越和逃逸案例目录的符号
链接；还会检查文件非空、gzip 可读、首条 FASTQ/FASTA 记录结构，以及 R1/R2
首条 read ID 是否配对。该检查刻意不全量扫描大型文件，真实工具运行仍是最终验证。
输入只会被 miniwdl 以只读方式提供给 task。

## 案例含义

| 案例 | 使用的 WDL | 当前状态 |
| --- | --- | --- |
| `smoke` | `examples/miniwdl-execution/smoke/workflow.wdl` | 无数据即可真实运行 |
| `fastp` | 编译器生成的 `phase1-fastp/expected/workflow.wdl` | 无已知结构阻塞，待双端 FASTQ 实测 |
| `fastp-bwa` | `cases/fastp-bwa/run-ready.wdl` | 运行结构就绪，待双端 FASTQ 和 FASTA 实测 |
| fastp→BWA 编译产物 | `phase1-fastp-bwa/expected/workflow.wdl` | 静态有效，但暂不可真实运行 |

最后一项有两个已确认的运行阻塞：

- WDL 只声明一个 FASTA，没有声明或生成 BWA 的五个索引 sidecar；
- 所选 BWA 镜像没有 `samtools`，但 task 命令调用了它。

`fastp-bwa` 的执行验收版会在 BWA task 内生成索引，并用独立 samtools task 生成和
检查 BAM。它不会覆盖编译器 golden，也不代表上述编译缺陷已经修复。

## 隔离与持久化

Compose profile 关系如下：

```text
miniwdl-check (无网络、无 Docker)

miniwdl-runner / analysis-worker --mTLS 2376--> miniwdl-docker (隔离 DIND)
                 |                                  |
                 +-------- 相同绝对路径挂载 --------+
                              |
                 data/miniwdl/work 或 data/analysis-runs
```

- 日常 `docker compose up` 不会启动 miniwdl profile；
- runner 和 analysis-worker 都不挂宿主机 `/var/run/docker.sock`；
- Docker API 使用自动生成的 mTLS 客户端证书，只存在于 Compose 内部网络，不发布到
  宿主机；
- `data/miniwdl/work/runs/` 持久保存 `request.json`、解析后的输入、miniwdl
  结果、状态和 task 输出；
- `data/miniwdl-engine/` 保存隔离引擎的镜像层，避免每次重新下载；
- `data/miniwdl-certs/` 保存本机隔离引擎证书，目录权限为 `0700`，runner 只读挂载
  client 证书；
- runner 和 DIND 必须看到完全相同的绝对运行路径，这是 miniwdl 容器输入挂载的要求。
- analysis-worker 以非 root 用户运行，原始数据和数据库只读挂载，只有运行结果目录可写。

两个专用网络默认使用 `10.253.0.0/24` 和 `10.253.1.0/24`，避免依赖 Docker
已耗尽的默认地址池。如果宿主机或 VPN 已使用这些网段，可在 `.env` 中修改
`MINIWDL_CONTROL_SUBNET` 和 `MINIWDL_EGRESS_SUBNET`。

`miniwdl-docker` 需要 `privileged` 才能运行嵌套 Docker。它适合受信任的本地开发和
CI 验收，但不构成恶意 WDL 的安全沙箱。生产执行应迁到独立 worker/VM，并补齐认证、
队列、资源限制、审计和网络策略。

开发环境不会自动清理 `data/miniwdl-engine`，避免误删镜像和运行上下文；长期使用时
应监控该目录大小。当前 DIND 也继承宿主可见的整体 CPU/内存上限，单 task 仍按 WDL
runtime 限制。生产 worker 需要额外设置整体配额和受控的缓存回收策略。

runner 基础镜像、DIND、smoke task 和执行验收版生信镜像均锁定 OCI digest；
miniwdl 的完整 Python 依赖闭包锁在 `docker/miniwdl-requirements.txt`，并与
`uv.lock` 中对应版本对齐。编译器当前生成的部分 golden WDL 仍保留容器 tag，这是
需要在 ToolSpec/编译器层继续收敛的可复现性债务。

miniwdl 的相关行为可参考官方文档：

- [Runner in Docker 与共享路径要求](https://miniwdl.readthedocs.io/en/latest/runner_advanced.html)
- [CLI 运行目录和输入输出](https://miniwdl.readthedocs.io/en/latest/runner_cli.html)
- [Docker/Swarm 执行后端](https://miniwdl.readthedocs.io/en/latest/runner_backends.html)

## 结果与排障

每次执行创建独立目录：

```text
data/miniwdl/work/runs/<case>-<UTC timestamp>-<random>/
```

优先查看：

- `status.json`：框架判定和退出码；
- `result.json`：成功输出或 miniwdl 错误 JSON；
- `request.json`：WDL、案例和编译产物运行阻塞快照；
- miniwdl 生成的 task 日志和工作目录。

环境诊断：

```bash
./scripts/miniwdl.sh status
./scripts/miniwdl.sh logs
./scripts/miniwdl.sh doctor
```

如果首次启动较慢，通常是在构建 runner 或拉取 DIND/task 镜像。不要删除
`data/miniwdl-engine`、`data/miniwdl-certs` 或 `data/miniwdl/work`；先查看日志
确认是网络、磁盘、Docker 权限还是资源不足。CI 会把 `runs/` 作为诊断 artifact
保留 7 天。
