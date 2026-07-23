# Workflow Graph Schema 规范

> 状态：Phase 1 基线草案  
> Schema 版本：`1.0.0`  
> 对应机器定义：`schemas/workflow-graph.schema.json`

## 1. 目的

Workflow Graph 是可视化画布与编译器之间的稳定契约。它描述用户如何把 workflow 输入、ToolSpec 实例和 workflow 输出连接成一个 DAG。

Workflow Graph 不是 Vue Flow 的内部状态，也不是 WDL AST。前端画布数据必须转换为该规范后再提交给后端。

```text
Vue Flow state
    -> Workflow Graph
    -> graph validation
    -> Compiler IR
    -> WDL Renderer
```

## 2. 设计原则

1. **语义与 UI 分离**：节点位置不影响编译结果。
2. **端口级连线**：边必须指向具体输入/输出端口。
3. **显式 ToolSpec 引用**：每个工具节点绑定确定版本和 digest。
4. **单一事实来源**：连接关系只由 `edges` 决定，节点不重复保存上游来源。
5. **确定性**：相同语义图生成相同 Compiler IR 和 WDL。
6. **保守校验**：类型不明确时拒绝编译，不自动猜测转换关系。
7. **第一阶段有限语义**：只支持普通 DAG，不支持 scatter、conditional 和 subworkflow。

## 3. 顶层结构

```json
{
  "schema_version": "1.0.0",
  "id": "fastp_bwa_demo",
  "name": "FASTQ preprocessing and alignment",
  "description": "Phase 1 demonstration workflow",
  "target": {
    "language": "wdl",
    "version": "1.0",
    "profile": "cromwell-compatible"
  },
  "nodes": [],
  "edges": [],
  "layout": {},
  "metadata": {}
}
```

### 3.1 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | Graph Schema 版本，第一阶段固定为 `1.0.0` |
| `id` | string | workflow 稳定标识，也是 WDL workflow 名称来源 |
| `name` | string | 显示名称 |
| `target` | object | 编译目标配置 |
| `nodes` | array | 节点集合 |
| `edges` | array | 端口连接集合 |

## 4. Target 定义

```json
{
  "language": "wdl",
  "version": "1.0",
  "profile": "cromwell-compatible"
}
```

第一阶段只接受：

- `language = wdl`
- `version = 1.0`
- `profile = cromwell-compatible` 或 `miniwdl-compatible`

Profile 用于处理不同执行器的 runtime 或细节兼容，不得改变 Workflow Graph 的核心语义。

## 5. 节点类型

第一阶段只有三种节点：

```text
workflow_input
      |
      v
tool -----> tool
      |
      v
workflow_output
```

### 5.1 公共字段

所有节点包含：

```json
{
  "id": "node_id",
  "type": "tool",
  "label": "Display label"
}
```

`id` 必须满足：

```regex
^[A-Za-z_][A-Za-z0-9_]*$
```

节点 ID 在一个 Graph 中必须唯一，并将用于 WDL call alias。节点重命名会影响生成的 WDL，但不改变所引用 ToolSpec。

## 6. Workflow Input 节点

Workflow Input 节点将外部参数暴露为图中的输出端口。

```json
{
  "id": "input_reads_1",
  "type": "workflow_input",
  "label": "Read 1 FASTQ",
  "port": {
    "name": "value",
    "wdl_type": "File",
    "semantic_type": "bio.fastq.gz.r1",
    "required": true,
    "description": "Read 1 input"
  }
}
```

规则：

- 固定只有一个输出端口，名称为 `value`；
- `wdl_type` 和 `semantic_type` 使用 ToolSpec 的同一类型规则；
- 节点 ID 是生成的 workflow input 名称来源；
- 可以被多个下游端口引用；
- 不允许有入边。

标量输入同样使用 workflow input 节点，例如参考基因组、样本名或线程数。对于仅在一个 Tool 节点中使用且无需用户配置的标量，推荐使用 Tool 节点的 `parameter_values`。

## 7. Tool 节点

Tool 节点是一个 ToolSpec 的实例。

