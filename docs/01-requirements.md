# BioWorkflowManage 产品需求文档

## 1. 产品目标

构建一个生信 Workflow 工程化平台，使用户无需直接编写 WDL，而通过定义软件组件和可视化流程图自动生成规范 WDL。

## 2. 第一阶段范围

第一阶段定位为 Workflow Compiler 原型。

核心流程：

ToolSpec -> Workflow DAG -> Compiler IR -> WDL

### 功能

1. 生信软件定义

用户可以定义：

- 软件名称
- 软件版本
- Docker/运行环境
- 输入参数
- 输出文件
- Command 模板

2. Workflow 可视化设计

支持：

- 节点拖拽
- 节点连接
- DAG 保存
- 参数配置

3. Workflow 校验

包括：

- 类型匹配
- 输入输出检查
- DAG 环检测

4. WDL 生成

生成：

- task
- workflow
- input/output

## 3. 第二阶段范围

平台化能力：

- Tool Registry
- 权限管理
- Workflow Version
- 流程发布
- 执行管理
- AI Agent
- 软件自动 Benchmark

## 4. 非目标

第一阶段不实现：

- 自动发现软件
- AI 自动设计流程
- 云计算调度
- 软件质量评价
