# ADR 0002：采用 WDL Type + Bio Semantic Type 双层类型系统

- 状态：Accepted
- 日期：2026-07-23
- 决策范围：Phase 1 及后续兼容演进

## Context

只使用 WDL 的 `File`、`String` 等类型，无法区分 FASTQ、BAM、Sorted BAM、VCF 和参考基因组。只使用生信语义类型，又无法可靠生成和校验 WDL 声明。

## Decision

每个可连接端口同时声明：

```json
{
  "wdl_type": "File",
  "semantic_type": "bio.bam.sorted"
}
```

第一阶段兼容规则：

1. WDL type 必须兼容；
2. semantic type 默认精确相等；
3. target `core.file.any` 可以接收任意 File semantic type；
4. source `core.file.any` 不自动提升为具体生信类型；
5. 不做隐式软件插入或数据格式转换；
6. 不做本体继承、模糊匹配或 LLM 推断。

## Consequences

正面：

- 可以在生成 WDL 前发现业务语义错误；
- 前端端口能显示真实生信类型；
- 后续 AI 能基于结构化输入输出组合工具；
- 未来可引入类型本体和转换图，而不修改基础端口结构。

代价：

- 用户定义 ToolSpec 时需要填写更多信息；
- semantic type 词表需要治理；
- 第一阶段严格匹配可能拒绝部分实际可用但描述不一致的连接。

## Guardrails

- semantic type 使用小写点分层级格式；
- 第一阶段不从扩展名自动断言 semantic type；
- 错误连接返回 `WG013`；
- WDL type 错误返回 `WG012`；
- semantic type 不直接写入 WDL 源码；
- 类型转换必须由显式 Tool 节点表达。
