# Phase 1 REST API 设计

> 状态：Phase 1 基线草案  
> API 版本：`v1`  
> 基础路径：`/api/v1`

## 1. 目标

第一阶段 API 只服务于以下闭环：

```text
编辑 ToolSpec
  -> 校验 ToolSpec
  -> 编辑 Workflow Graph
  -> 校验 Graph
  -> 编译 Graph + ToolSpec bundle
  -> 返回 IR、WDL、inputs template、manifest
```

第一阶段不把 API 设计成完整 Tool Registry 或 Workflow 管理系统。请求直接携带精确 ToolSpec bundle，后端使用 digest resolver 完成一次确定性编译。

## 2. 设计原则

1. **Stateless compiler first**：编译端点不依赖用户、项目、数据库版本状态。
2. **Contract driven**：请求和响应由 `schemas/` 中的机器契约约束。
3. **精确 ToolRef**：按 id、tool_version、spec_version、digest 解析，不允许“latest”。
4. **统一 diagnostics**：所有可修复输入错误返回 Validation Report。
5. **无部分成功**：有 error 时正式 artifacts 为空。
6. **确定性产物**：相同语义输入产生相同 artifact 内容和 digest。
7. **传输状态与语义状态分离**：HTTP status 表示请求处理结果，report status 表示校验结果。
8. **为第二阶段保留扩展位**：未来可将 inline ToolSpec bundle 替换为 Registry resolver，但不改变 Graph/IR 契约。

## 3. 第一阶段端点

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | 进程和依赖健康检查 |
| `GET` | `/contracts` | 返回可用契约版本列表 |
| `GET` | `/contracts/{contract_name}` | 返回 JSON Schema 或错误码目录 |
| `POST` | `/validations/tool-spec` | 校验单个 ToolSpec |
| `POST` | `/validations/workflow-graph` | 校验 Graph 与 ToolSpec bundle |
| `POST` | `/compilations` | 完整 Graph -> IR -> WDL 编译 |

第一阶段不提供：

- ToolSpec CRUD；
- Workflow CRUD；
- 登录、组织和权限；
- 编译任务队列；
- 执行 WDL；
- artifact 对象存储。

前端在预演阶段可以把草稿保存在浏览器、本地文件或开发态后端临时目录中；这不属于编译器核心契约。

## 4. 通用约定

### 4.1 Content Type

请求和响应：

```http
Content-Type: application/json
Accept: application/json
```

WDL 在成功编译响应中作为字符串 artifact 返回。后续大文件下载 API 进入第二阶段。

### 4.2 Request ID

服务端为每个请求生成：

```http
X-Request-ID: req_...
```

客户端可以传入合法 `X-Request-ID`，服务端可接受或重建。Request ID 不参与任何 artifact digest。

### 4.3 API 错误外壳

只有请求无法进入领域校验时使用通用错误：

```json
{
  "error": {
    "code": "REQUEST_INVALID",
    "message": "Request body is not valid JSON.",
    "request_id": "req_..."
  }
}
```

ToolSpec、Graph、类型和编译问题必须使用 Validation Report，不应只返回通用 message。

## 5. GET /health

响应：

```json
{
  "status": "ok",
  "service": "bioworkflow-compiler-api",
  "api_version": "v1",
  "compiler_contract": "phase1"
}
```

可选依赖状态：

```json
{
  "dependencies": {
    "miniwdl": "available",
    "womtool": "not_configured"
  }
}
```

健康检查不得执行完整编译。

## 6. GET /contracts

响应：

```json
{
  "contracts": [
    {"name": "tool-spec", "version": "1.0.0"},
    {"name": "workflow-graph", "version": "1.0.0"},
    {"name": "compiler-ir", "version": "1.0.0"},
    {"name": "validation-report", "version": "1.0.0"},
    {"name": "error-catalog", "version": "1.0.0"}
  ]
}
```

`GET /contracts/tool-spec` 返回对应 JSON Schema。允许的 contract_name 固定白名单，禁止拼接任意文件路径。

## 7. POST /validations/tool-spec

请求：

