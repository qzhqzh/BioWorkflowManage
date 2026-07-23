# Machine-readable Schemas

本目录保存第一阶段外部数据契约。

## Files

- `tool-spec.schema.json`：用户定义生信工具用法的结构约束。
- `workflow-graph.schema.json`：可视化 DAG 的持久化结构约束。

## Validation Layers

JSON Schema 只负责结构层校验，例如必填字段、枚举、格式和基础范围。以下规则必须由 Python/Pydantic 领域校验器完成：

- 输入和输出端口名称唯一；
- default 与具体 WDL 类型匹配；
- 命令模板占位符只引用已声明输入；
- ToolSpec digest 计算和校验；
- Graph 节点与边 ID 唯一；
- ToolRef 解析；
- 端口存在性、方向和基数；
- WDL 类型与 semantic type 兼容；
- required 输入完整性；
- DAG 环检测；
- 确定性拓扑排序。

## Compatibility

第一阶段只接受：

- ToolSpec Schema `1.0.0`
- Workflow Graph Schema `1.0.0`
- WDL target `1.0`

实现不得静默接受未知 major 版本。

## Canonical JSON

摘要计算统一使用：

1. UTF-8 编码；
2. 对 object key 递归排序；
3. array 顺序保持不变；
4. 不添加额外空白；
5. SHA-256；
6. 对外格式为 `sha256:<hex>`。

Python 参考：

```python
canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Workflow 的 `semantic_digest` 必须先移除 `layout` 和非语义 metadata，再按相同方法计算。
