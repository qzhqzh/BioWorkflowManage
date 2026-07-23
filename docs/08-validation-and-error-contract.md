# Validation 与 Error Contract

> 状态：Phase 1 基线草案  
> 报告 Schema 版本：`1.0.0`  
> 对应机器定义：`schemas/validation-report.schema.json`

## 1. 目的

第一阶段的核心不是“尽量生成一份 WDL”，而是只在输入可证明合法时生成可复现 WDL。Validation Contract 定义：

- 校验按什么顺序执行；
- 错误如何编码、定位和返回；
- 前端如何把错误映射回节点、端口和连线；
- 编译器何时可以继续、何时必须停止；
- CI 如何断言错误行为稳定。

错误码属于外部 API 契约。message 可以优化，code、stage、severity 和定位字段不能随意变化。

## 2. 核心原则

1. **全部收集，分阶段阻断**：一个阶段内尽可能收集多个错误；出现 error 后不进入依赖该阶段结果的后续阶段。
2. **机器码优先**：客户端不得依赖 message 文本判断错误类型。
3. **精确定位**：尽可能同时提供 JSON Pointer、node、edge 和 port。
4. **无猜测修复**：Validator 可以给 suggestion，但不能自动插入工具、转换类型或覆盖用户值。
5. **确定性**：相同输入产生同一组有序 diagnostics。
6. **安全输出**：不得在错误 context 中返回 secrets、完整环境变量或宿主机敏感路径。
7. **错误与警告分离**：error 阻止编译；warning 允许产物生成但必须写入 manifest。

## 3. 校验阶段

阶段顺序固定：

| stage | 输入 | 主要职责 | 失败后 |
|---|---|---|---|
| `parse` | 原始请求 | JSON 解码、编码和大小限制 | 停止 |
| `schema` | JSON document | JSON Schema 校验 | 停止领域校验 |
| `tool_spec` | ToolSpec | 端口唯一、模板、default、capture、runtime | 对应 ToolSpec 不可用 |
| `resolution` | ToolRef | 精确查找版本和 digest | 停止 Graph 领域校验 |
| `graph` | Graph + resolved tools | 节点、边、方向、基数、required、DAG | 停止 lowering |
| `type` | 端口索引 | WDL type 与 semantic type | 停止 lowering |
| `lowering` | validated model | 构建 Compiler IR 并检查 invariant | 停止 render |
| `render` | Compiler IR | WDL 1.0 输出 | 停止外部校验 |
| `wdl_validation` | WDL | miniwdl/WOMtool 语法和类型检查 | 编译失败 |
| `system` | 任意 | 不可预期内部错误 | 编译失败并记录 trace id |

`schema` 阶段不得查询数据库；`resolution` 才允许访问 Tool Registry 或本地 resolver。

## 4. Validation Report

统一响应：

```json
{
  "report_version": "1.0.0",
  "status": "invalid",
  "validation_id": "val_01J...",
  "source": {
    "kind": "workflow_graph",
    "id": "fastp_demo",
    "digest": "sha256:..."
  },
  "summary": {
    "error_count": 1,
    "warning_count": 0
  },
  "diagnostics": []
}
```

### 4.1 status

- `valid`：无 error，可以进入下一阶段或输出产物；
- `invalid`：至少一个 error；
- `failed`：系统或外部依赖失败，无法完成正常校验。

### 4.2 validation_id

服务端为每次校验生成追踪 ID。它不参与 Graph、IR 或 WDL digest，也不能进入确定性产物。

### 4.3 source

`source.kind` 可取：

- `tool_spec`；
- `workflow_graph`；
- `compiler_ir`；
- `rendered_wdl`；
- `compile_request`。

source digest 可用时应提供；解析失败时可以省略。

## 5. Diagnostic 结构

```json
{
  "code": "WG013",
  "stage": "type",
  "severity": "error",
  "message": "Semantic type mismatch: bio.bam.aligned -> bio.bam.sorted",
  "path": "/edges/8",
  "location": {
    "node_id": "samtools_index_1",
    "edge_id": "e9",
    "port": "bam"
  },
  "context": {
    "source_type": "bio.bam.aligned",
    "target_type": "bio.bam.sorted"
  },
  "suggestion": "Insert or select a tool that produces bio.bam.sorted."
}
```

### 5.1 必填字段

| 字段 | 说明 |
|---|---|
| `code` | 稳定机器错误码 |
| `stage` | 产生诊断的校验阶段 |
| `severity` | `error` 或 `warning` |
| `message` | 面向开发者和用户的默认说明 |

### 5.2 path

`path` 使用 RFC 6901 JSON Pointer，指向源文档中的最小相关位置，例如：

- `/inputs/2/default`；
- `/nodes/3/tool_ref/digest`；
- `/edges/8/target/port`。

解析错误无法提供 JSON Pointer 时可以省略，并提供行列信息。

### 5.3 location

可选字段：

- `node_id`；
- `edge_id`；
- `port`；
- `tool_id`；
- `line`；
- `column`。

