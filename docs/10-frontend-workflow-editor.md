# Phase 1 前端 Workflow Editor 设计

> 状态：Phase 1 基线草案  
> 前端：Nuxt 4 + Vue 3 + TypeScript + Vue Flow

## 1. 目标

前端第一阶段只需要让用户完成一个可信闭环：

```text
定义/导入 ToolSpec
  -> 把工具拖入画布
  -> 创建 workflow input/output
  -> 连接端口
  -> 配置标量参数
  -> 查看校验错误
  -> 生成并预览 WDL
  -> 导出编译产物
```

前端不是 WDL 编辑器，也不是执行监控平台。

## 2. 技术选型

- Nuxt 4；
- Vue 3 Composition API；
- TypeScript strict mode；
- Vue Flow；
- Pinia：仅用于跨组件编辑会话状态；
- AJV 8：浏览器端 JSON Schema 快速校验；
- Monaco Editor 或 CodeMirror 6：只用于 JSON/WDL 预览，可在 MVP 后半段选择；
- Vitest + Vue Test Utils；
- Playwright：端到端流程测试。

不建议：

- 把 Vue Flow node/edge 类型直接作为后端请求；
- 用 `any` 绕过 Schema；
- 在前端重新实现另一套完整编译器；
- 允许用户直接编辑生成后的 WDL 并反向同步 Graph；
- 第一阶段引入复杂设计系统或微前端。

## 3. 页面布局

桌面优先三栏：

```text
+----------------+-------------------------------+-------------------+
| Tool/Input 库  | Workflow Canvas               | Inspector         |
|                |                               |                   |
| ToolSpec cards | [input] -> [tool] -> [output] | node/port config  |
| Search/filter  |                               | validation issues |
+----------------+-------------------------------+-------------------+
| Validation summary | Compile | Export | WDL preview                |
+--------------------------------------------------------------------+
```

窄屏第一阶段只保证可查看，不承诺完整移动端拖拽体验。

## 4. 编辑器状态分层

必须分为三层：

### 4.1 UI State

只用于交互：

- 当前选中节点/边；
- 面板开关；
- hover；
- 临时拖拽连接；
- zoom/viewport；
- undo/redo history；
- 编译请求状态。

### 4.2 Editor Document

用于画布保存：

- 节点位置；
- 节点显示信息；
- Graph 语义节点和边；
- layout。

### 4.3 Workflow Graph Contract

提交后端前必须通过 adapter 转换成 `schemas/workflow-graph.schema.json`。

```text
Vue Flow nodes/edges
       |
       v
editorToWorkflowGraph()
       |
       v
AJV validation
       |
       v
POST /validations/workflow-graph or /compilations
```

后端返回的 Graph 也必须通过 `workflowGraphToEditor()`，不允许组件直接假设 API 对象含有 Vue Flow 字段。

## 5. 推荐目录

```text
frontend/
  components/workflow/
    WorkflowCanvas.vue
    ToolPalette.vue
    WorkflowToolbar.vue
    ValidationPanel.vue
    WdlPreview.vue
    inspector/
      NodeInspector.vue
      ToolNodeInspector.vue
      WorkflowInputInspector.vue
      WorkflowOutputInspector.vue
    nodes/
      ToolNode.vue
      WorkflowInputNode.vue
      WorkflowOutputNode.vue
    edges/
      TypedEdge.vue
  composables/
    useWorkflowEditor.ts
    useConnectionValidation.ts
    useCompilation.ts
    useContracts.ts
  stores/
    workflow-editor.ts
  domain/
    editor-document.ts
    adapters.ts
    identifiers.ts
    diagnostics.ts
  generated/contracts/
  pages/
    index.vue
  tests/
```

`generated/contracts` 由机器 Schema 生成或同步，不手工维护重复类型。

## 6. 节点类型

### 6.1 Workflow Input Node

显示：

- label；
- WDL type；
- semantic type；
- required/optional；
- 一个 source handle：`value`。

用户可以创建：

- File 输入；
- String/Int/Float/Boolean 输入；
- 一维数组输入。

节点不得接受入边。

### 6.2 Tool Node

显示：

- ToolSpec display name；
- tool version；
- container 状态；
- 输入端口列表；
- 输出端口列表；
- 错误/警告数量。

每个 input/output port 对应一个稳定 handle ID：

```text
in:<port_name>
out:<port_name>
```

