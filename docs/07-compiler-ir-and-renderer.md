# Compiler IR 与 WDL Renderer 设计

> 状态：Phase 1 基线草案  
> IR 版本：`1.0.0`  
> 对应机器定义：`schemas/compiler-ir.schema.json`

## 1. 目的

Compiler IR 是 Workflow Graph 与目标语言 Renderer 之间的稳定中间表示。它负责把 ToolSpec、图连接、参数值和确定性排序统一成一个已经通过语义校验的编译模型。

```text
ToolSpec + Workflow Graph
          |
          v
  resolve / validate
          |
          v
      Compiler IR
          |
          +--> WDL 1.0 Renderer
          +--> future WDL Renderer
          +--> future CWL/Nextflow adapter
```

第一阶段的 Renderer 只输出 WDL 1.0，但 Compiler IR 不保存 WDL 源码片段，也不依赖 Vue Flow 或数据库模型。

## 2. 非目标

第一阶段 IR 不表达：

- scatter；
- conditional；
- subworkflow；
- runtime backend 配置；
- secrets；
- 任意 WDL 表达式；
- 用户手写 WDL AST；
- 运行状态和执行结果。

## 3. 设计原则

1. **只接收已验证输入**：Schema、ToolRef、Graph、类型与绑定错误必须在 lowering 前处理。
2. **Renderer 无业务推理**：Renderer 只做目标语言映射，不猜测端口、不补齐工具、不修改 Graph。
3. **表达式受限**：IR 只支持第一阶段需要的引用、字面量、路径和 glob。
4. **确定性**：相同 semantic Graph 与相同 ToolSpec digest 必须产生字节级稳定的 IR 和 WDL。
5. **可追踪**：IR 中每个 task、call、input 和 output 都能追溯到源 Graph 或 ToolSpec。
6. **版本独立**：`ir_version` 与 ToolSpec Schema、Graph Schema、WDL 版本分别管理。
7. **不可携带 UI 状态**：layout、viewport 和非语义 metadata 不进入 IR。

## 4. 编译流水线

固定流水线如下：

```text
1. Parse JSON
2. JSON Schema validation
3. Resolve ToolRef by exact digest
4. Domain validation
5. Normalize identifiers and ordering
6. Lower Graph + ToolSpec to Compiler IR
7. Validate IR invariants
8. Render WDL 1.0
9. miniwdl syntax/type validation
10. optional WOMtool validation
11. emit artifacts and manifest
```

任何阶段失败都不得输出“成功”的 WDL。可以输出诊断报告和未完成的调试信息，但不能把部分 WDL 当作正式产物。

## 5. IR 顶层结构

```json
{
  "ir_version": "1.0.0",
  "source": {
    "workflow_id": "fastp_demo",
    "workflow_schema_version": "1.0.0",
    "workflow_semantic_digest": "sha256:...",
    "tool_digests": ["sha256:..."]
  },
  "target": {
    "language": "wdl",
    "version": "1.0",
    "profile": "cromwell-compatible"
  },
  "tasks": [],
  "workflow": {}
}
```

### 5.1 Source

`source` 用于可追踪性，不参与业务推理。

| 字段 | 说明 |
|---|---|
| `workflow_id` | 源 Workflow Graph ID |
| `workflow_schema_version` | Graph Schema 版本 |
| `workflow_semantic_digest` | 忽略 layout 和非语义 metadata 后的摘要 |
| `tool_digests` | 参与编译的 ToolSpec digest，去重并按字典序排列 |

### 5.2 Target

第一阶段固定：

```json
{
  "language": "wdl",
  "version": "1.0",
  "profile": "cromwell-compatible"
}
```

IR Schema 允许 `miniwdl-compatible` profile，但同一个 IR 只对应一个明确 target。

## 6. IR 类型模型

### 6.1 TypeRef

```json
{
  "wdl_type": "File",
  "optional": false,
  "semantic_type": "bio.fastq.gz.r1"
}
```

- `wdl_type` 使用第一阶段允许的基础类型和一维数组；
- `optional` 从 ToolSpec `required` 推导；
- `semantic_type` 保留用于审计和未来 Renderer/分析，但 WDL Renderer 不将其写入 WDL 类型。

### 6.2 Literal

字面量必须带类型：

```json
{
  "kind": "literal",
  "wdl_type": "Int",
  "value": 4
}
```

不得在 IR 中使用未标注类型的 JSON 值，避免 `1`、`1.0`、字符串路径等在 Renderer 中被重新猜测。

### 6.3 Reference Expression

第一阶段支持三类绑定表达式：

#### workflow input 引用

```json
{
  "kind": "workflow_input_ref",
  "name": "input_reads_1"
}
```

#### call output 引用

```json
{
  "kind": "call_output_ref",
  "call": "fastp_1",
  "output": "clean_reads_1"
}
```

#### literal

```json
{
  "kind": "literal",
  "wdl_type": "Int",
  "value": 4
}
```

Renderer 不允许接收任意字符串表达式，例如 `select_first(...)`、动态索引或用户提供的 WDL 代码。

## 7. Task IR