```json
{
  "request_version": "1.0.0",
  "tool_spec": {
    "schema_version": "1.0.0",
    "id": "fastp"
  },
  "options": {
    "warnings_as_errors": false
  }
}
```

响应：

```json
{
  "status": "completed",
  "validation": {
    "report_version": "1.0.0",
    "status": "valid",
    "validation_id": "val_...",
    "summary": {"error_count": 0, "warning_count": 1},
    "diagnostics": [
      {
        "code": "TW001",
        "stage": "tool_spec",
        "severity": "warning",
        "message": "Container image is not pinned by digest."
      }
    ]
  },
  "normalized": {
    "digest": "sha256:..."
  }
}
```

`normalized` 不返回被静默修改的 ToolSpec。第一阶段仅返回 canonical digest；若未来支持格式化，需要明确输出独立 `canonical_document`。

## 8. POST /validations/workflow-graph

请求：

```json
{
  "request_version": "1.0.0",
  "workflow_graph": {},
  "tool_specs": [],
  "options": {
    "warnings_as_errors": false
  }
}
```

### 8.1 ToolSpec bundle 规则

- 每个 Graph ToolRef 必须在 bundle 中精确匹配；
- 匹配键：`id + tool_version + schema_version + canonical digest`；
- 相同 digest 的重复 ToolSpec 拒绝或去重，推荐返回 `SCHEMA001`；
- 同一 digest 对应不同 canonical document 属于系统完整性错误；
- 多余 ToolSpec 可以 warning，也可以忽略；第一阶段推荐 `WW` 类 warning；
- bundle 不允许从 URL 动态下载 ToolSpec。

### 8.2 成功响应

```json
{
  "status": "completed",
  "validation": {
    "report_version": "1.0.0",
    "status": "valid",
    "validation_id": "val_...",
    "source": {
      "kind": "workflow_graph",
      "id": "fastp_demo",
      "digest": "sha256:..."
    },
    "summary": {"error_count": 0, "warning_count": 0},
    "diagnostics": []
  },
  "normalized": {
    "semantic_digest": "sha256:...",
    "topological_calls": ["fastp_1"]
  }
}
```

`topological_calls` 仅用于 UI 预览和调试，不替代 Compiler IR。

## 9. POST /compilations

### 9.1 Compile Request

```json
{
  "request_version": "1.0.0",
  "workflow_graph": {},
  "tool_specs": [],
  "options": {
    "emit_ir": true,
    "emit_inputs_template": true,
    "warnings_as_errors": false,
    "secondary_validator": "none"
  }
}
```

### 9.2 Options

| 字段 | 默认值 | 第一阶段含义 |
|---|---|---|
| `emit_ir` | `true` | 返回 `compiler-ir.json` |
| `emit_inputs_template` | `true` | 返回 workflow inputs 模板 |
| `warnings_as_errors` | `false` | 将 warning 按策略升级为阻断；必须记录在 manifest |
| `secondary_validator` | `none` | `none` 或 `womtool`；miniwdl 固定执行 |

target/version/profile 从 Workflow Graph `target` 读取，compile options 不得覆盖，避免同一个 Graph 在请求外部被悄悄改变语义。

### 9.3 Compile Success

```json
{
  "status": "succeeded",
  "request_id": "req_...",
  "validation": {
    "report_version": "1.0.0",
    "status": "valid",
    "validation_id": "val_...",
    "summary": {"error_count": 0, "warning_count": 0},
    "diagnostics": []
  },
  "artifacts": [
    {
      "name": "compiler-ir.json",
      "media_type": "application/json",
      "digest": "sha256:...",
      "content": "{...}"
    },
    {
      "name": "workflow.wdl",
      "media_type": "application/wdl",
      "digest": "sha256:...",
      "content": "version 1.0\n..."
    },
    {
      "name": "inputs.template.json",
      "media_type": "application/json",
      "digest": "sha256:...",
      "content": "{...}"
    },
    {
      "name": "compile-manifest.json",
      "media_type": "application/json",
      "digest": "sha256:...",
      "content": "{...}"
    }
  ]
}
```

Artifact content 编码：