```json
{
  "id": "fastp_1",
  "type": "tool",
  "label": "fastp",
  "tool_ref": {
    "id": "fastp",
    "tool_version": "0.23.4",
    "spec_version": "1.0.0",
    "digest": "sha256:..."
  },
  "parameter_values": {
    "threads": 8
  }
}
```

### 7.1 ToolRef

| 字段 | 是否必填 | 说明 |
|---|---:|---|
| `id` | 是 | ToolSpec ID |
| `tool_version` | 是 | 软件版本 |
| `spec_version` | 是 | ToolSpec Schema 版本 |
| `digest` | 是 | ToolSpec canonical JSON 的 SHA-256 |

编译前必须解析 ToolRef，并验证解析结果的 digest 完全一致。不得仅凭名称加载“最新版本”。

### 7.2 Parameter Values

`parameter_values` 用于配置未通过边连接的输入端口，主要适用于：

- String；
- Int；
- Float；
- Boolean；
- 这些基础类型的一维数组。

规则：

1. key 必须对应 ToolSpec 输入端口；
2. 值必须满足该端口 `wdl_type`；
3. 同一端口存在入边时，不得再设置 parameter value；
4. File 默认要求通过边连接；第一阶段不把宿主机绝对路径写入 WDL；
5. 未连接、未赋值且无 ToolSpec default 的 required 输入会导致编译错误。

## 8. Workflow Output 节点

Workflow Output 节点把上游结果暴露为 workflow output。

```json
{
  "id": "output_bam",
  "type": "workflow_output",
  "label": "Aligned BAM",
  "port": {
    "name": "value",
    "wdl_type": "File",
    "semantic_type": "bio.bam.aligned",
    "description": "Workflow BAM output"
  }
}
```

规则：

- 固定只有一个输入端口，名称为 `value`；
- 必须且只能有一个入边；
- 不允许有出边；
- 节点 ID 是生成的 workflow output 名称来源；
- 端口类型必须与上游输出兼容。

## 9. Edge 定义

```json
{
  "id": "edge_reads1_fastp",
  "source": {
    "node_id": "input_reads_1",
    "port": "value"
  },
  "target": {
    "node_id": "fastp_1",
    "port": "reads_1"
  }
}
```

### 9.1 端口方向

允许的方向：

- workflow_input -> tool
- tool -> tool
- tool -> workflow_output
- workflow_input -> workflow_output

禁止：

- 指向 workflow_input；
- 从 workflow_output 发出；
- tool 输入端口作为 source；
- tool 输出端口作为 target；
- 自连接；
- 创建环。

### 9.2 基数规则

- 一个输出端口可以连接多个下游端口；
- 一个输入端口最多一个入边；
- workflow output 的 `value` 必须恰好一个入边；
- 未连接的可选端口可由 parameter value 或 ToolSpec default 提供。

## 10. 类型兼容规则

一条边合法必须同时满足 WDL 类型和生信语义类型。

### 10.1 WDL 类型兼容

第一阶段规则：

- 完全相同类型兼容；
- 非可选值可以连接到可选输入；
- Int 可以按显式策略连接到 Float，默认不开启隐式提升；
- 单值不能连接数组；
- 数组不能连接单值；
- File 与 String 不兼容；
- 不支持自动 flatten、select_first 等表达式插入。

Graph Schema 中不直接写 `?`，输入是否可选从 ToolSpec `required` 获取。

### 10.2 Semantic Type 兼容

第一阶段规则：

- 默认要求 source 与 target `semantic_type` 精确相等；
- target 为 `core.file.any` 时可接受任意 File 语义类型；
- source 为 `core.file.any` 不得自动连接到具体生信类型；
- 不自动认为 `bio.bam.aligned` 等于 `bio.bam.sorted`；
- 不自动插入 samtools sort、index、bgzip 等转换步骤。

未来可以引入类型本体和转换图，但不得改变第一阶段已生成流程的含义。

## 11. Layout 定义

UI 信息单独保存在 `layout`，编译器必须忽略它。

