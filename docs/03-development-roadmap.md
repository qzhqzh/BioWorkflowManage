# BioWorkflowManage 开发路线

## Phase 1: Workflow Compiler

目标：完成最小闭环。

### Milestone 1

项目初始化

- 前后端工程结构
- Docker 开发环境
- 文档体系

### Milestone 2

ToolSpec 管理

- 创建工具
- 定义输入输出
- 保存 JSON Schema

### Milestone 3

Workflow Editor

- DAG 编辑器
- 节点组件
- 连线
- 保存 Graph JSON

### Milestone 4

WDL Generator

- task 生成
- workflow 生成
- validate

## Phase 2: Platform

增加：

- 版本管理
- 审核发布
- 执行引擎
- AI Assistant
- Benchmark 系统

## 后续 AI 方向

AI 可调用：

- create_tool
- create_workflow
- validate_workflow
- generate_wdl
- benchmark_tool
