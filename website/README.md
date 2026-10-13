# BioWorkflowManage 产品站

公开地址：<https://bwm.qzhqzh.com>。源码独立维护在 `codex/product-site` 分支，当前介绍基础版 v1.1.0。

首页采用白底、蓝色线条、四阶段执行图。公开场景统一使用 Web 应用、业务后台和自动化任务。文档覆盖源码安装、部署、首次使用、API 接入、试部署测评和问题反馈；AI 入口是 `/llms.txt`、`/llms-full.txt`、`/docs/ai-context.md` 与固定版 `/openapi.json`。

## 本地构建

需要 Git 和 Python 3.12+。无运行时 JS 框架、外部字体、统计服务或站点数据库。

```bash
python3 -W error website/build.py
python3 website/check.py
python3 -m unittest discover -s website/tests -v
node --check website/assets/site.js
python3 -m http.server 38120 --bind 127.0.0.1 --directory website/dist
```

打开 <http://127.0.0.1:38120>。`build.py` 只重建 `website/dist`；保留仓库历史，以便读取 `site.json` 固定的发布提交。生成物不提交到 Git。生产镜像使用 Nginx，能正确返回 UTF-8 文本及 HTTP 404。

`check.py` 检查本地链接、锚点、HTML 结构、OpenAPI 原样导出、下载摘要及公开内容。可以在本地通过 `BWM_SITE_DENY_TERMS` 传入以 `|` 分隔的私有名称进行额外检查；不要把这些名称写入源码或 CI 日志。

## 内容与版本

- `content/*.md`：网页与公开 Markdown 的共同来源。
- `site.json`：底座版本、发布提交、API 文档修订、官网与文档日期。
- `assets/`：响应式样式、复制、测评本机保存及反馈下载。
- `downloads/`：新环境初始化器、请求结构和反馈模板；构建时生成 SHA256SUMS。
- `version.json`：线上底座、站点提交与 OpenAPI 摘要，供部署核对。

下载初始化器仅适用于新建 v1.1.0 Linux 源码目录，生成随机凭据和 loopback 访问配置。它拒绝覆盖 `.env`、拒绝 root 和符号链接目录，不启动服务、不创建账号。底座 API 和数据库部署行为没有因产品站而改变。

## 独立站点部署

在长期保留的 checkout 中构建。该 Compose 使用固定摘要的 Nginx 镜像，只读挂载构建产物与站点配置，不依赖额外镜像构建，也不连接底座数据库和执行 Worker。

```bash
python3 website/build.py
python3 website/check.py
docker compose -f website/deploy/compose.yml up -d --force-recreate --wait
curl --fail http://127.0.0.1:18820/version.json
```

服务复用 Docker 默认网桥，仅绑定 `127.0.0.1:18820`，以非 root 用户、只读文件系统运行，不创建额外应用子网。`deploy/gateway.conf` 是指定域名的反向代理配置，TLS 配置继承现有网关；在其他主机上部署须自行配置域名证书和网关。网关先验证配置，再平滑加载。不要修改其他域名或重启业务容器。

构建会替换 `dist/` 目录，因此每次更新后须用 `--force-recreate` 重新挂载新产物；不要仅重启旧容器。发布后核对 HTTPS 首页、`/docs/quickstart/`、`/llms.txt`、`/openapi.json`、`/version.json` 以及不存在页面的 404。静态内容要求重新验证缓存，若接入 CDN，不应把 HTML 与版本清单配置为永久缓存。

部署前记录旧的站点提交。回滚时检出该提交，重新构建、检查，再执行上面的容器重建命令；底座数据不参与站点回滚。需要打包静态镜像时，也保留了可选的 `deploy/Dockerfile`；当前默认部署不需要它。

## 验收范围

手机与桌面检查页面溢出、导航、复制源码与 AI 接入说明、测评保存/下载和反馈文件生成。反馈表单没有提交接口；使用者检查下载内容后自行交给维护者。

源码安装说明基于固定版本及配置验证。真实业务流程、资源规模和生产适用性仍由同事按测评页记录，问题以修复版本和复测证据闭环。底座 Release 自身的 CI 结果见公开版本说明。
