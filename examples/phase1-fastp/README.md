# Phase 1 fastp Golden Fixture

这套文件是第一阶段编译器的第一条端到端验收基线。

## Input

- `tool-fastp.json`：fastp ToolSpec。
- `workflow-graph.json`：包含两个 workflow input、一个 tool 节点和三个 workflow output 的 DAG。

## Expected Output

- `expected/workflow.wdl`
- `expected/inputs.template.json`
- `expected/compile-manifest.json`

## Expected Compiler Flow

```text
tool-fastp.json + workflow-graph.json
    -> JSON Schema validation
    -> Pydantic/domain validation
    -> ToolRef digest verification
    -> Compiler IR
    -> WDL 1.0 renderer
    -> compare with expected/workflow.wdl
    -> miniwdl validation
```

## Digest Convention

ToolSpec 使用 canonical JSON 计算摘要：

```text
sha256:f65755c1e908864e19c2eefd7781a7dc035de02a90538b2ec527823bdab41bcc
```

Workflow semantic digest 排除 `layout` 和 `metadata` 后计算：

```text
sha256:6b2e90ea0b889e5d4148b0353e4ded68ba565dd88724f82add0371f4e745c163
```

实现初期如果规范化算法发生变化，必须同时：

1. 说明算法变化；
2. 更新规范文档；
3. 更新 fixture digest；
4. 添加迁移或兼容测试。

## First Test Cases

至少实现：

1. ToolSpec JSON Schema 通过；
2. Workflow Graph JSON Schema 通过；
3. ToolRef digest 匹配；
4. Graph 领域校验通过；
5. 输出与 golden WDL 完全一致；
6. 只修改 layout 后 WDL 与 semantic digest 不变；
7. 把输出 semantic type 改错后返回 `WG013`。