前端优先使用 `edge_id` 高亮连线，其次 node + port，再次 path。

### 5.4 context

context 只允许 JSON 标量或标量数组，用于程序化展示差异。不得放入：

- 原始 secrets；
- 完整命令环境；
- 超长文件内容；
- Python traceback；
- 数据库连接信息。

### 5.5 suggestion

suggestion 是建议，不是自动修复指令。前端可以展示，但不得未经用户确认修改 Graph。

## 6. 诊断排序

为了稳定测试和 UI，diagnostics 按以下 key 排序：

```text
stage_order,
severity(error before warning),
path or "",
location.node_id or "",
location.edge_id or "",
location.port or "",
code,
message
```

阶段顺序使用第 3 节固定顺序。不得依赖数据库返回顺序、set 顺序或异步任务完成顺序。

## 7. ToolSpec 错误码

| code | 默认 stage | 含义 |
|---|---|---|
| `TS001` | schema | 不支持的 ToolSpec Schema 版本 |
| `TS002` | tool_spec | 重复、非法或保留的 tool/port identifier |
| `TS003` | tool_spec | 容器定义无效或镜像未固定 tag/digest |
| `TS004` | tool_spec | input default 与 WDL 类型/required 规则不匹配 |
| `TS005` | tool_spec | output capture 路径、glob 或类型组合无效 |
| `TS006` | tool_spec | command template 语法错误 |
| `TS007` | tool_spec | command 引用未知 input |
| `TS008` | tool_spec | command 使用禁止的模板能力 |
| `TS009` | tool_spec | runtime 值无效 |
| `TS010` | tool_spec | 输入或输出端口重名 |
| `TS011` | tool_spec | 不支持的 WDL 类型 |
| `TS012` | tool_spec | semantic_type 格式无效 |

## 8. Workflow Graph 错误码

沿用 `06-workflow-graph-schema.md` 已定义契约：

| code | 默认 stage | 含义 |
|---|---|---|
| `WG001` | schema | 不支持的 Graph Schema 版本 |
| `WG002` | graph | 重复或非法节点 ID |
| `WG003` | graph | 重复或非法边 ID |
| `WG004` | graph | Edge 引用未知节点 |
| `WG005` | graph | Edge 引用未知端口 |
| `WG006` | graph | 端口方向非法 |
| `WG007` | graph | 输入端口存在多个入边 |
| `WG008` | resolution | ToolRef 无法解析或 digest 不一致 |
| `WG009` | graph | parameter value 引用未知端口 |
| `WG010` | type | parameter value 类型不匹配 |
| `WG011` | graph | 必填输入没有来源/default |
| `WG012` | type | WDL 类型不兼容 |
| `WG013` | type | semantic_type 不兼容 |
| `WG014` | graph | Graph 存在环 |
| `WG015` | graph | workflow output 未绑定或多重绑定 |
| `WG016` | graph | workflow input 存在入边 |
| `WG017` | graph | workflow output 存在出边 |
| `WG018` | graph | 同一端口同时由 edge 和 parameter value 赋值 |
| `WG019` | schema | 不支持的 target/profile |
| `WG020` | graph | Edge 自连接 |
| `WG021` | graph | 节点、边或 layout 引用不一致 |

`WG001` 至 `WG019` 保持兼容；新增错误只能追加，不能重新定义旧 code。

## 9. Compiler IR 与 Renderer 错误码

| code | stage | 含义 |
|---|---|---|
| `IR001` | lowering | lowering 后 IR 不满足 Schema |
| `IR002` | lowering | task/call/workflow symbol 冲突无法确定性解决 |
| `IR003` | lowering | binding 引用不存在的 input/output |
| `IR004` | lowering | call 依赖违反拓扑顺序 |
| `IR005` | lowering | 表达式类型与目标类型不匹配 |
| `IR006` | lowering | 发现不支持的 IR 表达式或领域构造 |
| `RD001` | render | 不支持的 target/version/profile |
| `RD002` | render | IR 表达式无法映射到 WDL 1.0 |
| `RD003` | render | 字面量无法安全编码为 WDL |
| `RD004` | render | command segment 引用未知 task input |
| `RD005` | render | Renderer 输出不满足内部格式 invariant |
| `WDL001` | wdl_validation | miniwdl 语法或类型检查失败 |
| `WDL002` | wdl_validation | WOMtool 验证失败 |
| `WDL003` | wdl_validation | 两个验证器结论冲突 |
| `SYS001` | system | 未分类内部错误 |
| `SYS002` | system | Tool resolver/数据库不可用 |
| `SYS003` | system | 外部验证器不可用或超时 |

## 10. Warning 码

Warning 必须有独立 code，不得复用 error code 改 severity。

| code | 含义 |
|---|---|
| `TW001` | 容器只有 tag，没有 immutable digest |
| `TW002` | metadata 缺少 license/source 信息 |
| `WW001` | workflow 没有暴露任何 output |
| `WW002` | 工具输出未被使用 |
| `WW003` | workflow input 被声明但未使用 |
| `RW001` | runtime 字段未被当前 profile 渲染 |
| `VW001` | WOMtool 未执行，仅完成 miniwdl 校验 |