```json
{
  "layout": {
    "nodes": {
      "input_reads_1": {"x": 0, "y": 100},
      "fastp_1": {"x": 300, "y": 100}
    },
    "viewport": {
      "x": 0,
      "y": 0,
      "zoom": 1
    }
  }
}
```

Layout 可以变化而不改变语义 digest。建议分别计算：

- `semantic_digest`：忽略 layout 和非语义 metadata；
- `document_digest`：包含完整文档，用于保存检测。

## 12. Metadata 定义

```json
{
  "metadata": {
    "created_by": "user-id",
    "tags": ["demo", "alignment"],
    "notes": "Phase 1 acceptance workflow"
  }
}
```

第一阶段 metadata 不参与编译。时间戳由服务端记录，不建议写入会参与 canonical document 的随机字段。

## 13. 完整示例

以下流程表达：两个 FASTQ 输入进入 fastp，清洗后的 read 1 被暴露为 workflow output。

```json
{
  "schema_version": "1.0.0",
  "id": "fastp_demo",
  "name": "fastp demo",
  "target": {
    "language": "wdl",
    "version": "1.0",
    "profile": "cromwell-compatible"
  },
  "nodes": [
    {
      "id": "input_reads_1",
      "type": "workflow_input",
      "label": "Read 1",
      "port": {
        "name": "value",
        "wdl_type": "File",
        "semantic_type": "bio.fastq.gz.r1",
        "required": true
      }
    },
    {
      "id": "input_reads_2",
      "type": "workflow_input",
      "label": "Read 2",
      "port": {
        "name": "value",
        "wdl_type": "File",
        "semantic_type": "bio.fastq.gz.r2",
        "required": true
      }
    },
    {
      "id": "fastp_1",
      "type": "tool",
      "label": "fastp",
      "tool_ref": {
        "id": "fastp",
        "tool_version": "0.23.4",
        "spec_version": "1.0.0",
        "digest": "sha256:replace-with-real-digest"
      },
      "parameter_values": {
        "threads": 4
      }
    },
    {
      "id": "output_clean_reads_1",
      "type": "workflow_output",
      "label": "Clean read 1",
      "port": {
        "name": "value",
        "wdl_type": "File",
        "semantic_type": "bio.fastq.gz.r1"
      }
    }
  ],
  "edges": [
    {
      "id": "e1",
      "source": {"node_id": "input_reads_1", "port": "value"},
      "target": {"node_id": "fastp_1", "port": "reads_1"}
    },
    {
      "id": "e2",
      "source": {"node_id": "input_reads_2", "port": "value"},
      "target": {"node_id": "fastp_1", "port": "reads_2"}
    },
    {
      "id": "e3",
      "source": {"node_id": "fastp_1", "port": "clean_reads_1"},
      "target": {"node_id": "output_clean_reads_1", "port": "value"}
    }
  ],
  "layout": {
    "nodes": {
      "input_reads_1": {"x": 0, "y": 40},
      "input_reads_2": {"x": 0, "y": 180},
      "fastp_1": {"x": 320, "y": 100},
      "output_clean_reads_1": {"x": 640, "y": 100}
    },
    "viewport": {"x": 0, "y": 0, "zoom": 1}
  }
}
```

## 14. Graph 验证流水线

验证顺序固定为：

```text
1. JSON Schema
2. 节点与边唯一性
3. ToolRef 解析与 digest
4. 端口存在性与方向
5. 入边基数
6. parameter_values 类型
7. required 输入完整性
8. WDL 类型兼容
9. semantic_type 兼容
10. DAG 环检测
11. workflow output 完整性
12. 规范化与拓扑排序
```

不要在 Schema 校验阶段访问数据库；ToolRef 解析属于领域验证阶段。

## 15. 验证错误码

