# Integration API 历史契约基线

这里保存平台已经承诺支持的 OpenAPI 快照，与客户名称和软件 Release 无关。
`integration-v1-1.7.0.json` 是首次治理基线；1.7.0 是接口文档修订号，补全已有的可选 rawdata
就绪协议，不表示新软件 Release 或血液流程已验收。

- 已提交基线只增不改，不重新生成覆盖旧文件；CI 对比 PR 目标提交中的历史存档。
- 当前 `schemas/integration-openapi-v1.json` 必须通过所有 `integration-v1-*.json`。
- 兼容新增后可另存新修订，继续保留旧快照；不为每次文案或内部重构增加基线。
- 新主版本另立接口和基线；仍在支持期的 v1 必须保留，不能通过改名绕过门禁。

检查器自动允许新路径/方法、新组件、可选查询/请求头参数、开放对象的非必填属性，以及说明文字调整。
它保守拒绝已有类型、约束、枚举、required、默认值、scope、安全结构和回调等变化，包括某些实际可能
兼容的放宽操作。闭合或受约束对象不自动放行新增字段。需要更丰富的兼容扩展时，先补专项测试再扩展
检查逻辑，不改旧基线。结构检查不能检测未写入 Schema 的行为变化，相关 API/引擎回归仍是必需的。

```bash
uv run python scripts/validate_integration_compatibility.py
```

PR 中由 CI 使用 `--base-ref` 加查旧存档不可变。完整约定见
[平台契约与兼容开发约定](../../docs/22-integration-contract-governance.md)。
