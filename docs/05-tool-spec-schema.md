# ToolSpec Schema 规范

> 状态：Phase 1 基线草案  
> Schema 版本：`1.0.0`  
> 对应机器定义：`schemas/tool-spec.schema.json`

## 1. 目的

ToolSpec 是平台对一个可复用生信软件调用方式的结构化描述。它不是 WDL task 文本，也不是软件安装说明，而是生成 WDL task 所需的、可校验的领域模型。

```text
ToolSpec
  + 输入/输出契约
  + 命令模板
  + 容器和资源声明
  + 可复现元数据
       |
       v
Compiler IR
       |
       v
WDL task
```

第一阶段要求用户能够手工定义软件用法，平台负责检查、标准化并生成 WDL。

## 2. 设计原则

1. **与 WDL Renderer 解耦**：ToolSpec 不直接保存完整 WDL。
2. **确定性**：同一 ToolSpec 必须生成相同语义的 task。
3. **显式端口**：所有可连接输入和输出必须有明确端口。
4. **双层类型**：同时描述 WDL 类型和生信语义类型。
5. **容器优先**：第一阶段只承诺 Docker 镜像形式的可复现运行环境。
6. **有限模板能力**：允许命令模板，但禁止模板执行任意 Python 或系统代码。
7. **可演进**：Schema 版本与软件版本分开管理。

## 3. 顶层结构

```json
{
  "schema_version": "1.0.0",
  "id": "fastp",
  "name": "fastp",
  "display_name": "fastp read preprocessing",
  "tool_version": "0.23.4",
  "description": "Adapter trimming and quality filtering for FASTQ reads.",
  "category": "quality_control",
  "container": {},
  "inputs": [],
  "outputs": [],
  "command": {},
  "runtime": {},
  "metadata": {}
}
```

### 3.1 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | ToolSpec 结构版本，第一阶段固定为 `1.0.0` |
| `id` | string | 平台稳定标识，也是 WDL task 名称的来源 |
| `name` | string | 软件或命令名称 |
| `tool_version` | string | 被封装软件版本 |
| `container` | object | 容器定义 |
| `inputs` | array | 输入端口列表 |
| `outputs` | array | 输出端口列表 |
| `command` | object | 命令模板 |

## 4. 标识命名规则

`id`、输入名和输出名必须满足：

```regex
^[A-Za-z_][A-Za-z0-9_]*$
```

推荐使用小写 snake_case，例如：

- `bwa_mem`
- `input_bam`
- `clean_fastq_1`

禁止：

- 使用空格、短横线或中文作为机器标识；
- 与同一 ToolSpec 中其他端口重名；
- 使用 WDL 保留字；
- 通过名称表达软件版本，例如 `fastp_0_23_4`。

显示名称可以使用中文、空格和其他自然语言字符。

## 5. Container 定义

```json
{
  "engine": "docker",
  "image": "quay.io/biocontainers/fastp:0.23.4--h5f740d0_0",
  "digest": "sha256:optional-image-digest"
}
```

| 字段 | 是否必填 | 说明 |
|---|---:|---|
| `engine` | 是 | 第一阶段仅允许 `docker` |
| `image` | 是 | 完整镜像引用，必须包含 tag 或 digest |
| `digest` | 否 | 推荐填写，用于精确复现 |

第一阶段编译器把 `image` 写入 WDL runtime `docker`。如果同时提供 digest，manifest 必须记录 digest；是否使用 digest 替换 tag 由编译配置决定。

## 6. InputPort 定义

```json
{
  "name": "reads_1",
  "label": "Read 1 FASTQ",
  "wdl_type": "File",
  "semantic_type": "bio.fastq.gz.single_or_r1",
  "required": true,
  "description": "Input FASTQ file for read 1",
  "default": null,
  "constraints": {
    "extensions": [".fastq.gz", ".fq.gz"]
  }
}
```

### 6.1 字段

| 字段 | 是否必填 | 说明 |
|---|---:|---|
| `name` | 是 | 唯一端口名 |
| `wdl_type` | 是 | WDL 语言层类型 |
| `semantic_type` | 是 | 生信领域语义类型 |
| `required` | 是 | 是否必须绑定 |
| `label` | 否 | UI 显示名称 |
| `description` | 否 | 输入说明 |
| `default` | 否 | 标量默认值；File 不建议设置默认值 |
| `constraints` | 否 | 文件扩展名、数值范围等 UI/校验提示 |

### 6.2 第一阶段允许的 WDL 类型

基础类型：

- `File`
- `String`
- `Int`
- `Float`
- `Boolean`

数组类型：

- `Array[File]`
- `Array[String]`
- `Array[Int]`
- `Array[Float]`
- `Array[Boolean]`

