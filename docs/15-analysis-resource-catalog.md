# 分析资源中心

## 目标

资源中心统一管理运行分析依赖的参考基因组、Panel、BED、gene BED、基因列表、CNV
基线和附加数据库文件。目录只保存相对路径和校验信息，不复制 FASTA、BED 或数据库大文件。

资源目录采用不可变修订记录：每次保存必须携带页面读取时的 `base_version` 和
`base_digest`。并发修改返回 HTTP 409，操作者、备注和变更对象会写入修订记录。

## 新环境初始化

应用迁移完成后执行：

```bash
docker compose exec backend \
  python backend/manage.py migrate_resource_catalog --actor zhuqin
```

命令会把现有 `catalog.json` 纳管，并加入血液肿瘤 hg38 资源清单及 84、396、624 三个
Panel 模板。该命令可重复执行，不会复制数据库文件，也不会替用户猜测 BED 路径。

随后由管理员或 `workflow_maintainer` 打开 `/resources`，为 Panel 配置缺失的 `bed`、
`gene_bed` 等相对路径。路径相对于 `ANALYSIS_DATABASE_ROOT`；宿主 Docker 模式中，执行
路径仍由 `ANALYSIS_DATABASE_EXECUTION_ROOT` 转换，两者应指向同一份 NAS 数据。

建议的目录结构如下，文件名可按本地生产规范调整，并在页面中配置实际路径：

```text
databases/
└── hg38/
    └── blood_tumor/
        ├── panels/
        │   ├── 84/
        │   │   ├── targets.84panel.bed
        │   │   └── genes.bed
        │   ├── 396/
        │   │   ├── targets.396.bed
        │   │   └── genes.bed
        │   └── 624/
        │       ├── targets.624.bed
        │       └── genes.bed
        ├── cnvkit/
        └── resource/
```

## 就绪规则

- `未配置路径`：资源模板要求该语义输入，但目录尚未填写路径；
- `文件缺失`：路径已填写，但数据库根目录内不存在对应文件或目录；
- `文件名不匹配`：当前正式 WDL 仍按 BED basename 选择 84/396/624 分支，文件名必须包含对应标识；
- `校验值不一致`：声明了 SHA-256 的关键文件内容与目录记录不一致；
- `资源完整`：当前 Reference 或 Panel 的全部必需项均存在；
- 页面检查存在性和类型；只对显式填写 SHA-256 的关键文件计算内容摘要，不扫描大型目录内容。

正式血液肿瘤 WDL 当前还包含 `oss://`、`/easygene_data` 历史引用，以及
`sample_info*` 等业务输入。资源补齐不会绕过这些适配器阻塞；运行页会列出具体原因，
在映射完成前禁止投递，避免任务运行到中途才失败。

## 下一阶段

1. 收集并录入 84/396/624 Panel 的正式 BED、gene BED 及版本信息；
2. 把正式 WDL 中的外部绝对路径逐项替换为受管资源绑定；
3. 明确 `sample_info*` 输入格式，生成单样本和配对样本适配器；
4. 用约 100 MB 的裁剪 FASTQ 做单样本端到端验证，再覆盖配对流程；
5. 增加资源版本升级、受影响流程预览和生产发布检查。