| 错误码 | 含义 |
|---|---|
| `WG001` | 不支持的 Graph Schema 版本 |
| `WG002` | 重复或非法节点 ID |
| `WG003` | 重复或非法边 ID |
| `WG004` | Edge 引用未知节点 |
| `WG005` | Edge 引用未知端口 |
| `WG006` | 端口方向非法 |
| `WG007` | 输入端口存在多个入边 |
| `WG008` | ToolRef 无法解析或 digest 不一致 |
| `WG009` | parameter value 引用未知端口 |
| `WG010` | parameter value 类型不匹配 |
| `WG011` | 必填输入没有来源/default |
| `WG012` | WDL 类型不兼容 |
| `WG013` | semantic_type 不兼容 |
| `WG014` | Graph 存在环 |
| `WG015` | workflow output 未绑定或多重绑定 |
| `WG016` | workflow input 存在入边 |
| `WG017` | workflow output 存在出边 |
| `WG018` | 同一端口同时由 edge 和 parameter value 赋值 |
| `WG019` | 不支持的 target/profile |

错误响应：

```json
{
  "code": "WG013",
  "severity": "error",
  "message": "Semantic type mismatch: bio.bam.aligned -> bio.bam.sorted",
  "node_id": "samtools_index_1",
  "port": "bam",
  "edge_id": "e9",
  "path": "/edges/8",
  "context": {
    "source_type": "bio.bam.aligned",
    "target_type": "bio.bam.sorted"
  }
}
```

前端根据 `node_id`、`port` 和 `edge_id` 高亮具体错误位置。

## 16. 确定性编译规则

1. 编译器忽略 `layout`；
2. 先按依赖关系进行拓扑排序；
3. 同一拓扑层的 tool 节点按节点 ID 升序排列；
4. workflow inputs 和 outputs 按 Graph 中声明顺序生成；
5. ToolSpec 端口按 ToolSpec 中声明顺序生成；
6. call alias 由 tool node ID 规范化得到；
7. 所有 ToolRef digest 和 Graph semantic digest 写入 manifest；
8. 不允许 Renderer 根据数据库当前状态选择未固定版本；
9. 编译时间、随机 ID 等非确定数据不得写入 WDL 正文。

## 17. Graph 到 Compiler IR 的转换

Compiler IR 至少包含：

```text
WorkflowIR
- workflow identifier
- ordered workflow inputs
- resolved task definitions
- topologically ordered calls
- explicit input bindings
- workflow outputs
- runtime/profile information
- source digests
```

Graph 节点、ToolSpec、WDL 输出之间的关系：

```text
ToolSpec.id             -> WDL task definition
Tool node.id            -> WDL call alias
Workflow input node.id  -> workflow input variable
Workflow output node.id -> workflow output variable
Edge                     -> call input binding/output reference
parameter_values         -> WDL literal binding
```

Compiler IR 必须是纯 Python 领域对象，不依赖 Vue Flow、Django ORM 或 Jinja2。

## 18. 前端适配要求

Vue Flow 可以维护自身节点和边对象，但保存时必须转换成 Workflow Graph：

- 前端组件类型不得泄漏到 `type` 字段；
- Vue Flow handle ID 必须映射为规范端口名；
- 临时选中、折叠、颜色等信息只进入 layout 或本地状态；
- 前端连接时进行即时预校验，后端仍执行权威校验；
- 加载后必须可无损恢复语义图，视觉样式不要求完全一致。

## 19. 第一阶段测试要求

至少覆盖：

- 单节点合法图；
- 线性合法图；
- 分支合法图；
- 环路；
- 未知节点/端口；
- 双重入边；
- 缺失 required 输入；
- parameter value 类型错误；
- WDL 类型不匹配；
- BAM 与 SortedBAM 语义不匹配；
- ToolSpec digest 不一致；
- layout 改变但 semantic digest 和 WDL 不变；
- 节点数组顺序改变但拓扑语义不变时 WDL 保持确定。

## 20. 向后兼容策略

- Graph Schema 使用独立 `schema_version`；
- 新增可选字段可以发布 minor 版本；
- 改变节点/边语义必须发布 major 版本；
- 进入 Compiler IR 前必须先迁移到当前内部版本；
- 未识别的必需语义必须报错，不得静默丢弃；
- UI layout 的扩展不得触发语义 Schema major 升级。