可选性不写进 `wdl_type` 字符串，由 `required` 表达：

- `required: true` -> `File x`
- `required: false` 且无 default -> `File? x`
- `required: false` 且有 default -> 根据 Renderer 生成带默认值声明

第一阶段不允许 Map、Pair、Object、Struct 和嵌套数组。

### 6.3 Semantic Type

`semantic_type` 是平台用于流程连接校验的领域标识，例如：

- `bio.fastq.gz.r1`
- `bio.fastq.gz.r2`
- `bio.fastq.gz.single`
- `bio.bam.aligned`
- `bio.bam.sorted`
- `bio.bam.sorted.indexed`
- `bio.vcf.gz`
- `bio.reference.fasta`
- `bio.reference.fasta_index`
- `bio.report.html`
- `core.string`
- `core.integer`

第一阶段兼容规则：

1. WDL 类型必须兼容；
2. `semantic_type` 默认要求精确相等；
3. `core.file.any` 可以接收任意 `File` 语义类型；
4. 其他继承、转换和本体推理进入第二阶段。

这是一种刻意保守的策略，宁可拒绝模糊连接，也不静默生成语义错误流程。

### 6.4 Default 规则

- `default` 必须与 `wdl_type` 匹配；
- `required: true` 时不得设置 `null` 以外的缺省语义；
- File 默认值原则上不进入 WDL 源码，应通过 workflow input 或 inputs JSON 注入；
- 密码、Token、云凭据等秘密不得写入 ToolSpec。

## 7. OutputPort 定义

```json
{
  "name": "clean_reads_1",
  "label": "Clean read 1 FASTQ",
  "wdl_type": "File",
  "semantic_type": "bio.fastq.gz.r1",
  "description": "Filtered read 1 output",
  "capture": {
    "mode": "path",
    "value": "outputs/clean_R1.fastq.gz"
  }
}
```

### 7.1 Capture 模式

第一阶段支持：

#### path

确定的相对路径：

```json
{
  "mode": "path",
  "value": "outputs/result.bam"
}
```

生成：

```wdl
File bam = "outputs/result.bam"
```

#### glob

通过 glob 捕获一个或多个文件：

```json
{
  "mode": "glob",
  "value": "outputs/*.vcf.gz"
}
```

当 `wdl_type` 为 `File` 时，编译器必须拒绝可能返回多个文件的含糊 glob，或根据明确策略生成 `glob(...)[0]` 并给出警告。推荐用户把类型声明为 `Array[File]`。

### 7.2 输出路径规则

- 必须是任务工作目录内的相对路径；
- 禁止绝对路径；
- 禁止 `../` 跳出工作目录；
- 输出路径可以引用允许的模板变量，但第一阶段推荐固定相对路径；
- 命令模板与 capture 必须对同一输出路径达成一致。

## 8. Command 定义

```json
{
  "shell": "bash",
  "strict_mode": true,
  "template": "mkdir -p outputs\nfastp --in1 {{ inputs.reads_1 }} --in2 {{ inputs.reads_2 }} --out1 outputs/clean_R1.fastq.gz --out2 outputs/clean_R2.fastq.gz --thread {{ inputs.threads }}\n"
}
```

### 8.1 模板变量

第一阶段仅允许：

```text
{{ inputs.<input_name> }}
```

例如：

- `{{ inputs.reads_1 }}`
- `{{ inputs.threads }}`

编译器将其转换为目标 WDL 版本的插值表达式，例如 WDL 1.0 的 `~{reads_1}`。

禁止：

- 调用函数；
- 属性遍历；
- 循环和条件模板；
- include/import；
- 执行 Python、JavaScript 或 shell 子模板；
- 引用未声明输入；
- 从环境变量隐式读取凭据。

复杂条件参数在第一阶段应通过明确的 Boolean/String 输入和简单命令行策略解决；高级模板 DSL 后置。

### 8.2 Shell 策略

第一阶段只支持 `bash`。

当 `strict_mode: true` 时，Renderer 在 command 开头插入：

```bash
set -euo pipefail
```

用户提供的命令模板不应包含宿主机路径假设，也不应依赖容器外软件。

## 9. Runtime 定义

```json
{
  "cpu": 4,
  "memory_gb": 8,
  "disk_gb": 20,
  "max_retries": 0
}
```

| 字段 | 类型 | 第一阶段映射 |
|---|---|---|
| `cpu` | integer | WDL runtime `cpu` |
| `memory_gb` | number | `memory: "N GB"` |
| `disk_gb` | integer | 后端兼容配置映射 |
| `max_retries` | integer | 可记录到 manifest；是否渲染取决于目标引擎配置 |

Runtime 是建议值，不保证所有执行引擎都采用同一语义。

## 10. Metadata 定义