- JSON/WDL 使用 UTF-8 文本字符串；
- digest 对 UTF-8 原始 bytes 计算；
- JSON artifact 使用 canonical JSON 计算语义 digest，同时可以记录 pretty document 的 byte digest；
- 第一阶段响应体总大小建议限制为 5 MiB。

### 9.4 Compile Rejected

```json
{
  "status": "rejected",
  "request_id": "req_...",
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

### 9.5 Compile Failed

系统或外部依赖失败：

```json
{
  "status": "failed",
  "request_id": "req_...",
  "validation": {
    "report_version": "1.0.0",
    "status": "failed",
    "validation_id": "val_...",
    "summary": {"error_count": 1, "warning_count": 0},
    "diagnostics": [
      {
        "code": "SYS003",
        "stage": "system",
        "severity": "error",
        "message": "The configured WDL validator is unavailable."
      }
    ]
  },
  "artifacts": []
}
```

## 10. HTTP Status 映射

| HTTP | 场景 |
|---:|---|
| `200` | validation 完成，可能 valid 或 invalid |
| `201` | compilation 成功创建一次编译结果；第一阶段也可统一使用 200 |
| `400` | JSON 无法解析、请求外壳缺字段 |
| `413` | 请求或响应估算超过大小限制 |
| `415` | Content-Type 不支持 |
| `422` | compile 被领域校验拒绝；若 validation endpoint 统一 200，应保持文档一致 |
| `500` | 未处理内部错误 |
| `503` | resolver 或外部 validator 不可用 |

建议：

- validation endpoints 始终 `200`，由 report.status 表达 valid/invalid；
- compilation 输入错误返回 `422`；
- compilation 成功返回 `201`；
- 前端同时读取 HTTP status 和 body.status。

## 11. 大小与安全限制

第一阶段建议：

| 对象 | 限制 |
|---|---:|
| 请求体 | 5 MiB |
| ToolSpec bundle | 512 个 |
| Graph nodes | 4096 |
| Graph edges | 16384 |
| command template | 64 KiB/ToolSpec |
| diagnostics | 10000 条，超过后增加截断标记进入后续 minor contract |
| 编译同步超时 | 30 秒 |

安全要求：

- 禁止 API 读取 ToolSpec 指定的本地路径；
- 编译阶段不执行 container 或 shell command；
- miniwdl 只进行静态 check，不运行 workflow；
- 不从 ToolSpec URL 下载文件；
- 不把 traceback、secret 或环境变量返回客户端；
- contract_name、artifact name 使用白名单；
- 对 command 和 metadata 做日志截断。

## 12. Determinism 与缓存

编译语义缓存键：

```text
compiler_contract
+ renderer_version
+ graph_semantic_digest
+ sorted ToolSpec digests
+ target/profile
+ semantic compile options
```

以下内容不进入缓存键：

- validation_id；
- request_id；
- layout；
- display-only metadata；
- API 调用时间。

第一阶段可以不实现缓存，但接口和 manifest 不能阻碍未来增加缓存。

## 13. OpenAPI 与实现

正式编码时应提供：

```text
openapi/phase1.yaml
backend/api/v1/
  health.py
  contracts.py
  validations.py
  compilations.py
  serializers.py
```

DRF Serializer 负责请求外壳；Pydantic/JSON Schema 和 compiler domain layer 负责 ToolSpec、Graph、IR 与 diagnostics。不要在 DRF View 中实现图算法或 WDL Renderer。

## 14. 第一阶段 API 验收

1. fastp fixture 可通过 `/validations/tool-spec`；
2. fastp Graph bundle 可通过 `/validations/workflow-graph`；
3. `/compilations` 返回与 golden files 字节级一致的 WDL；
4. 修改 layout 不改变 artifact digest；
5. 打乱 nodes/edges/tool_specs 顺序不改变 artifact digest；
6. semantic mismatch 返回 `WG013` 并定位 edge/node/port；
7. error 时 artifacts 为空；
8. API 不执行任何生信软件；
9. 所有响应带 request ID；
10. OpenAPI 示例与自动测试一致。
