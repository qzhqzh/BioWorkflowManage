# miniwdl 执行案例

这里的资产分为三层：

1. `smoke/workflow.wdl` 不需要业务数据，用来证明
   miniwdl → Docker → task → 持久化输出的真实链路；
2. `cases/fastp` 直接运行编译器生成的 fastp WDL；
3. `cases/fastp-bwa/run-ready.wdl` 是执行环境验收版，会现场生成 BWA
   索引，并把 SAM 转 BAM 拆到独立 samtools task。

`phase1-fastp-bwa/expected/workflow.wdl` 已通过 miniwdl 静态检查，但目前不具备
真实运行条件：它没有建模 BWA 索引文件，同时 BWA 镜像内没有命令所需的
samtools。执行环境验收版不会覆盖或伪装编译产物；案例清单会同时展示这两个
已知阻塞，后续应在 ToolSpec/编译器层正式修复。

测试数据不进入仓库。运行 `./scripts/miniwdl.sh prepare` 后，按生成的
`expected-inputs.json` 把 FASTQ/FASTA 放到 `data/miniwdl/work/cases/`。
