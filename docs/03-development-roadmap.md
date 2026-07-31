# BioWorkflowManage 开发路线

## 当前判断

项目已经跨过单流程编译器演示阶段，当前目标是形成 **可持续使用的多流程 Beta**。
版本管理、固定版本子流程、显式升级、真实多流程切换和核心前端 E2E 已进入主链路。
线性 fastp -> BWA 验收已经闭环；专用 FastQC 单节点、分支流程、流程创建入口和
生产安全仍未完成。

## Phase 1: Workflow Compiler Foundation

状态：**核心能力已实现，验收资产尚未全部完成。**

已实现：

- 前后端与 Docker Compose 工程结构；
- PostgreSQL 持久化；
- ToolSpec 和 Workflow Graph 契约；
- 可视化 DAG、逐端口连线、保存和重新加载；
- 结构、WDL 类型、语义类型、必填端口和 DAG 校验；
- 确定性 Graph -> IR -> WDL 1.0；
- miniwdl 静态校验、预览和产物导出。

仍需补齐：

- 专用 FastQC 单节点与分支流程的完整 golden/negative fixtures；
- 语义不变变换和更多编译器边界测试；
- 文档、Schema、fixture 的持续一致性门禁。

## Phase 2A: Registry 与版本基础

状态：**主要能力已实现。**

- 工具、流程、WDL 与子流程的不可变版本；
- 系统生成和人工编辑 WDL 的来源标签；
- 固定 `slug + version + digest` 的子流程引用；
- 子流程接口快照、递归依赖检查和 WDL `import/call`；
- 画布坐标自动保存、自动布局和产物侧栏预览。

## Phase 2B: Multi-workflow Beta

状态：**当前优先级。**

已完成：

1. 建立工程门禁：
   - contracts/golden；
   - 后端测试、Ruff、Django check、migration drift；
   - 前端依赖锁定、typecheck、production build 和 Compose 浏览器回归。
2. 使用统一的 active workflow 状态，贯穿打开、保存、验证、编译、WDL 和历史。
3. 工具名称、端口、参数、草稿校验和发布新版本已形成真实写回。
4. 可从父流程打开固定版本子流程的内部画布。
5. Playwright 已连接真实后端，覆盖多流程切换、WDL 滚动预览、参数刷新持久化和
   语义类型冲突诊断。
6. 父流程可查看子流程接口差异并显式升级固定版本；保存失败回滚，保存成功后重新验证。
7. Playwright 已覆盖工具不可变版本冲突和完整四类编译产物，边界用例会恢复草稿且
   不写入真实编译历史。
8. 重复编译未变化的流程会复用已有不可变版本；布局变化不产生语义版本。

仍需完成：

1. 流程创建与显式发布入口；
2. 专用 FastQC 单节点与分支流程的完整 golden/negative fixture。

完成标准：

- 用户能在两个以上流程之间切换，页面和所有 API 操作始终作用于当前流程；
- 创建/编辑工具到发布版本形成闭环；
- 保存画布、刷新恢复、验证、编译、预览产物由 E2E 自动证明；
- 三套 Phase 1 验收流程均拥有可重复的 golden 和错误用例。

## Phase 2C: Production Hardening

- 用户、组织、角色与权限；
- 乐观锁或版本号驱动的并发编辑保护；
- 审计日志、结构化日志与可观测性；
- PostgreSQL 备份、恢复演练和升级策略；
- TLS、密钥管理、限流与生产配置检查。

## Phase 2D: 历史 WDL 接管与重构

状态：**资产管理和单文件工作台已实现，task 提取尚未进入发布闭环。**

已完成：

- 独立 WDL 资产列表和工作台页面；
- 历史源码原样导入、miniwdl 分析与诊断；
- 标签、生命周期、搜索和分组；
- 不可变源码版本，以及操作者、时间、备注和 Diff 审计；
- Monaco 语法高亮、两空格格式化预览和保存新版本。

下一步：

- 支持多文件 WDL/import 包和依赖锁定；
- 将解析出的 task 转为待确认候选，并映射到 `ToolSpecDraft`；
- 通过通用工具库的校验、版本和发布机制完成人工确认；
- 记录历史流程从原始 WDL 到平台 Workflow Graph 的迁移关系。

## Phase 3: Execution Engine

执行引擎与编译器保持清晰边界：

```text
Workflow Graph -> Compiler -> versioned artifacts
                                  |
                                  v
                  Execution adapter / scheduler
                                  |
                                  v
                    run status / logs / outputs
```

后续可接 Cromwell、MiniWDL 或云平台，但调度、重试、日志、成本和运行数据模型不应
写入编译器核心。当前项目只保证静态编译和校验，不声明可执行真实生信任务。

## 后续 AI 方向

AI Assistant 在多流程 Beta 和权限边界稳定后再进入主线，可调用：

- create_tool
- create_workflow
- validate_workflow
- generate_wdl
- benchmark_tool

AI 生成内容仍必须通过现有 Schema、Graph 和 WDL 校验，并以可审查的新版本保存。
