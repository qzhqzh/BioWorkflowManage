# ADR 0003：Workflow Graph 语义规范化后计算摘要

- 状态：Accepted
- 日期：2026-07-23
- 决策范围：Phase 1 及缓存/版本兼容

## Context

Vue Flow、JSON 编辑器或数据库可能以不同数组顺序保存同一组 nodes 和 edges。如果直接对原始 Graph JSON 计算摘要，仅调整节点位置或序列化顺序就可能导致新的 digest、缓存失效和无意义版本差异。

## Decision

Workflow Graph 同时支持两个摘要概念：

### document digest

对完整保存文档计算，包括 layout 和 metadata，用于检测文件内容是否变化。

### semantic digest

先构造 semantic canonical document：

1. 删除 `layout`；
2. 删除第一阶段非语义 `metadata`；
3. `nodes` 按 `id` 字典序排序；
4. `edges` 按 `id` 字典序排序；
5. object key 使用字典序；
6. JSON 使用 UTF-8、`ensure_ascii=false` 和紧凑 separators；
7. 对 canonical bytes 计算 SHA-256。

伪代码：

```python
semantic = {k: v for k, v in graph.items() if k not in {"layout", "metadata"}}
semantic["nodes"] = sorted(semantic["nodes"], key=lambda node: node["id"])
semantic["edges"] = sorted(semantic["edges"], key=lambda edge: edge["id"])
digest = sha256(canonical_json(semantic))
```

## Consequences

正面：

- 移动节点不改变语义摘要；
- nodes/edges 原始数组顺序不改变摘要；
- 编译缓存键稳定；
- Graph、IR 和 manifest 可以可靠闭环；
- 前端实现细节不会制造伪版本。

代价：

- 摘要算法必须跨语言完全一致；
- 未来新增字段时必须明确其语义属性；
- JSON number canonicalization 需要持续测试。

## Guardrails

- Graph semantic digest 算法属于兼容性契约；
- 修改规范化算法需要新的 contract/major version；
- Compiler IR 的 call 排序仍由稳定拓扑排序决定，不由 nodes 数组顺序决定；
- ToolSpec 的 inputs/outputs 数组顺序当前仍属于文档语义，Renderer 会显式规范化端口输出顺序；
- CI 必须包含 layout/order metamorphic tests。
