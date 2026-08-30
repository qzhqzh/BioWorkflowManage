# BioWorkflowManage 文档索引

本目录是项目开发的规范来源。第一阶段开发应以这里的需求、Schema、ADR 和验收标准为准，而不是以前端临时对象、数据库字段或生成后的 WDL 反推模型。

## 第一阶段核心文档

1. [产品需求](01-requirements.md)
2. [技术架构](02-architecture.md)
3. [开发路线](03-development-roadmap.md)
4. [第一阶段完成定义](04-phase1-definition-of-done.md)
5. [ToolSpec Schema 规范](05-tool-spec-schema.md)
6. [Workflow Graph Schema 规范](06-workflow-graph-schema.md)
7. [Compiler IR 与 WDL Renderer](07-compiler-ir-and-renderer.md)
8. [Validation 与 Error Contract](08-validation-and-error-contract.md)
9. [Phase 1 REST API](09-rest-api-design.md)
10. [前端 Workflow Editor](10-frontend-workflow-editor.md)
11. [测试与 CI](11-testing-and-ci.md)
12. [miniwdl 校验与隔离执行框架](12-miniwdl-execution.md)
13. [历史 WDL 资产与工作台](13-historical-wdl-workbench.md)
14. [第三方分析投递 API 与 MCP](14-integration-api-and-mcp.md)
15. [分析资源统一目录](15-analysis-resource-catalog.md)
16. [产品融合评估](16-product-cohesion-assessment.md)
17. [WDL 协作与原始数据体验迭代](17-wdl-rawdata-iteration.md)
18. [稳定版本发布与升级](18-stable-release.md)
19. [Analysis Node 独立交付与第三方部署](19-analysis-node-deployment.md)
20. [Reference Connector 与 MES 兼容性套件](20-reference-connector.md)

## Architecture Decision Records

- [ADR 0001：WDL 1.0 Compatibility Profile](adr/0001-wdl-1.0-compatibility-profile.md)
- [ADR 0002：双层类型系统](adr/0002-dual-type-system.md)
- [ADR 0003：Workflow Graph 语义规范化](adr/0003-semantic-canonicalization.md)
- [ADR 0004：Stateless Compiler First](adr/0004-stateless-compiler-first.md)

## 机器可读契约

- [`schemas/tool-spec.schema.json`](../schemas/tool-spec.schema.json)
- [`schemas/workflow-graph.schema.json`](../schemas/workflow-graph.schema.json)
- [`schemas/compiler-ir.schema.json`](../schemas/compiler-ir.schema.json)
- [`schemas/validation-report.schema.json`](../schemas/validation-report.schema.json)
- [`schemas/error-catalog.json`](../schemas/error-catalog.json)
- [`schemas/integration-openapi-v1.json`](../schemas/integration-openapi-v1.json)

这些文件是前端表单、后端 Pydantic 模型、编译器校验和 CI 的共同契约。

## 第一套 Golden Fixture

- [`examples/phase1-fastp/tool-fastp.json`](../examples/phase1-fastp/tool-fastp.json)
- [`examples/phase1-fastp/workflow-graph.json`](../examples/phase1-fastp/workflow-graph.json)
- [`examples/phase1-fastp/expected/compiler-ir.json`](../examples/phase1-fastp/expected/compiler-ir.json)
- [`examples/phase1-fastp/expected/workflow.wdl`](../examples/phase1-fastp/expected/workflow.wdl)
- [`examples/phase1-fastp/expected/inputs.template.json`](../examples/phase1-fastp/expected/inputs.template.json)
- [`examples/phase1-fastp/expected/compile-manifest.json`](../examples/phase1-fastp/expected/compile-manifest.json)

## Validation Fixture

- [`examples/validation/semantic-mismatch-report.json`](../examples/validation/semantic-mismatch-report.json)

后续每个稳定错误码至少增加一个 negative fixture。

## 自动契约校验

```bash
python -m pip install "jsonschema>=4.20,<5" "miniwdl>=1.10,<2"
python scripts/validate_contracts.py
miniwdl check examples/phase1-fastp/expected/workflow.wdl
```

GitHub Actions 配置：`.github/workflows/validate-contracts.yml`。

## 文档优先级

出现冲突时按以下顺序处理：

1. 已批准的 Architecture Decision Record；
2. 机器可读 JSON Schema；
3. `04-phase1-definition-of-done.md`；
4. `05` 至 `11` 领域、编译、接口和测试规范；
5. 需求和路线文档；
6. 示例与实现代码。

示例如果与 Schema 不一致，必须修复示例；实现如果与规范不一致，必须先判断是实现缺陷还是需要正式修改规范，不允许静默偏离。

## 当前文档基线已覆盖

- Phase 1 范围和完成定义；
- ToolSpec；
- Workflow Graph；
- Compiler IR；
- WDL Renderer；
- Validation/Error Contract；
- Stateless REST API；
- Vue Flow 编辑器边界；
- 测试、golden fixture 和 CI；
- miniwdl 静态检查、隔离容器 smoke 和真实数据案例框架；
- 四项关键架构决策。

在 Phase 1 编译规范之外，项目已补充认证、执行调度、历史 WDL、第三方 Integration API
与受限 AI Agent MCP、WDL 多人协作、原始数据后台索引和稳定发布治理；对应运行边界以
第 12 至 20 章为准。
