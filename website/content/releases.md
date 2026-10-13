# 版本与兼容
固定底座版本，独立维护流程与业务应用，保留可追溯的升级记录。

## v1.1.0 基础版 {#current}

发布于 2026 年 10 月 13 日，源码提交 `2087547268444c6327fa74f48968c74edd578f78`。

- [正式 Release](https://github.com/qzhqzh/BioWorkflowManage/releases/tag/v1.1.0)
- [固定版本源码](https://github.com/qzhqzh/BioWorkflowManage/tree/v1.1.0)
- [源码 ZIP](https://github.com/qzhqzh/BioWorkflowManage/archive/refs/tags/v1.1.0.zip)
- [源码 TAR.GZ](https://github.com/qzhqzh/BioWorkflowManage/archive/refs/tags/v1.1.0.tar.gz)
- [OpenAPI 文档 1.7.0](/openapi.json)

本次软件发布包含版本标签、正式 Release、源码归档和兼容说明。未提供完整双引擎离线包，也不会自动升级任何已部署环境。

## 验证范围 {#validation}

发布候选的七项 CI 全部通过，浏览器测试 60/60 通过。包括契约与 golden、后端、PostgreSQL 并发、前端、真实 MiniWDL smoke、浏览器、Headless API 任务执行。

[查看发布验证记录](https://github.com/qzhqzh/BioWorkflowManage/actions/runs/38372882281)。合并提交与该候选的源码树一致。平台验证不等同于任意业务流程或任意生产环境已通过验收。

## 兼容原则 {#compatibility}

| 对象 | 原则 |
| --- | --- |
| API 路径 | 继续使用 /api/v1/integration |
| 既有请求与状态 | 不静默改变既有调用语义 |
| 分析产品 | analysis_code + contract_version 固定绑定，不原地替换 |
| 新能力 | 显式、可选启用，旧配置保留原行为 |
| 历史记录 | 保留流程、输入与契约快照 |

数据服务拆分、Panel 输入扩展和跨引擎并发上限属于按需配置的能力。切换数据所有权需要单独迁移；不能只改一个 URL 就视为迁移完成。

## 后续迭代 {#next}

先围绕 v1.1.0 完成独立部署、单流程验证与业务联调。按反馈修复确定的问题，重新验证受影响链路，再确定下一个版本。

兼容修复使用补丁版本，可选新增能力使用次版本；公共行为需要迁移时先明确并行契约、接入顺序和回滚方案。具体计划以已确认的问题与发布记录为准。

产品站与底座分别维护；文档更新不表示底座软件已升级。当前产品站源码位于仓库的独立产品页分支。
