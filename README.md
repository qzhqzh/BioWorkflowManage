# BioWorkflowManage

BioWorkflowManage 是面向生物信息学流程工程化的可视化 Workflow 编译平台。

第一阶段只验证一条核心链路：

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

## Phase 1: Workflow Compiler Prototype

必须交付：

- ToolSpec 定义和校验；
- workflow input、tool、workflow output 三类节点；
- 可视化 DAG 编辑和 Graph JSON 保存；
- WDL 类型与生信 semantic type 校验；
- 环检测、必填端口和 ToolRef digest 校验；
- Graph -> Compiler IR -> WDL 1.0；
- WDL、inputs template 和 compile manifest 导出；
- miniwdl 校验和 golden tests。

第一阶段暂不包含：

- AI Agent；
- 流程执行管理；
- 用户权限和组织管理；
- Tool/Workflow 发布审核；
- 软件自动评测；
- scatter、conditional 和 subworkflow。

## Phase 2: BioWorkflow Platform

后续扩展：

- Tool Registry；
- Workflow Version Management；
- Cromwell/MiniWDL 执行；
- AI Workflow Assistant；
- 生信软件 Benchmark；
- 自动化流程优化。

## Documentation

开发规范入口：[`docs/README.md`](docs/README.md)

第一阶段核心文档：

- [`docs/04-phase1-definition-of-done.md`](docs/04-phase1-definition-of-done.md)
- [`docs/05-tool-spec-schema.md`](docs/05-tool-spec-schema.md)
- [`docs/06-workflow-graph-schema.md`](docs/06-workflow-graph-schema.md)

机器可读契约：

- [`schemas/tool-spec.schema.json`](schemas/tool-spec.schema.json)
- [`schemas/workflow-graph.schema.json`](schemas/workflow-graph.schema.json)

第一套端到端基线：

- [`examples/phase1-fastp/`](examples/phase1-fastp/)

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
