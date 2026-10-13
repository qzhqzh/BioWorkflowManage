# 独立数据服务接入与迁移

本次兼容分类：**additive（可选接入）+ migration（部署时的数据所有权切换）**。受影响方为 BWM API、所有分析 Worker、旧 rawdata-indexer、OKBOX 及直接调用 rawdata 接口的上游。

## 职责与契约

新项目 `DataManagementService` 独立维护数据目录、扫描、R1/R2 配对、版本及复制完成快照，具有独立模型、迁移、依赖锁和 API/indexer Dockerfile。它不导入 BWM 模型。BWM 维护流程、分析任务、运行状态、产物与追溯；管理页面中的分析历史按已冻结的 `request_payload.dataset/control_dataset` 从本地 AnalysisRun 汇总。

设置 `DATA_SERVICE_URL`（服务 origin，不带 `/api`）、`DATA_SERVICE_TOKEN_FILE`，API 和所有 Worker 一致切换。超时默认 `DATA_SERVICE_TIMEOUT_SECONDS=10`。Token 需要 `data:read`、`data:verify`；管理台触发扫描还需 `data:scan`。BWM 管理目录需要数据根 `.` 授权；各业务系统直接使用数据服务时可配置更窄目录。BWM Integration API 继续沿用已有 analysis scope；该兼容入口不额外承诺逐上游 rawdata 物理隔离。

- 未配置时保留旧内嵌实现及既有部署默认值。
- 配置后通过 HTTP 访问数据服务，不查询新服务的数据库，也不在旧数据表建立新 run 外键。
- 原 `GET /api/v1/integration/rawdata-datasets` 及 readiness 请求/响应与 scope 不变，移除新服务原生 `version` 扩展以保持旧封闭 schema；上游无需同时升级。
- 管理目录和手动扫描入口也代理到新服务。旧 indexer 在此配置下拒绝运行，防止两边继续写不同索引。
- 接受任务前和 Worker 执行前均调用数据服务验证同一冻结快照；Worker 还保持原有本地文件清单核验。
- 网络、凭据和协议故障返回 `DATA_SERVICE_UNAVAILABLE`（503、可重试），不会自动读取旧索引；确切的快照变化仍作为输入错误。此故障错误是新增可诊断错误，不改变执行成功与产物交付语义。

核心外部契约 `schemas/integration-openapi-v1.json` 及历史基线不修改。新服务原生契约在其 `schemas/openapi-v1.json`。大型 FASTQ 仍通过共享受管存储读取，不经两个 API 转发文件内容。

## 切换顺序

1. 构建新数据 API、数据 indexer，以及带适配代码的 BWM API/各 Worker 镜像。准备独立 PostgreSQL 数据库和数据服务账号；原始数据保持相同内容及只读挂载。
2. 进入维护窗口：暂停所有 rawdata 请求/提交，排空分析或明确暂停任务领取；等当前扫描完成，停止旧索引器。readiness 请求也会写 changed 状态，因此只停止扫描还不够。
3. 备份旧数据库。用 `python backend/manage.py export_data_service --output /secure/rawdata-export.json` 导出；命令只读且拒绝活动扫描、覆盖文件。
4. 新服务显式执行数据库迁移，配置 `DATA_ROOT_KEY` 为导出的 root_key。先 `import_bwm_data <file>` 检查，再 `--apply` 导入空的数据目录库；保留 dataset_id、身份摘要、标记代次、changed 状态和事件，不迁走 AnalysisRun。
5. 启动新数据 API/indexer，核对目录与历史受阻批次，给 BWM 配置 URL 和独立 Token；全部 API/Worker 使用一致配置。`deploy/data-service.compose.yml` 是可选叠加文件，共用受控 integration 网络。仍需显式停止已有旧索引器容器，profile 只影响后续选择。
6. 先通过 BWM 旧入口验收预检、提交、幂等、执行前复核及故障阻断，再将 OKBOX 数据读取切到原生数据 API。恢复入口及任务领取。

API、Worker 与数据服务的路径可以不同，但必须是同一批共享文件。root_key 是持久身份，迁移时不可因路径字符串变化而生成新值。不得把新扫描当成已发生变化批次的重新认可。

## 回滚和备份

不删除 BWM 旧模型和 migration，旧部署可继续使用它们。切换前可放弃新实例继续旧服务；切换后如产生新状态，不可直接取消 URL 配置并恢复旧库，否则可能丢失 changed 锁存。优先保留新数据库并回退兼容它的应用版本；如需恢复内嵌模式，暂停提交并另行规划状态迁回。运行结果与业务库备份保持原规则，新增数据服务元数据备份。

## 验收证据与范围

- 旧 rawdata、Integration API、历史契约基线、Reference Connector 回归。
- 新远端适配测试：scope、响应形状、故障无回退、旧 indexer 禁用、Worker 远端与本地双重核验。
- 实际独立 HTTP 进程：目录、预检、幂等提交基础路径、冻结快照、执行前复核、完成标记移除后阻断、BWM 自有分析历史。
- 从旧 BWM 导出到另一个数据库，检查保持 changed 状态和 dataset_id；重复导入非空目标被拒绝。
- OKBOX 实际客户端读取并复核，BWM 客户端被禁止调用仍成功。

```bash
uv run python scripts/validate_contracts.py
uv run pytest backend/tests/test_data_service.py backend/tests/test_rawdata_readiness.py backend/tests/test_rawdata_catalog.py backend/tests/test_integration_api.py backend/tests/test_integration_compatibility.py backend/tests/test_reference_connector.py
DATA_SERVICE_SOURCE=/path/to/DataManagementService OKBOX_API_SOURCE=/path/to/okb/api uv run pytest backend/tests/test_data_service_live.py
```

上述测试使用合成数据和隔离数据库，不执行 LC103 全流程，不表示现有部署已切换或已完成临床流程验收。
