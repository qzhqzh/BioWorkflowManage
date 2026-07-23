# BioWorkflowManage

## 项目定位

BioWorkflowManage 是面向生物信息学流程工程化管理的平台。

第一阶段目标：验证从可视化 Workflow DAG 到 WDL 自动生成的完整链路。

核心理念：

> 用户定义生信工具能力，平台负责流程编排和 WDL 生成。

## Roadmap

### Phase 1: Workflow Compiler Prototype

目标：

- ToolSpec 定义
- 可视化 DAG 编辑
- Workflow Graph 保存
- Graph -> Compiler IR -> WDL
- WDL 校验和导出

暂不包含：

- AI Agent
- 流程执行管理
- 用户权限
- 软件自动评测

### Phase 2: BioWorkflow Platform

扩展：

- Tool Registry
- Workflow Version Management
- Cromwell/MiniWDL 执行
- AI Workflow Assistant
- 生信软件 Benchmark
- 自动化流程优化

## Documentation

详细设计见 docs 目录。