第一阶段默认 warnings 不阻止编译；未来若支持 strict policy，应由 compile request 显式指定，并写入 manifest。

## 11. Schema 错误归一化

jsonschema 原始错误不得直接作为 API 契约。服务端需要把它归一化：

```text
validator error
  -> determine document kind
  -> map to TS001/WG001 or generic schema code
  -> RFC 6901 path
  -> safe context
  -> stable diagnostic sort
```

推荐：

- ToolSpec Schema 通用错误：`TS001` 或与字段对应的 TS code；
- Graph Schema 通用错误：`WG001`/`WG019` 或 `WG002`/`WG003`；
- 无法精确映射时使用 `SCHEMA001`，context 记录 validator keyword。

新增通用错误：

| code | stage | 含义 |
|---|---|---|
| `PARSE001` | parse | 请求不是合法 JSON |
| `PARSE002` | parse | 编码、深度、大小或数组长度超过限制 |
| `SCHEMA001` | schema | 未归类的 JSON Schema 约束失败 |

## 12. HTTP/API 映射建议

| 场景 | HTTP status | report status |
|---|---:|---|
| JSON 无法解析 | 400 | invalid |
| Schema/Graph/Type 错误 | 422 | invalid |
| ToolRef 不存在 | 422 | invalid |
| Tool Registry 暂时不可用 | 503 | failed |
| 外部 WDL validator 暂时不可用 | 503 或按 policy 降级 | failed/valid+warning |
| 内部未处理异常 | 500 | failed |
| 校验通过 | 200 | valid |
| 编译成功 | 200/201 | valid，另返回 artifacts |

不要把用户可修复的 Graph 错误返回成 500。

## 13. 前端交互契约

前端收到 report 后：

1. 顶部显示 error/warning 汇总；
2. 画布按 `edge_id` 或 `node_id` 高亮；
3. 右侧属性面板定位 `port`；
4. 错误列表显示 code、message、suggestion；
5. 点击错误时聚焦节点或连线；
6. code 可用于国际化 message lookup；
7. 原始服务端 message 作为 fallback；
8. warning 与 error 视觉上必须区分。

前端不得只显示“生成失败”。至少需要告诉用户哪个节点、哪个端口、为什么失败。

## 14. Compile Result

编译 API 推荐返回：

```json
{
  "status": "succeeded",
  "validation": {
    "report_version": "1.0.0",
    "status": "valid",
    "validation_id": "val_...",
    "summary": {"error_count": 0, "warning_count": 1},
    "diagnostics": []
  },
  "artifacts": [
    {"name": "workflow.wdl", "media_type": "application/wdl"},
    {"name": "inputs.template.json", "media_type": "application/json"},
    {"name": "compile-manifest.json", "media_type": "application/json"}
  ]
}
```

失败时：

```json
{
  "status": "rejected",
  "validation": {
    "report_version": "1.0.0",
    "status": "invalid",
    "validation_id": "val_...",
    "summary": {"error_count": 1, "warning_count": 0},
    "diagnostics": [
      {
        "code": "WG013",
        "stage": "type",
        "severity": "error",
        "message": "Semantic type mismatch"
      }
    ]
  },
  "artifacts": []
}
```

只要存在 error，正式 artifacts 必须为空。

## 15. 测试要求

每个稳定错误码至少有一个 fixture，断言：

- code；
- stage；
- severity；
- path；
- location；
- diagnostics 顺序；
- 不生成正式 WDL。

第一阶段最低 negative fixtures：

1. unknown ToolRef/digest mismatch -> `WG008`；
2. unknown port -> `WG005`；
3. duplicate inbound edge -> `WG007`；
4. missing required input -> `WG011`；
5. WDL type mismatch -> `WG012`；
6. semantic mismatch -> `WG013`；
7. cycle -> `WG014`；
8. edge + parameter double binding -> `WG018`；
9. forbidden command template -> `TS008`；
10. invalid output capture -> `TS005`。

## 16. 兼容性规则

- code 含义不得在同一 major contract 中改变；
- 新 code 只能追加；
- message 可以改进，但客户端不得依赖原文；
- context 可以增加字段，不应删除已公开字段；
- severity 从 warning 升为 error 属于行为变化，必须记录；
- Validation Report 删除或重定义字段需要 major 版本；
- 前端未知 code 必须显示通用 fallback，而不是丢弃诊断。

## 17. 实现边界建议

```text
backend/compiler/diagnostics.py
  DiagnosticCode
  Diagnostic
  ValidationReport
  DiagnosticCollector

backend/compiler/validation/
  schema.py
  tool_spec.py
  resolution.py
  workflow_graph.py
  types.py
  ir.py
  wdl.py
```

Validator 不应直接抛出面向 HTTP 的异常。领域层返回 Validation Report，由 API 层决定 HTTP status。