# 分析数据库目录

运行页只读取本目录，不会把数据库复制进 Git。`catalog.json` 定义可选参考版本、Panel
以及运行前必须存在的文件；页面会把缺失路径逐项列出来。

```text
workspace/databases/
├── catalog.json
├── hg19/
│   ├── reference/   # hg19.simp.fa、BWA 索引及可选 fa.fai/dict
│   ├── humandb/     # Annovar hg19 数据库
│   ├── local/       # 本地变异频率与本地 CNV 频率库
│   ├── resource/    # 实体瘤筛选、热点、融合、CNV 和基因映射规则
│   ├── database/    # genome windows、genomic.gene.gff、ref_annot.gtf
│   └── cnvdb/       # DGV、DECIPHER、ClinVar、ClinGen、RefSeq CNV 注释
└── task_resource/
    ├── zhenyuan_tumor_120_V4.bed
    ├── zhenyuan_1100.gene.bed
    ├── zy.colon.18.gene.list
    ├── zy.180.gene.list
    ├── tert.bed
    ├── 1p19q.bed
    ├── druggable.hg19.csv
    ├── hg19_baseline_120_zm_kz/
    └── hg19_baseline_1100_zm_kz/
```

## Annovar 数据

`hg19/humandb` 还需要覆盖当前 WDL 使用的全部 protocol：

- `refGeneWithVer`
- `cytoBand`
- `clinvar_20220320`
- `1000g2015aug_all`
- `avsnp150`
- `cosmic96`
- `dbnsfp42a`
- `exac03`
- `gnomad211_genome`

这些文件通常由 Annovar 下载器生成，catalog 对常见的 `.txt` / `.txt.gz` 命名均做了兼容；
仍建议整体复制原运行环境中已验证的 `humandb`，不要只放单个注释文件。

## 添加参考版本或 Panel

先按独立目录放置资源，再给 `catalog.json` 增加一项。所有 path 必须是相对本目录的
路径，不能使用 `..`、绝对路径或指向目录外的符号链接。参考库与 Panel 均显示“就绪”
后，运行页才允许提交任务。
