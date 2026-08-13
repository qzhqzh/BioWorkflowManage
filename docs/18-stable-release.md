# v1.0.0 稳定版本发布与升级

## 发布边界

本稳定版本固定以下产品主链路：WDL 资产与工具包维护、多人评审与行级讨论、不可变版本
发布、原始数据后台索引、资源清单、运行分析、工具单测和运行结果审计。此后小版本只做
修复与打磨；新增核心模型、一级菜单或执行模式必须单独立项并提供迁移和回滚方案。

## 升级顺序

升级包含数据库迁移 `0023` 至 `0026`，新增 WDL 协作、原始数据索引和 WDL 发布证据表，
并约束同一用户在同一资产上只能保留一个待解决源码冲突。
迁移只新增表与约束，不改写已有 WDL revision、运行记录或原始数据。

```bash
# 1. 备份 PostgreSQL；备份文件必须落在项目数据目录之外
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-bioworkflow}" \
  -d "${POSTGRES_DB:-bioworkflow}" -Fc > /path/to/backup/bioworkflow-before-stable.dump

# 2. 停止会创建运行记录或索引记录的 worker
docker compose stop analysis-worker rawdata-indexer
docker compose --profile wdl-host-runtime stop analysis-worker-host

# 3. 拉取稳定 tag 后构建并执行迁移
docker compose build backend frontend
docker compose up -d db backend
docker compose exec backend python backend/manage.py migrate

# 4. 启动页面、后台索引和所选执行 worker
docker compose up -d frontend gateway rawdata-indexer
docker compose --profile wdl-host-runtime up -d analysis-worker-host
```

首次启动 `rawdata-indexer` 会分批建立清单；原始文件只读挂载，不会被移动或修改。页面在
首次完整扫描前显示“正在建立清单”，之后始终优先展示最近一次完整成功快照。

## 验证清单

```bash
docker compose ps
docker compose exec backend python backend/manage.py showmigrations workflows
curl -fsS http://127.0.0.1:${APP_PORT:-8082}/api/v1/health
```

登录后确认：

1. 历史 WDL 详情可打开“协作”，能看到评审人、讨论和发布检查；
2. 原始数据页显示后台索引状态，点击“更新清单”后页面仍保留上次成功数据；
3. 运行分析能选择索引中状态为“可运行”的数据集；
4. `docker compose logs rawdata-indexer` 没有持续失败或权限错误；
5. 使用小数据完成一次运行后，WDL 发布检查能引用该运行并形成证据。

## 回滚

应用回滚前停止 `analysis-worker`、`analysis-worker-host` 与 `rawdata-indexer`。代码可回到
上一稳定 tag；新增表对旧代码无影响，因此正常回滚不执行逆向 migration，也不删除任何
表或 Docker volume。若必须恢复数据库，使用升级前的 `pg_dump` 在独立数据库中验证后再
切换，不能直接覆盖唯一生产副本。

## 发布检查契约

默认模板包含：语法与静态检查、imports 完整、工具包版本固定、当前 revision 已通过评审、
没有未解决讨论、当前 revision 有成功的小数据运行。管理员可调整启用项与小数据输入上限；
模板修改使用 `base_policy_version`，过期请求返回 409。

稳定发布必须携带 `base_version`、`base_digest` 与已通过的 `release_check_id`。服务端在事务
内复核源码和动态证据，任何变化都会拒绝发布；`WDLAssetRelease` 形成后不可修改或删除。
