# 快速安装
在独立 Linux 环境中部署 v1.1.0，先验证访问，再验证执行。

## 环境准备 {#requirements}

准备 Linux 主机、Git、curl、Python 3、Docker Engine 与 Docker Compose v2。当前用户须能执行 `docker info`。首次构建需要访问代码仓库、依赖源与镜像源。

```bash
git --version
python3 --version
docker info
docker compose version
```

算力与存储按流程评估。Worker 默认在可用内存低于 40 GiB 时等待，这不是 Web/API 的内存要求，也不是每个任务分配 40 GiB。不要为消除等待而盲目降低阈值。

> 本页用于全新试部署。已有环境请保留原 .env、数据库和运行数据，参照部署与升级说明。

## 1. 获取固定版本 {#source}

```bash
git clone --branch v1.1.0 --depth 1 \
  https://github.com/qzhqzh/BioWorkflowManage.git
cd BioWorkflowManage
git rev-parse HEAD
```

本版提交应为 `2087547268444c6327fa74f48968c74edd578f78`。也可以从 [v1.1.0 Release](https://github.com/qzhqzh/BioWorkflowManage/releases/tag/v1.1.0) 下载源码归档。

## 2. 初始化试用配置 {#configure}

下载并阅读本站提供的 [初始化脚本](/downloads/init-local-v1.1.0.py)，再在源码根目录执行。脚本为新环境生成独立随机密钥，创建数据目录，将端口绑定到 `127.0.0.1:8082`；不启动服务，不创建账号，遇到已有 .env 会停止。

```bash
curl --fail --show-error --location \
  https://bwm.qzhqzh.com/downloads/init-local-v1.1.0.py \
  --output init-local-v1.1.0.py
python3 init-local-v1.1.0.py
docker compose config --quiet
```

生成的 `.env` 仅当前用户可读写。默认测试账号保持关闭；HTTP Cookie 配置只用于本机或 SSH 隧道试用。手动配置和 HTTPS 部署见 [部署与运维](/docs/deployment/)。

## 3. 启动服务 {#start}

```bash
docker compose up -d --build --wait
docker compose ps
curl --fail --show-error http://127.0.0.1:8082/api/v1/health
curl --fail --show-error http://127.0.0.1:8082/api/v1/ready
```

首次构建可能需要较长时间，以构建输出和容器健康状态为准。健康检查成功只说明服务可访问，下一步仍需配置执行引擎。

## 4. 创建管理员并登录 {#login}

```bash
docker compose exec backend python backend/manage.py createsuperuser
```

按提示设置账号和密码，浏览器打开 `http://localhost:8082`。管理员默认进入 `/operations` 运维控制台。账号由部署者创建，没有本站提供的通用密码。

如果部署在远程服务器，从自己的电脑建立隧道，然后访问本机地址：

```bash
ssh -L 8082:127.0.0.1:8082 your-user@your-server
```

共享局域网访问和正式 HTTPS 地址的配置见 [访问方式](/docs/deployment/#access)。

## 5. 验证执行引擎 {#verify}

先运行不需要业务数据的真实容器 smoke。它验证 MiniWDL、隔离 Docker 与输出落盘的链路：

```bash
./scripts/miniwdl.sh doctor
./scripts/miniwdl.sh smoke
```

成功时命令退出码为 0，运行证据保存在 `data/miniwdl/work/runs/`。这是一项执行环境测试，不会在控制台创建业务运行记录。

准备通过页面或 Integration API 执行 MiniWDL 产品时，再启动 Worker：

```bash
docker compose --profile wdl-runtime up -d --build \
  miniwdl-docker analysis-worker
docker compose --profile wdl-runtime ps
```

运行环境需要 Docker-in-Docker 的 privileged 能力；只用于受信任的流程。宿主机或 VPN 已占用 `10.253.0.0/24`、`10.253.1.0/24` 时，先在 .env 中选择未冲突的 `MINIWDL_CONTROL_SUBNET`、`MINIWDL_EGRESS_SUBNET`。Nextflow 另需独立引擎、产品包和资源配置，见 [执行引擎](/docs/deployment/#engines)。

## 完成与排障 {#finish}

完成标准是：页面可登录、健康检查正常、smoke 退出码为 0、运行证据可读取。记录实际环境，再进入 [第一次使用](/docs/usage/) 和 [测评清单](/docs/evaluation/)。

| 现象 | 优先检查 |
| --- | --- |
| 镜像或依赖拉取失败 | 主机联网、镜像源、Docker 全局代理及磁盘空间 |
| 登录后仍未认证 | HTTP 与 Secure Cookie 配置、访问地址和 CSRF 来源是否匹配 |
| Network overlaps | 执行隔离网段与已有 Docker / VPN 网段冲突 |
| 任务停在 queued | 是否启用对应 Worker、内存门槛、产品所需资源和并发上限 |
| Permission denied | Worker UID/GID 与目录属主、只读挂载、NAS 权限 |

查看日志，不要直接贴出 .env 或包含 Token 的完整请求：

```bash
docker compose logs --tail=100 backend gateway
docker compose --profile wdl-runtime logs --tail=100 analysis-worker
```

停止试用服务可使用 `docker compose --profile wdl-runtime stop`；数据目录会保留。
