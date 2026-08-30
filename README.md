# BioWorkflowManage

## 本地运行

复制环境变量并启动整套服务：

```bash
cp .env.example .env
docker compose up -d --build
```

浏览器访问 `http://localhost:8082`。局域网设备使用
`http://<运行服务的电脑局域网 IP>:8082`。API 健康检查位于
`/api/v1/health`。

PostgreSQL 数据通过 bind mount 保存在 `./data/postgres`；停止或重建容器不会删除数据。
生产环境必须修改 `.env` 中的数据库密码和 `DJANGO_SECRET_KEY`。

BioWorkflowManage 是面向生物信息学流程工程化的可视化 Workflow 编译平台。

当前已经打通核心编译链路：

```text
用户定义 ToolSpec
    -> 可视化 Workflow DAG
    -> 结构与类型校验
    -> Compiler IR
    -> WDL 1.0
    -> 独立校验与导出
```

核心理念：

> 用户定义生信工具的使用方式，平台负责规范化、流程编排和确定性 WDL 生成。

## 当前阶段

项目已完成多流程 Beta 的核心闭环，进入 **稳定维护期**：

- Docker Compose、PostgreSQL 持久化和局域网访问；
- ToolSpec、Workflow Graph、DAG 与类型校验；
- Graph -> Compiler IR -> WDL 1.0 和 miniwdl 校验；
- 独立 miniwdl 隔离执行环境、真实容器 smoke 和数据案例预检；
- Service Account、稳定分析产品目录、幂等第三方投递、受管路径与 S3/MinIO 不可变输入、取消/重跑、语义化输出、
  S3/MinIO/受管目录异步结果交付与确认，以及持久 Outbox 签名 Webhook；
- 通过受限 MCP 查询分析产品/流程/软件并独立测试固定 WorkflowVersion 或 ToolVersion；
- 工具、流程、WDL 与子流程的不可变版本；
- 子流程以固定 `slug + version + digest` 的黑盒节点复用，并编译为 WDL
  `import/call`；
- 可视化画布、逐端口连线、自动布局、坐标保存和产物预览；
- 多流程真实切换，以及工具草稿编辑、校验和不可变版本发布；
- 父流程可显式比较并升级固定的子流程版本，升级后保存并重新验证；
- 未发生语义或元数据变化的重复编译复用已有流程版本，画布布局不制造新版本；
- Playwright 连接真实 Docker Compose 服务，覆盖流程切换、WDL 预览、参数持久化、
  校验失败、工具版本冲突和完整编译产物。
- 历史 WDL 支持指派评审、行级讨论、冲突工作队列和不可变发布证据链；
- 原始数据由后台索引器分批扫描，页面读取持久快照并展示变化与运行引用。

稳定版本之后默认只做缺陷修复、性能、可用性、安全、兼容性和测试改进；新增大功能必须
独立立项，不混入维护版本。

认证登录、WDL 工具包管理和运行分析页面已经落地；运行分析由独立
`analysis-worker` 领取队列任务并记录进度、事件、取消、重跑和输出证据；独立
`artifact-exporter` 以租约和 SHA-256 清单交付结果，`webhook-dispatcher` 从事务 Outbox 投递
终态与交付完成通知；交付或通知失败不会反向修改分析状态。输出清理由显式 dry-run 优先的
maintenance 命令控制，不自动删除 Docker volume。成本与
集群资源配额属于后续执行引擎阶段。

## miniwdl 校验与真实运行

```bash
# 静态检查所有 WDL
./scripts/miniwdl.sh check

# 真实运行一个无业务数据的容器 task
./scripts/miniwdl.sh smoke

# 创建数据目录；后续补入 FASTQ/FASTA
./scripts/miniwdl.sh prepare

# 数据就绪后
./scripts/miniwdl.sh preflight fastp
./scripts/miniwdl.sh run fastp
```

运行结果保存在 `data/miniwdl/work/runs/`，隔离 Docker 引擎的镜像缓存保存在
`data/miniwdl-engine/`，mTLS 证书保存在权限受限的 `data/miniwdl-certs/`。
停止命令不会删除这些数据：

```bash
./scripts/miniwdl.sh stop
```

完整说明和 fastp→BWA 案例边界见
[`docs/12-miniwdl-execution.md`](docs/12-miniwdl-execution.md)。
第三方报告系统和 AI Agent 接入见
[`docs/14-integration-api-and-mcp.md`](docs/14-integration-api-and-mcp.md)。
Reference、Panel、BED 与 CNV 基线的统一配置见
[`docs/15-analysis-resource-catalog.md`](docs/15-analysis-resource-catalog.md)。
WDL 协作、原始数据索引和稳定升级见
[`docs/17-wdl-rawdata-iteration.md`](docs/17-wdl-rawdata-iteration.md) 与
[`docs/18-stable-release.md`](docs/18-stable-release.md)。

## Phase 1: Workflow Compiler Foundation

已经实现的基础能力：

- ToolSpec 定义和校验；
- workflow input、tool、workflow output 三类节点；
- 可视化 DAG 编辑和 Graph JSON 保存；
- WDL 类型与生信 semantic type 校验；
- 环检测、必填端口和 ToolRef digest 校验；
- Graph -> Compiler IR -> WDL 1.0；
- WDL、inputs template 和 compile manifest 导出；
- miniwdl 校验和 golden tests。

原始 Phase 1 边界不包含：

- AI Agent；
- 流程执行管理；
- 用户权限和组织管理；
- Tool/Workflow 发布审核；
- 软件自动评测；
- scatter、conditional 和 subworkflow。

其中工具/流程版本管理和固定版本子流程已经作为 Phase 2 能力落地；scatter 与
conditional 仍未实现。

## Phase 2: Multi-workflow Beta

当前重点：

- 补齐 FastQC 单节点与分支流程的 golden/negative 验收资产；
- 完成流程创建和显式发布入口；
- 完成认证、并发控制、审计、备份与恢复。

## Documentation

开发规范入口：[`docs/README.md`](docs/README.md)

第一阶段核心文档：

- [`docs/04-phase1-definition-of-done.md`](docs/04-phase1-definition-of-done.md)
- [`docs/05-tool-spec-schema.md`](docs/05-tool-spec-schema.md)
- [`docs/06-workflow-graph-schema.md`](docs/06-workflow-graph-schema.md)

机器可读契约：

- [`schemas/tool-spec.schema.json`](schemas/tool-spec.schema.json)
- [`schemas/workflow-graph.schema.json`](schemas/workflow-graph.schema.json)
- [`schemas/integration-openapi-v1.json`](schemas/integration-openapi-v1.json)

当前 contracts/golden 基线：

- [`examples/phase1-fastp/`](examples/phase1-fastp/)
- [`examples/phase1-fastp-bwa/`](examples/phase1-fastp-bwa/)：完整线性
  fastp -> BWA 工具束、四类 golden 产物和语义类型错误图。

## Architecture Rule

WDL 是编译输出，不是平台核心数据模型。

```text
ToolSpec + Workflow Graph
          |
          v
      Compiler IR
          |
          v
   Versioned Renderer
```

编译器核心必须独立于 Vue Flow、Django ORM 和具体 WDL 模板引擎。
