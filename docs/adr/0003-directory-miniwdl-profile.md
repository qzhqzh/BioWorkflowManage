# ADR 0003：Directory 使用 miniwdl development 语法

- 状态：Accepted
- 日期：2026-08-10
- 决策范围：miniwdl-compatible profile

## Context

历史 WDL 的注释任务以目录传递 ANNOVAR 数据库。把目录降级成 `String` 会丢失
文件本地化和访问边界语义，而 WDL 1.0 没有 `Directory` 类型。

## Decision

- ToolSpec、Workflow Graph 与 Compiler IR 保留 `Directory` 类型；
- 仅当实际使用的端口包含 `Directory` 时，编译产物使用 miniwdl 支持的
  `version development`；
- 编译 manifest 必须记录最终 target，运行使用该不可变产物；
- 不包含 `Directory` 的既有流程继续输出 WDL 1.0；
- cromwell-compatible profile 不承诺执行包含 `Directory` 的产物。

## Consequences

数据库目录不会被伪装成普通字符串，miniwdl 可以正确本地化并限制访问范围。
对应产物属于 miniwdl 执行兼容层，不保证交给其他 WDL 1.0 引擎运行。
