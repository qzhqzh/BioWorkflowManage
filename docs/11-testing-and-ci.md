# Phase 1 测试与 CI 策略

> 状态：Phase 1 基线草案

## 1. 测试目标

第一阶段测试的核心不是提高一个抽象覆盖率数字，而是证明：

1. ToolSpec 和 Workflow Graph 契约稳定；
2. 相同语义输入产生相同 IR 和 WDL；
3. 非法流程被稳定、可定位地拒绝；
4. layout、数组顺序等非语义变化不影响产物；
5. WDL 能被独立解析器接受；
6. 前后端对同一契约没有分叉理解。

## 2. 测试分层

```text
                 E2E
           API / integration
       compiler golden / contract
   domain, graph, renderer unit tests
schema validation / static checks
```

优先级：下层测试多而快，上层测试少而覆盖真实闭环。

## 3. Contract Tests

每次提交必须校验：

- 所有 JSON Schema 本身符合 Draft 2020-12；
- ToolSpec fixture 符合 ToolSpec Schema；
- Workflow Graph fixture 符合 Graph Schema；
- Compiler IR fixture 符合 IR Schema；
- Validation Report fixture 符合 Report Schema；
- error catalog code 唯一；
- diagnostic stage/severity 与 catalog 一致；
- manifest 中 digest 与实际 canonical document 一致。

当前入口：

```bash
python scripts/validate_contracts.py
```

## 4. Canonicalization Tests

canonical JSON：

```python
json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

必须覆盖：

- object key 顺序变化 digest 不变；
- Unicode 不转义导致的实现差异被固定；
- number 表示规则明确；
- layout/metadata 是否参与 semantic digest 符合规范；
- document digest 与 semantic digest 分开；
- ToolSpec digest 可在 Graph、IR、manifest 中闭环验证。

注意：JSON 数组顺序默认参与 digest。Graph semantic digest 当前仍包含 nodes/edges 原始数组顺序；正式 compiler 实现前应决定是否先规范化排序再计算 digest。推荐对 Graph 语义 canonical form 先按 ID 排序，再计算 semantic digest，并通过 ADR 固定。

## 5. Unit Tests

### 5.1 ToolSpec Validator

覆盖：

- 标识符；
- 保留字；
- input/output 重名；
- required/default；
- WDL 类型与 JSON value；
- semantic type；
- container tag/digest；
- output path traversal；
- File + ambiguous glob；
- command placeholder；
- forbidden template construct；
- runtime 边界。

### 5.2 Graph Validator

覆盖 `WG001` 至 `WG021`：

- node/edge 唯一；
- unknown node/port；
- 方向；
- 基数；
- required 完整性；
- edge/parameter double binding；
- WDL type；
- semantic type；
- cycle；
- output binding；
- target/profile。

### 5.3 Topological Sort

- 线性流程；
- 分支；
- 汇合；
- 多个独立根节点；
- lexical tie-breaker；
- 输入 nodes/edges 顺序变化；
- cycle 输出稳定错误；
- 大图边界。

### 5.4 Lowering

- ToolSpec digest 去重；
- 同一 Tool 多实例复用 task；
- 同 tool ID 不同 digest 的 symbol 分配；
- workflow input binding；
- call output binding；
- literal binding；
- default 省略/覆盖；
- workflow outputs；
- IR 稳定排序；
- 不携带 layout；
- 不携带原始 Jinja。

### 5.5 WDL Renderer

- required/optional/default input；
- String/Int/Float/Boolean literal；
- 数组 literal；
- 字符串转义；
- command segments；
- strict mode；
- path/glob output；
- runtime；
- call input syntax；
- output reference；
- 末尾换行和稳定格式。

## 6. Golden Tests

Golden test 输入：

```text
ToolSpec(s) + Workflow Graph + compile options
```

输出：

```text
compiler-ir.json
workflow.wdl
inputs.template.json
compile-manifest.json
```

测试比较：

- JSON 使用 canonical semantic compare，并可附 pretty byte compare；
- WDL 必须 byte-for-byte；
- 更新 golden 文件必须人工审查 diff；
- 禁止测试失败时自动覆盖 expected 文件；
- golden update 使用显式命令，例如 `pytest --update-golden`，默认关闭。

第一套：`examples/phase1-fastp`。

后续最低 golden workflows：

1. 单工具 paired FASTQ；
2. fastp -> aligner 两工具线性流程；
3. 一个输出连接两个下游分支；
4. workflow input 直连 workflow output；
5. 标量和数组参数；
6. 同一 ToolSpec 两个实例。

## 7. Metamorphic Tests

与其只验证固定示例，还应验证“语义不变变换”：

### 7.1 必须不改变 IR/WDL

- 移动节点位置；
- 修改 viewport；
- 修改 display label；
- 修改非语义 metadata；
- 打乱 nodes 数组；
- 打乱 edges 数组；
- 打乱 ToolSpec bundle；
- 打乱 JSON object key。

### 7.2 必须改变 IR/WDL 或 digest

- 修改 parameter value；
- 修改 edge source/target；
- 修改 ToolSpec digest/version；
- 修改 command；
- 修改 output capture；
- 修改 target/profile。

这些测试能比普通 snapshot 更早发现确定性缺陷。

## 8. Property-Based Tests

推荐使用 Hypothesis：

- 生成合法 identifier；
- 生成标量 WDL types 和匹配 literal；
- 生成小型 DAG；
- 随机打乱 Graph 顺序；
- 验证 topological order；
- 验证无环图 lowering 不抛未分类异常；
- 验证非法 edge 最终返回已登记 error code；
- 验证 Renderer 输出经过 miniwdl check。

Property tests 不替代领域 fixture，但适合发现边界组合。

## 9. Negative Fixtures

目录建议：

```text
examples/invalid/
  wg005-unknown-port/
  wg007-multiple-inbound/
  wg011-missing-required/
  wg012-wdl-type-mismatch/
  wg013-semantic-mismatch/
  wg014-cycle/
  wg018-double-binding/
  ts005-invalid-capture/
  ts008-forbidden-template/