一个去重后的 ToolSpec 对应一个 Task IR。

```json
{
  "name": "fastp",
  "source_tool": {
    "id": "fastp",
    "tool_version": "0.23.4",
    "spec_version": "1.0.0",
    "digest": "sha256:..."
  },
  "inputs": [],
  "command": {},
  "outputs": [],
  "runtime": {}
}
```

### 7.1 Task 名称分配

默认 task 名称为 ToolSpec `id`。

如果同一个 workflow 引用了相同 `id` 但不同 digest：

1. 按 digest 字典序排序；
2. 为冲突项分配 `<tool_id>__<digest前8位>`；
3. 所有调用使用分配后的 task 名称；
4. manifest 记录名称映射。

相同 digest 的多个 Tool 节点复用同一个 task 定义。

### 7.2 Task Input

```json
{
  "name": "threads",
  "type": {
    "wdl_type": "Int",
    "optional": false,
    "semantic_type": "core.integer"
  },
  "default": {
    "kind": "literal",
    "wdl_type": "Int",
    "value": 4
  }
}
```

规则：

- required 且无 default：WDL 中生成必填声明；
- optional 且无 default：生成 `Type?`；
- 有 default：生成带默认值声明；
- File 默认值第一阶段禁止进入 IR；
- inputs 按 `name` 字典序排列。

### 7.3 Command IR

命令模板必须先被解析为 segment，而不是把原始 Jinja 文本交给 Renderer。

```json
{
  "shell": "bash",
  "strict_mode": true,
  "segments": [
    {"kind": "literal", "value": "fastp --in1 "},
    {"kind": "input_ref", "name": "reads_1"},
    {"kind": "literal", "value": " --thread "},
    {"kind": "input_ref", "name": "threads"},
    {"kind": "literal", "value": "\n"}
  ]
}
```

Lowerer 只接受 ToolSpec 中允许的 `{{ inputs.<name> }}` 占位符，并转换为 `input_ref` segment。任何未声明输入、函数调用、循环、条件或模板语法都应在 ToolSpec 验证阶段失败。

WDL 1.0 Renderer 将 `input_ref` 转换为 `~{name}`。

### 7.4 Task Output

```json
{
  "name": "clean_reads_1",
  "type": {
    "wdl_type": "File",
    "optional": false,
    "semantic_type": "bio.fastq.gz.r1"
  },
  "expression": {
    "kind": "path",
    "value": "outputs/clean_R1.fastq.gz"
  }
}
```

第一阶段输出表达式：

- `path`：固定相对路径；
- `glob`：仅允许 `Array[File]`，生成 `glob("pattern")`。

`File + glob` 默认编译错误，不自动生成 `glob(...)[0]`，避免不确定性。

### 7.5 Runtime

```json
{
  "docker": "quay.io/biocontainers/fastp:0.23.4--h5f740d0_0",
  "cpu": 4,
  "memory_gb": 8,
  "disk_gb": 20,
  "max_retries": 0
}
```

WDL 1.0 Renderer 第一阶段映射：

- `docker` -> `runtime.docker`；
- `cpu` -> `runtime.cpu`；
- `memory_gb` -> `runtime.memory: "N GB"`；
- `disk_gb` 和 `max_retries` 默认只进入 manifest，除非 profile 明确启用映射。

## 8. Workflow IR

```json
{
  "name": "fastp_demo",
  "inputs": [],
  "calls": [],
  "outputs": []
}
```

### 8.1 Workflow Input

```json
{
  "name": "input_reads_1",
  "type": {
    "wdl_type": "File",
    "optional": false,
    "semantic_type": "bio.fastq.gz.r1"
  }
}
```

按 `name` 字典序排列。

### 8.2 Call

```json
{
  "alias": "fastp_1",
  "task": "fastp",
  "bindings": {
    "reads_1": {
      "kind": "workflow_input_ref",
      "name": "input_reads_1"
    },
    "threads": {
      "kind": "literal",
      "wdl_type": "Int",
      "value": 4
    }
  }
}
```

- `alias` 来自 Tool 节点 ID；
- `task` 是名称分配后的 Task IR 名称；
- bindings 的 key 按字典序输出；
- ToolSpec default 如果没有被 Graph 覆盖，可以不出现在 call binding 中；
- 为了 golden fixture 可读性，第一阶段允许显式重复写入与 default 相同的 parameter value。

### 8.3 Call 排序

使用 Kahn 拓扑排序，并以 Tool 节点 ID 作为同层节点的字典序 tie-breaker。

因此：

- layout 不影响排序；
- Graph 中 nodes/edges 数组原始顺序不影响排序；
- 独立分支仍能稳定输出；
- 检测到环时不产生 IR。

### 8.4 Workflow Output

```json
{
  "name": "output_clean_reads_1",
  "type": {
    "wdl_type": "File",
    "optional": false,
    "semantic_type": "bio.fastq.gz.r1"
  },
  "expression": {
    "kind": "call_output_ref",
    "call": "fastp_1",
    "output": "clean_reads_1"
  }
}
```

Workflow output 按 `name` 字典序排列。

