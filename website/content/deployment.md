# 部署与运维
明确服务、执行与数据边界，为试用和后续生产评审准备可重复的部署。

## 服务组成 {#services}

| 服务 | 职责 |
| --- | --- |
| gateway / frontend | 同源 Web 界面与 API 入口 |
| backend / db | 认证、产品契约、任务状态与 PostgreSQL 数据 |
| rawdata-indexer | 原始数据索引，不移动原始文件 |
| analysis-worker | MiniWDL 任务执行；按 profile 显式启用 |
| analysis-worker-nextflow | Nextflow 任务执行；按 profile 显式启用 |
| webhook-dispatcher / artifact-exporter | 可选通知与结果交付的后台处理 |

根目录 Compose 是本版源码部署入口。业务应用与底座分开部署，Token 调用只在后端进行。生产准入按本机构的实际网络、资源和运维条件验证。

## 配置与访问方式 {#access}

手动部署时，首次复制 `.env.example` 为 `.env`，填写两个独立的随机值：`POSTGRES_PASSWORD`、`DJANGO_SECRET_KEY`。可分别执行 `openssl rand -hex 32` 生成，不要复用。已有配置不覆盖；已有数据库不能只改 .env 密码来轮换账号。

| 配置 | 本机试用 | HTTPS 部署 |
| --- | --- | --- |
| APP_PORT | 127.0.0.1:8082 | 按反向代理选择绑定地址与端口 |
| DJANGO_ALLOWED_HOSTS | localhost,127.0.0.1,[::1] | 保留健康检查地址并添加实际域名 |
| DJANGO_CSRF_TRUSTED_ORIGINS | http://localhost:8082,http://127.0.0.1:8082 | https://analysis.example.org |
| DJANGO_SESSION_COOKIE_SECURE | 0 | 1 |
| DJANGO_CSRF_COOKIE_SECURE | 0 | 1 |
| DJANGO_TRUSTED_PROXY_COUNT | 默认 gateway 为 1 | 按真实可信代理层数调整 |

若临时在可信内网通过 HTTP 共享，修改 `APP_PORT=8082`、允许访问的主机和完整 CSRF 来源，并限制主机防火墙。HTTP 不加密凭据；共享使用建议配置 HTTPS。不要关闭认证来处理登录问题。

官网 `bwm.qzhqzh.com` 提供介绍与文档；你的分析服务使用自己的部署地址。

## 数据与权限 {#data}

| 数据 | 默认位置 | 管理要求 |
| --- | --- | --- |
| PostgreSQL | data/postgres | 持久化、备份并验证恢复 |
| 原始数据 | workspace/rawdata | 平台只读；完成投递后保持输入不变 |
| 参考资源 | workspace/databases | 固定资源清单，平台只读 |
| 分析结果 | data/analysis-runs | Worker 可写，API 只读；按保留规则管理 |
| 输入暂存 / 缓存 | data/input-staging、data/analysis-cache | 监控容量和目录权限 |
| 引擎状态 / 证书 | data/miniwdl-engine、data/miniwdl-certs | 不跨部署复用私钥；保留所需引擎缓存 |

Worker UID/GID 默认 1000；试用初始化器改为执行者的 UID/GID。挂载 NAS 前确认目录存在、存储已挂载、Worker 能读输入和写结果。不能通过全目录 777 掩盖属主错误。

## 执行引擎 {#engines}

MiniWDL 隔离模式：

```bash
docker compose --profile wdl-runtime up -d --build \
  miniwdl-docker analysis-worker
```

宿主 Docker 模式使用 `wdl-host-runtime` 与 `analysis-worker-host`。必须按 `.env.host.example` 配置宿主绝对路径，并使 API、Worker 和任务容器看到一致的数据。`MINIWDL_DOCKER_GID` 使用本机 `stat -c '%g' /var/run/docker.sock` 的结果。

Nextflow 模式使用 `nextflow-runtime` 与 `analysis-worker-nextflow`。四组 rawdata、database、run、input-staging 的 HOST_PATH / EXECUTION_ROOT 要使用相同的宿主绝对路径；部署方还须提供审核后的流程源码、固定镜像摘要与参考资源。

```bash
docker compose --profile nextflow-runtime up -d --build \
  nextflow-docker analysis-worker-nextflow
```

这条命令只启动执行服务，不安装任何业务流程包。Nextflow 产品由部署方按固定 manifest 导入、发布；业务请求不传任意源码、引擎参数或镜像。

两种引擎可按产品需要选用。Docker-in-Docker 需要 privileged 权限；生产环境应评估独立 Worker/VM、网络隔离与资源限制。Service Account 任务隔离不等于不可信代码的安全沙箱。

## 容量与并发 {#capacity}

`ANALYSIS_MIN_AVAILABLE_MEMORY_GB` 和 `NEXTFLOW_MIN_AVAILABLE_MEMORY_GB` 默认 40，表示开始领取任务前的可用内存门槛。按实际流程峰值、并行任务和系统预留资源评估。

可选全局 `ANALYSIS_MAX_ACTIVE_RUNS` 为 0 时保持原行为。启用正数上限要求 PostgreSQL，各执行 Worker 配置一致。输入暂存、输出快照与结果导出各有磁盘预算，结果目录需要为校验快照预留额外空间。

独立数据服务是可选部署能力；新环境可保留内嵌索引。已有数据所有权切换须单独计划迁移与回滚。

## 备份与恢复 {#backup}

在项目数据目录外创建受限备份目录，再备份数据库。下列示例使用默认数据库名与账号，自定义部署需替换为实际值。

```bash
umask 077
mkdir -p ../bwm-backups
docker compose exec -T db pg_dump \
  -U bioworkflow -d bioworkflow -Fc \
  > ../bwm-backups/bioworkflow-before-upgrade.dump
```

同时按一致性窗口保存 .env、资源清单、流程包、原始数据引用、运行结果及交付记录。数据库备份不包含这些文件。加密或受限保存密钥，不能提交到 Git。

先在独立环境演练数据库与文件恢复，检查固定产品、历史任务、输出摘要、账号和权限。恢复成功后再记录可接受的数据丢失窗口与恢复时间；不要在正式库上试验恢复命令。

## 升级与回滚 {#upgrade}

1. 固定当前底座、流程、资源和镜像版本，保留完整可恢复备份。
2. 停止新的业务投递，妥善处理在途任务，再停止对应执行 Worker。
3. 对照目标版本迁移说明；backend 启动会执行数据库迁移。
4. 验证登录、固定产品、旧请求、任务查询和结果交付，再逐步恢复流量。
5. 回滚方案同时考虑代码、数据库与文件；不能假定只换旧镜像就完成回滚。

本版的历史升级涉及输出清单等迁移，已有环境由维护者确认具体起点。全新安装指南不能直接当作旧环境升级脚本。

## 日常检查 {#operations}

持续观察服务健康、Worker 心跳、队列等待、执行错误、磁盘容量、备份结果及回调/导出积压。出错时保留时间点、运行 ID 和稳定错误码，使用 [反馈模板](/docs/feedback/) 记录。

首次进入生产前，以 [试部署与测评](/docs/evaluation/) 的结果作为评审输入。