```

每个目录包含：

- source document；
- ToolSpec bundle；
- expected validation report；
- README（可选，解释意图）。

断言至少包括 code/stage/severity/path/location，message 只做关键片段断言，避免正常措辞优化导致大量脆弱测试。

## 10. External WDL Validation

第一阶段：

```bash
miniwdl check workflow.wdl
```

推荐增加 WOMtool：

```bash
java -jar womtool.jar validate workflow.wdl
```

策略：

- miniwdl 是 CI 必跑；
- WOMtool 可以在单独 job 或容器运行；
- 两者任何一个确认语法/类型错误，编译测试失败；
- 结论冲突记录 `WDL003` 并阻断发布；
- 版本必须固定，避免 validator 自动升级改变结果。

第一阶段不执行 workflow，不下载容器，不处理真实生信数据。

## 11. API Tests

### 11.1 Contract

- content-type；
- request version；
- validation report shape；
- HTTP status 映射；
- error 时 artifacts 为空；
- request ID；
- size limits；
- contract endpoint whitelist。

### 11.2 Integration

fastp compile API：

- 请求 inline Graph + ToolSpec；
- 返回 succeeded；
- artifact digests 正确；
- WDL 与 golden 一致；
- IR 与 golden 一致；
- manifest 完整；
- warning policy 生效。

negative API：

- unknown digest；
- semantic mismatch；
- malformed JSON；
- unsupported profile；
- miniwdl unavailable。

## 12. Frontend Tests

### Unit

- Graph adapters；
- identifier allocator；
- connection compatibility；
- diagnostic mapping；
- parameter serialization。

### Component

- nodes/ports；
- Inspector；
- ToolSpec import；
- Validation Panel；
- artifact preview。

### Playwright E2E

至少两条：

1. fastp happy path；
2. semantic mismatch `WG013`。

E2E 后端使用真实 compiler API 或固定 contract stub；正式发布门禁至少一套使用真实后端。

## 13. Static Quality Gates

Backend：

```text
ruff check
ruff format --check
mypy --strict 或 pyright strict
pytest
```

Frontend：

```text
eslint
prettier --check
vue-tsc --noEmit
vitest run
playwright test
```

Contracts：

```text
validate_contracts.py
JSON/YAML parse
生成 TypeScript 类型无 diff
```

## 14. CI Job 设计

建议：

```text
contracts
backend-lint-typecheck
backend-tests
wdl-validation
frontend-lint-typecheck
frontend-unit
frontend-e2e
```

依赖关系：

```text
contracts
   +--> backend tests
   +--> frontend typecheck
backend + frontend
   +--> e2e
```

第一阶段最小 CI：

1. contracts；
2. miniwdl；
3. backend unit/golden；
4. frontend typecheck/unit；
5. fastp E2E。

## 15. Dependency Pinning

- Python 使用 lock file；
- Node 使用 lock file 并固定 package manager；
- GitHub Actions 使用 major tag起步，成熟后可 pin commit SHA；
- miniwdl/WOMtool 固定版本；
- test container 固定 digest；
- golden fixture 中的工具容器不实际拉取，但引用固定 tag/digest。

依赖升级独立 PR，必须重新生成/审查 golden diff。

## 16. Coverage

建议门槛：

- compiler domain/lowering/renderer 行覆盖 >= 90%；
- API >= 80%；
- frontend domain adapters >= 90%；
- UI 组件不强求盲目追求高覆盖，关键交互由 E2E 负责；
- 所有稳定 error code 必须有 fixture，重要性高于总覆盖率。

禁止为了覆盖率测试无意义 getter 或 snapshot 整个页面 DOM。

## 17. 发布门禁

Phase 1 release candidate 必须满足：

- 所有 Schema/fixture 一致；
- fastp golden 编译通过；
- miniwdl 通过；
- WOMtool 通过或明确批准暂时豁免；
- negative fixture 集合通过；
- layout/order metamorphic tests 通过；
- 前端 fastp E2E 通过；
- 无未登记 error code；
- 无 high/critical 依赖漏洞（根据项目安全 policy）；
- 文档和 OpenAPI 与实现同步。

## 18. 当前立即任务

文档阶段后，编码第一批测试应按此顺序：

1. 保持 `scripts/validate_contracts.py` 通过；
2. 建立 Pydantic models 与 Schema parity tests；
3. 实现 canonical digest tests；
4. 实现 ToolSpec validator；
5. 实现 Graph validator negative fixtures；
6. 实现 lowering fastp golden；
7. 实现 WDL Renderer golden；
8. 接入 miniwdl；
9. 接入 API；
10. 接入前端 E2E。
