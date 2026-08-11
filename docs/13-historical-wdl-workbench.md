# 历史 WDL 资产与工作台

## 定位

历史 WDL 工作台用于接管已经在线运行、但尚未进入 Workflow Graph 模型的 WDL。
它与流程编译器的只读生成结果保持边界：

- Workflow Graph 仍是新流程的核心模型，生成 WDL 不支持反向同步；
- 历史 WDL 作为独立资产导入、检索、分析和维护；
- 经过人工拆解确认后，task 才能进入通用工具库，不能因解析成功而自动发布。

页面入口为 `/wdl`，资产详情为 `/wdl/:slug`。

## 数据与审计

每个 `WDLAsset` 保存名称、源文件名、说明、生命周期和标签。生命周期字段继续保留
用于兼容已有数据和 API，但当前页面不展示、不要求用户维护。源码变更通过
`WDLSourceRevision` 形成不可变版本，至少记录：

- 操作类型：导入、编辑或格式化；
- 操作者与 UTC 时间；
- 完整源码、sha256 digest 和相对上一版本的 unified diff；
- 本次操作备注；
- 当时的解析结果、task/workflow/import 索引和诊断。

标签等元数据变化不重写源码版本，而是写入 `WDLAuditEvent`。当前项目尚未
接入用户认证时，操作者统一记录为 `local-user`；认证上线后取登录用户名。

资产列表上方展示总标签池及每个标签的使用数。单击标签筛选资产，双击名称原地
重命名；重命名会同步所有关联 WDL，并分别写入元数据审计。只有使用数为 0 的标签
显示删除入口，后端也会拒绝删除正在使用的标签。

源码相对当前版本第一次发生变化时，“变更”标签立即出现，并持续展示工作副本的
unified diff。保存成功后工作副本归零，“变更”隐藏，完整 diff 进入对应版本和操作
历史；格式化仍会自动打开“变更”。

工作台支持按钮或 `Shift + Alt/Option + E` 导出当前源码，文件名格式为
`<WDL名称>-v<版本>[-draft]-YYYYMMDD-HHmmss.wdl`。存在未保存内容时必须带
`draft`，避免与已保存版本混淆。

## 格式化规则

导入时必须保留原始源码，不静默格式化。用户在工作台点击“格式化”后先看到 Diff，
确认保存才创建新的 `format` 版本。

按钮和 `Shift + Alt/Option + F` 快捷键调用同一格式化流程。格式化结果通过 Monaco
原地 edit 写回，必须保留编辑器尺寸、滚动位置、光标、选区和键盘焦点；Diff 显示在
右侧检查器，不得通过插入底部面板改变编辑区高度。

WDL 1.0 及以上版本使用固定为 `v0.28.0` 的 Sprocket formatter。运行配置见
`docker/sprocket-format.toml`：

- WDL 结构统一为两空格层级缩进；
- 声明、赋值、运算符、逗号等内部空格统一；
- task、workflow 的结构区块之间补充单个空行；
- 最大行宽为 120，不排序 imports 和 inputs，不主动增加尾逗号；
- `command` 跟随 task 层级，但保留命令正文中的相对空格和空行；
- Windows CRLF 和旧 Mac CR 换行统一为 LF；
- 格式化前后都必须能被 miniwdl 解析，且重复格式化结果不再变化。

没有 `version` 声明的历史 draft-2 WDL 继续使用原有的保守缩进器，避免格式化动作
隐式升级 WDL 语言版本。Sprocket 不可用或超时时，接口返回
`WDL_FORMATTER_UNAVAILABLE`，不回退到弱格式化结果。

## API

```text
GET    /api/v1/wdl-assets
POST   /api/v1/wdl-assets
GET    /api/v1/wdl-assets/tags
POST   /api/v1/wdl-assets/tags
PATCH  /api/v1/wdl-assets/tags/{tag_id}
DELETE /api/v1/wdl-assets/tags/{tag_id}
GET    /api/v1/wdl-assets/{slug}
PATCH  /api/v1/wdl-assets/{slug}
GET    /api/v1/wdl-assets/{slug}/revisions
POST   /api/v1/wdl-assets/{slug}/revisions
GET    /api/v1/wdl-assets/{slug}/revisions/{version}
POST   /api/v1/wdl-assets/{slug}/format
```

修改资产信息时必须提交 GET 响应中的 `metadata_version` 作为
`base_metadata_version`；保存源码新版本时必须提交当前 revision 的
`version` 与 `digest` 作为 `base_version`、`base_digest`。缺少基线返回 428，基线过期
返回 409，并附带最新资产或源码版本，客户端不得自动覆盖。

### 并发写入契约升级

迁移 `0013_wdl_asset_metadata_version` 启用后，上述基线字段由可选参数升级为必填参数。
这是防止多用户互相覆盖的写入契约升级，后端与前端必须在同一发布窗口部署。升级前：

1. 停止仍在调用 WDL 写接口的旧页面和脚本；
2. 资产 PATCH 调用先 GET 资产并回传 `base_metadata_version`；
3. revision POST 调用先 GET 当前版本并回传 `base_version`、`base_digest`；
4. 客户端对 409 保留本地内容并要求合并，对 428 视为旧调用方尚未升级；
5. 部署后分别执行一次元数据保存和源码保存冒烟测试，再恢复其他调用方。

仓库内工作台已使用新契约。外部脚本不能跨过 428 自动重试，否则会重新引入
last-write-wins；必须读取最新基线后由用户确认合并。

API 继续支持关键字、标签和生命周期筛选，当前页面只保留关键字和标签。导入允许
暂时无法解析的旧 WDL 入库，诊断会随导入版本一起保存；格式化和后续版本保存仍要求
源码可解析。

## 当前边界与后续拆解

当前版本先完成单文件资产管理、语法高亮、辅助编辑、格式化、结构索引、版本和审计。
后续按以下顺序扩展：

1. 多文件 WDL/import 包上传与依赖锁定；
2. task 候选抽取、差异识别和人工校正；
3. task 候选映射为 `ToolSpecDraft`；
4. 通过现有校验与发布流程进入通用工具库；
5. 历史 WDL 引用已发布工具后的逐步重构和迁移状态跟踪。

“解析出 task”只代表候选识别成功，不等同于工具已可复用或已发布。
