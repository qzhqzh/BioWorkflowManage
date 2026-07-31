# 第一阶段完成定义（Definition of Done）

> 当前状态（2026-07）：核心编译闭环和线性 fastp -> BWA 验收已经实现，但第 5 节
> 要求的三套完整验收资产尚未全部具备，因此不能宣布本 DoD 全部完成。工具、流程和
> 子流程版本属于后来落地的 Phase 2 能力，不改变本文件记录的 Phase 1 历史边界。

## 1. 阶段目标

第一阶段只验证一个核心闭环：

```text
用户定义 ToolSpec
    -> 在画布上拼接 Workflow Graph
    -> 平台执行结构与类型校验
    -> 编译为确定性的 WDL 1.0
    -> 通过独立 WDL 校验器
    -> 导出 WDL 与输入模板
```

第一阶段的产品性质是 **Workflow Compiler Prototype**，不是完整的生信流程管理平台。

## 2. 必须交付的能力

### 2.1 ToolSpec

用户能够定义一个生信软件的：

- 标识、名称和软件版本；
- 容器镜像；
- 输入端口和输出端口；
- WDL 基础类型与生信语义类型；
- 命令模板；
- 输出文件路径或 glob；
- CPU、内存和磁盘等基础运行资源。

ToolSpec 必须能通过 JSON Schema 校验，并能独立生成一个合法 WDL task。

### 2.2 Workflow Graph

用户能够在可视化画布中：

- 添加 workflow input、tool 和 workflow output 节点；
- 通过明确的端口建立连线；
- 配置非文件型参数；
- 删除节点与连线；
- 保存和重新加载 Graph JSON；
- 查看结构和类型错误。

### 2.3 Graph Validation

编译前至少执行：

- JSON Schema 校验；
- 节点、端口和 ToolSpec 引用校验；
- 必填输入完整性校验；
- WDL 类型兼容校验；
- 生信语义类型兼容校验；
- 单输入端口多来源校验；
- DAG 环检测；
- workflow output 来源校验。

错误必须返回稳定的错误码、节点定位和端口定位，供前端高亮。

### 2.4 WDL Compiler

编译器必须采用：

```text
Workflow Graph + resolved ToolSpecs
    -> validation
    -> normalized Compiler IR
    -> deterministic renderer
    -> WDL 1.0
```

同一份语义输入重复编译，生成内容必须一致；画布坐标、缩放比例等 UI 字段不得影响 WDL。

### 2.5 Validation 与导出

输出至少包括：

- `workflow.wdl`；
- `inputs.template.json`；
- `compile-manifest.json`；
- 校验结果。

生成的 WDL 必须通过 miniwdl 校验；在可用的 CI 环境中，再使用 WOMtool 做兼容性检查。

## 3. 第一阶段明确不做

以下能力在原始规划中进入第二阶段，不阻塞 Phase 1 编译器原型：

- 用户、组织、角色和权限体系；
- Tool Registry 审核、发布和市场；
- Workflow 版本分支、审批和发布；
- Cromwell 或云平台执行调度；
- 运行日志、成本和资源监控；
- AI 自动生成流程；
- AI 自动封装软件；
- 生信软件自动 Benchmark；
- scatter、conditional、subworkflow 等高级 WDL 语义；
- WDL 1.1/1.2/1.3 渲染器。

当前实际进展：

- 工具、流程、WDL 和子流程不可变版本已经实现；
- 固定版本子流程节点已经实现，并可编译为 WDL `import/call`；
- 用户权限、执行调度、运行日志、Benchmark、AI、scatter 和 conditional 仍未实现。

这里的“生成 WDL 并由 miniwdl 校验”仅表示静态编译成立，不表示平台已经拥有执行
引擎。真实任务调度、重试、日志、资源和产物回收属于后续独立阶段。

## 4. 第一阶段支持边界

### 支持

- 线性流程与普通 DAG；
- 一个输出连接多个下游输入；
- 每个输入端口最多一个上游来源；
- WDL 1.0 基础类型；
- 基础数组类型；
- workflow input、tool、workflow output 三类节点；
- Docker 容器运行时描述；
- 确定性生成和静态校验。

### 暂不支持

- 图中循环；
- 动态端口；
- 任意 WDL 表达式输入；
- 运行时才可确定的图结构；
- 自动插入格式转换工具；
- 复杂的文件组和隐式 secondary files；
- 对 shell 命令业务正确性的自动证明。

## 5. 验收场景

第一阶段至少提供三套可重复验收用例：

1. **单节点流程**：FASTQ 输入 -> FastQC -> HTML/ZIP 输出；
2. **线性流程**：FASTQ -> fastp -> BWA-MEM -> BAM；
3. **分支流程**：清洗后的 FASTQ 同时进入 FastQC 和比对工具。

每个用例必须包含：

- ToolSpec；
- Workflow Graph；
- 期望生成的 WDL golden file；
- inputs 模板；
- 正向校验测试；
- 至少一个故意构造的错误图测试。

### 当前验收资产差距

- `examples/phase1-fastp` 已提供当前 contracts/golden 基线，但不等同于上述 FastQC
  单节点验收；
- `examples/phase1-fastp-bwa` 已提供线性流程完整工具束、Workflow Graph、四类
  expected 产物、确定性编译测试和语义类型错误图；
- 分支流程的 ToolSpec、Graph、golden 和错误图尚待补齐；
- 后端已有编译 API 测试；Playwright 已连接真实 Docker Compose 服务并覆盖多流程
  切换、WDL 滚动预览、节点参数刷新持久化、语义类型冲突诊断、工具不可变版本冲突
  和完整四类编译产物。编译边界用例使用请求拦截，证明界面行为且不污染真实版本历史。

因此当前 CI 能证明已有契约、两套 golden、后端测试、前端构建和浏览器主链路没有
回归，但仍不能替代尚缺的 FastQC 单节点与分支产品验收场景。

## 6. 质量门槛

满足以下条件才能宣布第一阶段完成：

- 核心 Schema 有版本号和兼容策略；
- 编译器核心不依赖 Django ORM 或前端数据结构；
- 所有验证错误可机器读取；
- 编译结果可复现；
- 核心编译器单元测试覆盖主要分支；
- 三套验收流程在 CI 中生成并校验成功；
- 文档中的示例与机器可读 Schema 保持一致；
- 第二阶段功能没有侵入第一阶段核心模型。

## 7. 后续 Beta 门槛

在本 DoD 之外，多流程 Beta 还需要：

- 多流程切换贯穿打开、保存、验证、发布、编译和历史；
- 工具端口和参数编辑后能够校验并发布不可变新版本；
- 子流程内部画布可查看，并可显式升级父流程固定的版本；
- 真实后端 Playwright happy path 和验证失败场景已经具备；
- 认证授权、并发编辑保护、备份恢复在进入生产环境前完成。
