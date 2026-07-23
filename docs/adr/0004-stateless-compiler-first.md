# ADR 0004：第一阶段采用 Stateless Compiler First

- 状态：Accepted
- 日期：2026-07-23
- 决策范围：Phase 1

## Context

长期平台需要 Tool Registry、Workflow Version、用户权限和执行管理，但这些能力不是验证 Graph -> WDL 技术闭环的前提。如果第一阶段编译依赖数据库中的“当前版本”、用户上下文或发布状态，就会把编译模型与平台治理过早耦合。

## Decision

第一阶段编译请求直接携带：

- Workflow Graph；
- Graph 所引用的精确 ToolSpec bundle；
- 编译 options。

Compiler 仅按 id、tool version、Schema version 和 digest 解析 ToolRef。核心 compiler Python package：

- 不依赖 Django ORM；
- 不读取用户 session；
- 不查询“latest”；
- 不执行工具；
- 不下载外部 ToolSpec；
- 输入相同则产物相同。

Django API 只负责 HTTP、请求外壳、限制和返回映射。第二阶段 Registry resolver 实现同一个 resolver interface。

## Consequences

正面：

- 编译器可单元测试、CLI 调用和离线运行；
- 先验证最关键的领域模型；
- 数据库和权限不会污染确定性；
- 第二阶段可增加 Registry 而不重写 lowering/Renderer。

代价：

- 请求会重复携带 ToolSpec；
- 第一阶段没有正式资产管理；
- 前端草稿和工具列表只能使用本地/临时方式；
- 大规模 bundle 优化后置。

## Guardrails

- API endpoint 不在 View 内实现图算法；
- compiler package 不 import Django model；
- ToolRef resolver 必须显式注入；
- inline resolver 与未来 Registry resolver 使用同一 protocol；
- Registry 只能改变 ToolSpec 的获取方式，不能改变 digest 匹配规则。