adapter 转换时去掉方向前缀，Graph 只保存真实 port name。

### 6.3 Workflow Output Node

显示：

- label；
- WDL type；
- semantic type；
- 一个 target handle：`value`。

必须恰好一个入边，不允许出边。

## 7. 节点 ID 与名称

Graph 节点 ID 必须是合法 WDL identifier。前端不得直接使用 UUID、带短横线的 nanoid 或中文名称作为 node ID。

建议：

```text
workflow input: input_reads_1
Tool node: fastp_1, fastp_2
workflow output: output_clean_reads_1
```

分配函数：

```ts
nextAvailableIdentifier(base: string, existing: Set<string>): string
```

规则：

1. 把 base 规范成 snake_case；
2. 处理 WDL 保留字；
3. 第一个实例使用 `<base>_1`；
4. 冲突时递增；
5. 用户可重命名，但需立即校验；
6. 重命名必须原子更新 edges 和 layout key；
7. display label 与 node ID 分离。

## 8. ToolSpec 输入方式

第一阶段支持两种：

### 8.1 JSON 导入/编辑

用户粘贴或上传 ToolSpec JSON：

- AJV 即时 Schema 校验；
- 后端 `/validations/tool-spec` 权威校验；
- 校验通过后加入当前编辑会话 Tool Palette；
- 不代表已发布到 Registry。

### 8.2 示例工具

开发环境预置 fastp 等 fixture，来源仍是标准 ToolSpec JSON，而不是前端硬编码节点。

第一阶段可增加结构化表单，但表单必须生成同一个 ToolSpec contract。不要同时维护“表单模型”和“JSON 模型”两套业务语义。

## 9. Tool Node Inspector

Inspector 分区：

1. Identity：alias、label、ToolSpec 版本和 digest；
2. Inputs：每个端口的来源状态；
3. Parameters：未连接标量输入的值；
4. Runtime：只读展示 ToolSpec 建议值，第一阶段不做 node-level override；
5. Diagnostics：与当前 node/port 相关错误。

参数控件按 WDL type：

| 类型 | 控件 |
|---|---|
| String | text input |
| Int | integer input |
| Float | number input |
| Boolean | switch/checkbox |
| Array[String/Int/Float/Boolean] | 可增删列表 |
| File/Array[File] | 第一阶段优先通过 Graph 边绑定 |

端口已有入边时，对应 parameter value 控件禁用，并提示“由上游节点提供”。

## 10. 连线校验

### 10.1 本地即时校验

拖动连接时，前端可快速检查：

- source/target 方向；
- 自连接；
- target 是否已有入边；
- WDL type 精确匹配；
- semantic type 精确匹配或 target 为 `core.file.any`；
- workflow input/output 方向限制。

本地校验只用于 UX，不是权威结论。

### 10.2 后端权威校验

以下情况必须调用后端：

- 完成一条边后 debounce 校验；
- 修改 parameter value；
- 修改 node ID/port contract；
- 导入 Graph；
- 点击 Validate；
- 点击 Compile。

后端返回 diagnostics 后，前端按 code/location 映射画布。

### 10.3 不自动修复

semantic mismatch 时前端可以显示：

```text
bio.bam.aligned 不能连接到 bio.bam.sorted
```

但不得自动插入 samtools sort。第二阶段 AI 可以提出建议，仍需用户确认。

## 11. Diagnostics UI

### 11.1 展示层级

- 全局 summary：errors/warnings；
- 节点 badge；
- port 状态；
- edge 状态；
- 底部问题列表；
- Inspector 详情。

### 11.2 定位优先级

1. `location.edge_id`；
2. `location.node_id + port`；
3. `location.node_id`；
4. JSON Pointer path；
5. 全局错误。

点击 diagnostic：

- `fitView` 到相关元素；
- 选中节点/边；
- 打开 Inspector；
- 滚动到端口；
- 显示 code、message、suggestion。

未知 code 仍显示服务端 message，不得丢弃。

## 12. Graph Adapter

### 12.1 editorToWorkflowGraph

必须：

- 丢弃 selection、hover、component state；
- 把 handle ID 映射为 port name；
- 分离 layout；
- 排除临时未完成 edge；
- 输出稳定字段；
- 不负责排序语义，后端仍会规范化；
- 通过 AJV 后才发送。

### 12.2 workflowGraphToEditor