## 9. Graph 到 IR Lowering 算法

### 9.1 输入

- 一个通过 Graph Schema 校验的 Workflow Graph；
- ToolRef resolver，能按 id/version/spec_version/digest 精确返回 ToolSpec；
- 编译 target/profile。

### 9.2 过程

1. 去除 layout 和非语义 metadata，计算 Graph semantic digest；
2. 解析所有 ToolRef 并验证 digest；
3. 对 ToolSpec 做领域校验和命令模板解析；
4. 建立节点、端口、入边和出边索引；
5. 执行 required、基数、方向和双层类型校验；
6. 对 Tool 节点执行稳定拓扑排序；
7. 对 ToolSpec digest 去重并分配 task symbol；
8. lowering workflow inputs；
9. lowering task definitions；
10. lowering call bindings；
11. lowering workflow outputs；
12. 构建 source/target 信息；
13. 使用 IR Schema 和领域 invariant 再次校验；
14. 对 canonical IR 计算 digest，供 manifest 使用。

### 9.3 不变量

进入 Renderer 前必须成立：

- 所有名称是合法且唯一的 WDL identifier；
- 所有 call 引用已存在 task；
- 所有 binding key 是 task input；
- 所有引用指向已存在 workflow input 或上游 call output；
- call 顺序满足依赖关系；
- 所有 required task input 有 binding 或 task default；
- 所有 output expression 类型匹配；
- 不存在 UI layout；
- 不存在原始 Jinja 模板；
- 不存在任意 WDL 表达式字符串。

## 10. WDL 1.0 Renderer

Renderer 接口建议：

```python
def render_wdl(ir: CompilerIR) -> RenderedArtifact:
    ...
```

`RenderedArtifact` 至少包含：

- `content`；
- `media_type = application/wdl`；
- `target`；
- `ir_digest`；
- Renderer 版本。

### 10.1 输出顺序

固定顺序：

1. `version 1.0`；
2. task 定义，按 task name；
3. workflow；
4. workflow input；
5. calls，按拓扑顺序；
6. workflow output。

### 10.2 格式约束

- 两空格缩进；
- Unix `\n` 换行；
- 文件末尾一个换行；
- 不输出时间戳、随机 ID 或主机路径；
- 不依赖 Python dict 插入顺序，显式排序；
- 字符串使用双引号并正确转义；
- command 使用 `<<<` / `>>>`；
- command 内容按 IR segment 合并；
- 不对用户 shell 命令做语义重排。

### 10.3 输入声明

| IR | WDL 1.0 |
|---|---|
| required, no default | `File x` |
| optional, no default | `File? x` |
| default literal | `Int threads = 4` |

### 10.4 表达式映射

| IR kind | WDL |
|---|---|
| `workflow_input_ref` | `input_name` |
| `call_output_ref` | `call_alias.output_name` |
| `literal` | 类型安全的 WDL literal |
| task output `path` | `"relative/path"` |
| task output `glob` | `glob("pattern")` |

## 11. 编译产物

一次成功编译至少产生：

```text
workflow.wdl
inputs.template.json
compile-manifest.json
compiler-ir.json   # 调试/验收模式必须输出，生产可配置
```

manifest 需要记录：

- compiler/version；
- Renderer/version；
- source Graph semantic digest；
- ToolSpec digests；
- IR digest；
- target/profile；
- 产物路径和 media type；
- 外部校验器结果。

## 12. Golden Test

`examples/phase1-fastp` 是第一套 golden fixture：

```text
tool-fastp.json
workflow-graph.json
expected/compiler-ir.json
expected/workflow.wdl
expected/inputs.template.json
expected/compile-manifest.json
```

测试要求：

1. ToolSpec 和 Graph 通过 Schema；
2. 实际 lowering IR 与 expected IR canonical JSON 完全一致；
3. 实际 WDL 与 expected WDL 字节级一致；
4. WDL 通过 miniwdl；
5. digest 与 manifest 一致；
6. 修改 layout 后 IR 和 WDL 不变化；
7. 打乱 nodes/edges 数组顺序后 IR 和 WDL 不变化。

## 13. Python 包边界建议

```text
backend/compiler/
  models/
    tool_spec.py
    workflow_graph.py
    compiler_ir.py
    diagnostics.py
  validation/
    tool_spec.py
    workflow_graph.py
    ir.py
  lowering/
    resolver.py
    symbols.py
    graph_to_ir.py
  renderers/
    base.py
    wdl_1_0.py
  canonical.py
  manifest.py
```

Pydantic 模型和 JSON Schema 必须保持语义一致；正式实现后建议由模型生成 Schema，再进行人工审查和兼容性测试。

## 14. 变更规则

- 新增可选字段：IR minor 版本；
- 删除字段、修改字段含义或改变排序：IR major 版本；
- Renderer 格式变化但语义不变：Renderer 版本变化，并更新 golden files；
- WDL target/profile 变化：新增 Renderer/Profile，不静默修改原 profile；
- 任何导致相同源输入产生不同 WDL 的变更必须在 PR 中明确记录。