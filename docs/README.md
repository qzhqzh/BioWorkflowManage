# BioWorkflowManage 文档索引

本目录是项目开发的规范来源。第一阶段开发应以这里的需求、Schema 和验收标准为准，而不是以前端临时对象、数据库字段或生成后的 WDL 反推模型。

## 第一阶段核心文档

1. [产品需求](01-requirements.md)
2. [技术架构](02-architecture.md)
3. [开发路线](03-development-roadmap.md)
4. [第一阶段完成定义](04-phase1-definition-of-done.md)
5. [ToolSpec Schema 规范](05-tool-spec-schema.md)
6. [Workflow Graph Schema 规范](06-workflow-graph-schema.md)

## 机器可读契约

- [`schemas/tool-spec.schema.json`](../schemas/tool-spec.schema.json)
- [`schemas/workflow-graph.schema.json`](../schemas/workflow-graph.schema.json)

## 第一套 Golden Fixture

- [`examples/phase1-fastp/tool-fastp.json`](../examples/phase1-fastp/tool-fastp.json)
- [`examples/phase1-fastp/workflow-graph.json`](../examples/phase1-fastp/workflow-graph.json)
- [`examples/phase1-fastp/expected/workflow.wdl`](../examples/phase1-fastp/expected/workflow.wdl)
- [`examples/phase1-fastp/expected/inputs.template.json`](../examples/phase1-fastp/expected/inputs.template.json)
- [`examples/phase1-fastp/expected/compile-manifest.json`](../examples/phase1-fastp/expected/compile-manifest.json)

## 文档优先级

出现冲突时按以下顺序处理：

1. 已批准的 Architecture Decision Record；
2. 机器可读 JSON Schema；
3. `04-phase1-definition-of-done.md`；
4. `05`、`06` 领域规范；
5. 需求和路线文档；
6. 示例与实现代码。

示例如果与 Schema 不一致，必须修复示例；实现如果与规范不一致，必须先判断是实现缺陷还是需要正式修改规范，不允许静默偏离。

## 后续待补文档

在进入正式编码前，继续补齐：

- `07-compiler-ir-and-renderer.md`
- `08-validation-and-error-contract.md`
- `09-rest-api-design.md`
- `10-frontend-workflow-editor.md`
- `11-testing-and-ci.md`
- `adr/0001-wdl-1.0-compatibility-profile.md`

这些文档必须服务于第一阶段闭环。用户权限、AI、执行调度和 Benchmark 相关文档暂不进入第一阶段主线。