```json
{
  "homepage": "https://example.invalid/tool-homepage",
  "source_repository": "owner/project",
  "license": "MIT",
  "authors": ["Example Team"],
  "tags": ["qc", "fastq"],
  "created_by": "user-or-system-id"
}
```

Metadata 不参与流程端口兼容判断，但必须进入编译 manifest，便于追溯。

## 11. 完整示例

```json
{
  "schema_version": "1.0.0",
  "id": "fastp",
  "name": "fastp",
  "display_name": "fastp paired-end preprocessing",
  "tool_version": "0.23.4",
  "description": "Quality filtering and adapter trimming.",
  "category": "quality_control",
  "container": {
    "engine": "docker",
    "image": "quay.io/biocontainers/fastp:0.23.4--h5f740d0_0"
  },
  "inputs": [
    {
      "name": "reads_1",
      "wdl_type": "File",
      "semantic_type": "bio.fastq.gz.r1",
      "required": true
    },
    {
      "name": "reads_2",
      "wdl_type": "File",
      "semantic_type": "bio.fastq.gz.r2",
      "required": true
    },
    {
      "name": "threads",
      "wdl_type": "Int",
      "semantic_type": "core.integer",
      "required": false,
      "default": 4,
      "constraints": {
        "minimum": 1,
        "maximum": 64
      }
    }
  ],
  "outputs": [
    {
      "name": "clean_reads_1",
      "wdl_type": "File",
      "semantic_type": "bio.fastq.gz.r1",
      "capture": {
        "mode": "path",
        "value": "outputs/clean_R1.fastq.gz"
      }
    },
    {
      "name": "clean_reads_2",
      "wdl_type": "File",
      "semantic_type": "bio.fastq.gz.r2",
      "capture": {
        "mode": "path",
        "value": "outputs/clean_R2.fastq.gz"
      }
    }
  ],
  "command": {
    "shell": "bash",
    "strict_mode": true,
    "template": "mkdir -p outputs\nfastp --in1 {{ inputs.reads_1 }} --in2 {{ inputs.reads_2 }} --out1 outputs/clean_R1.fastq.gz --out2 outputs/clean_R2.fastq.gz --thread {{ inputs.threads }}\n"
  },
  "runtime": {
    "cpu": 4,
    "memory_gb": 8,
    "disk_gb": 20
  },
  "metadata": {
    "tags": ["qc", "fastq"]
  }
}
```

## 12. ToolSpec 验证规则与错误码

| 错误码 | 含义 |
|---|---|
| `TS001` | Schema 版本不支持 |
| `TS002` | 标识不符合规则或使用保留字 |
| `TS003` | 输入/输出端口重名 |
| `TS004` | 不支持的 WDL 类型 |
| `TS005` | default 与 WDL 类型不匹配 |
| `TS006` | 容器镜像未固定 tag/digest |
| `TS007` | 输出 capture 非法或越界 |
| `TS008` | 命令模板语法非法 |
| `TS009` | 命令模板引用未知输入 |
| `TS010` | 必要输入未在命令中使用（警告或错误，按策略） |
| `TS011` | runtime 资源值非法 |
| `TS012` | semantic_type 缺失或非法 |

错误响应建议：

```json
{
  "code": "TS009",
  "severity": "error",
  "message": "Command template references undeclared input: read1",
  "path": "/command/template",
  "context": {
    "input_name": "read1"
  }
}
```

## 13. 规范化与摘要

ToolSpec 在进入编译器前必须规范化：

- 对对象键使用稳定排序；
- 保留数组顺序，端口顺序即 UI 和生成顺序；
- 去除不影响语义的空字段；
- 使用 UTF-8；
- 生成 canonical JSON；
- 对 canonical JSON 计算 SHA-256 digest。

`ToolSpec digest` 必须写入 compile manifest。这样即使第二阶段引入 Registry 和版本管理，第一阶段生成结果也可以准确追溯。

## 14. 向后兼容策略

- `schema_version` 使用语义化版本；
- Patch 版本只允许增加不影响现有文档的说明或约束修复；
- Minor 版本可以增加可选字段；
- Major 版本允许破坏性变化，必须提供迁移器；
- 编译器必须明确声明支持的 Schema 版本，不得静默忽略未知必需字段。

## 15. 第一阶段实现要求

1. JSON Schema 是外部输入的第一层校验；
2. Pydantic 模型承担跨字段和领域规则校验；
3. 模板解析器必须只识别白名单占位符；
4. WDL task Renderer 必须有 golden tests；
5. 示例 ToolSpec 必须在 CI 中通过 Schema、Pydantic 和 WDL 校验；
6. Django 模型只负责存储，不得成为 ToolSpec 的唯一规范来源。
