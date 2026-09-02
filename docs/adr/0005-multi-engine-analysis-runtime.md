# ADR 0005：共享任务控制面与显式执行引擎适配器

- 状态：Accepted
- 日期：2026-09-02
- 关联 Issue：[#38](https://github.com/qzhqzh/BioWorkflowManage/issues/38)

## 背景

BioWorkflowManage 已用 MiniWDL 执行 WDL，并通过 Integration API 向 okbox 等外部系统提供投递、查询、取消、重跑和结果下载能力。首个 Nextflow 需求是把已验证的 `01_amp_pipeline.nf + LC103` 作为 Analysis Product 提供给 okbox，首期不增加前端。

Nextflow 与 MiniWDL 不是可互换的流程语言或运行器。二者可以共享任务生命周期，但入口、参数构造、日志、工作目录、容器清理和结果发现方式不同。

## 决策

采用“共享任务控制面 + 显式执行引擎适配器 + 独立 worker profile”。

1. `WorkflowVersion` 固化 `execution_engine` 与 `runtime_manifest`；`AnalysisRun` 在受理时再次快照这两个字段。
2. `compiled_digest` 覆盖源码包及其 `execution` 段，确保 Nextflow 版本、固定参数、输出采集规则和容器 digest 与源码共同受保护。
3. Integration API 和 Analysis Product 仍是外部系统的稳定边界。okbox 选择产品代码与契约版本，不提交或覆盖执行引擎。
4. MiniWDL worker 默认只领取 `miniwdl` 任务；Nextflow worker 必须通过 `--engine nextflow` 显式领取，避免运行时依赖混装和错误抢占。
5. Nextflow 使用独立镜像层，固定 Java 17+ 运行环境（当前镜像为 Java 21）、Nextflow 25.04.8 与任务容器的 `repo@sha256`。API 容器不安装 Java/Nextflow。
6. Nextflow worker 只连接独立的 TLS Docker-in-Docker daemon，不挂载宿主 Docker socket；运行目录、数据库目录在 worker 与隔离 daemon 中使用相同绝对路径。任务容器带运行 ID label，取消或租约失效时只清理由该 label 且挂载当前运行目录的容器。
7. 首期只启用 `paired_fastq_csv` 输入适配器和 LC103 的两个受管输出：QC Excel、过滤后变异 TSV。自动 `-resume` 不启用；失败重跑仍创建新的 `AnalysisRun`。
8. Nextflow 子进程使用环境变量白名单，不继承数据库口令、Django 密钥或对象存储配置；固定包中的 `nextflow.config` 不参与运行，参数与执行策略全部由平台生成。

## 被否决方案

### 在现有 worker 中散布 `if nextflow`

改动表面较少，但会让领取、输入构造、日志、取消和结果采集逐步耦合，难以保证 MiniWDL 回归边界，因此不采用。

### 把 Nextflow 单独拆成新服务与新任务库

隔离更彻底，但会重复现有的幂等、租约、webhook、输出完整性与审计能力。首期成本过高，暂不采用。

### 把 Nextflow 转换成 WDL

两套 DSL、运行语义和生态能力并不等价，转换会引入不可验证的语义偏差，因此不采用。

## 结果

- 新引擎可以复用 Analysis Run 状态机、幂等、租约、输入资源快照、输出完整性、取消和 webhook。
- 每个执行引擎仍需实现自己的运行适配器和受限清理逻辑。
- 新的 Nextflow 产品必须通过受控导入命令生成不可变 `WorkflowVersion`，不能在投递接口中接受任意 `.nf`、任意参数或任意镜像。
- 回滚只需停用对应 Analysis Product 并停止 Nextflow worker；MiniWDL worker 和既有任务不受影响。