必须：

- 为三种 node type 选择对应组件；
- 从 layout 读取位置，没有位置时使用自动布局；
- 根据 ToolSpec resolver 构造 tool handles；
- 未解析 ToolRef 显示 broken node，而不是删除节点；
- 保持 Graph 原始 ID。

### 12.3 Layout 不影响编译

保存位置会改变 document digest，但不能改变 semantic digest、IR 或 WDL。前端 E2E 必须覆盖拖动节点后 WDL 不变。

## 13. Undo/Redo

第一阶段建议支持本地 undo/redo，命令粒度：

- add/remove node；
- add/remove edge；
- move node；
- change parameter；
- rename node；
- change workflow input/output contract。

校验结果、compile status 和 WDL preview 不进入历史栈，它们是派生状态。

连续 move 事件需要合并，避免每个像素产生历史记录。

## 14. Compile 交互

### 14.1 按钮状态

- Graph Schema 本地失败：Compile disabled；
- 后端存在 error：Compile disabled 或点击后立即 rejected；
- warning：允许 Compile，显示确认状态；
- compiling：防重复提交；
- succeeded：显示 artifact tabs；
- rejected：聚焦 validation panel；
- failed：显示 request ID 和系统错误。

### 14.2 Artifact 预览

Tabs：

- WDL；
- Inputs template；
- Compiler IR；
- Manifest；
- Diagnostics。

WDL 预览只读。第一阶段不支持编辑生成 WDL 后继续运行。

### 14.3 导出

支持逐个下载和 ZIP 下载可以二选一；最小 MVP 至少支持：

- `workflow.wdl`；
- `inputs.template.json`；
- `compile-manifest.json`；
- `compiler-ir.json`（debug/验收模式）。

## 15. 草稿与持久化

第一阶段不建立正式 Workflow Version 表。

可选实现顺序：

1. 浏览器内存；
2. 浏览器 IndexedDB/localStorage（仅非敏感草稿）；
3. 导入/导出 editor document JSON；
4. 第二阶段进入服务端项目和版本管理。

不要把浏览器 localStorage 当成平台版本管理。

## 16. Contract 类型生成

推荐流程：

```text
schemas/*.json
  -> generate TypeScript types
  -> generated/contracts
  -> frontend typecheck
```

可使用 `json-schema-to-typescript` 或等价工具。生成文件：

- 禁止手改；
- CI 检查是否与 Schema 同步；
- adapter 和 API client 使用生成类型；
- Vue Flow 专属类型定义在 domain/editor-document 中。

## 17. 前端测试

### 17.1 Unit

- identifier 分配；
- handle/port 转换；
- Graph adapter；
- diagnostic 定位；
- 本地连线规则；
- parameter value 序列化。

### 17.2 Component

- 三种节点渲染；
- Inspector 类型控件；
- Validation Panel；
- WDL preview；
- ToolSpec import。

### 17.3 E2E

fastp happy path：

1. 导入 fastp ToolSpec；
2. 添加两个 workflow input；
3. 拖入 fastp；
4. 连接 reads；
5. 设置 threads；
6. 添加 outputs；
7. Validate；
8. Compile；
9. WDL 与 golden fixture 一致；
10. 移动节点后重新 Compile，WDL digest 不变。

negative path：连接错误 semantic type，画布正确高亮并显示 `WG013`。

## 18. 可访问性与可用性

- 所有 port 有可读 label 和 type tooltip；
- 不只依赖颜色表示 error/warning；
- Validation list 可键盘导航；
- Inspector 表单 label 与 input 关联；
- 支持删除选中节点/edge 的键盘操作，并有撤销；
- 画布缩放不影响属性面板可读性；
- 错误 message 支持中英文国际化，但 code 保持不变。

## 19. 第一阶段前端验收

1. 可导入并校验标准 ToolSpec；
2. 可创建三类节点；
3. Tool ports 来自 ToolSpec，不是硬编码；
4. 可建立合法端口连线；
5. 非法连接在本地提示且后端返回稳定 code；
6. 可配置未连接标量参数；
7. 可导出符合 Graph Schema 的 JSON；
8. layout 与编译语义分离；
9. fastp 流程可生成 golden WDL；
10. diagnostics 能定位到 node/edge/port；
11. 生成产物只读并可导出；
12. TypeScript strict、lint、unit、E2E 通过。
