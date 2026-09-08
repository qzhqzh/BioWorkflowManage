# 人工复制 FASTQ 的完成标记（可选集成协议 v1）

每次复制使用独立批次目录；FASTQ 与大小写固定的 `_READY.done` 位于同一目录。
标记可以是空文件，必须是可读普通文件，不允许符号链接。完成全部复制并确认成功后，
由操作人员或复制脚本创建标记；BWM 不自动生成标记，也不在分析结束后删除标记。

```sh
rsync -a --info=progress2 --exclude='_READY.done' /source/run-001/ /data/rawdata/run-001/ \
  && touch /data/rawdata/run-001/_READY.done
```

`/data/rawdata` 为示例宿主机目录，应替换为现场挂载的 rawdata 根目录。
子目录是不同批次，不跨目录配对。一个批次可以包含多个样本和多个 lane。
标记仅声明复制完成，不表示 FASTQ 内容验证成功、分析完成或报告签发。

## 判定和更新

- 无标记：`waiting`；有标记但尚未被完整后台索引确认：`unknown`。
- 标记、非空可读 FASTQ、完整且不重复的 R1/R2 配对通过检查：`ready`。
- 标记无效、空文件、缺失配对等：`issue`；索引过期或扫描失败：`unknown`。
- 已确认批次在同一标记代次下变化：`changed`，状态锁存，不因下一次扫描自动恢复。
- 未关联文件：`unbound`。

核验包含 inode、device、size、mtime、ctime 和批次 FASTQ 文件名清单，因此能发现
保留 mtime 的 rsync 覆盖、增删、替换和普通原地写入。ctime 晚于标记也不能建立基线。
这是本地文件系统身份核验，不是密码学完整性证明；执行链路继续使用既有资源快照验证。
不承诺防御可篡改文件系统元数据的特权操作者，也不能使人工复制目录成为不可变存储。

补充数据优先新建批次。若尚未投递且误提前创建标记，应先移除标记，完成复制并确认后
重新创建，等待索引确认。已经投递的任务固定旧快照，不能靠更新标记继续运行旧任务。
已接受任务执行前仍核验原快照；不因排队期间索引 TTL 到期而失败。
已分析记录保留在任务历史中，不写第二个“分析完成”文件。本轮不提供自动分析或缓存复用。

## 集成接口

原 `GET /api/v1/integration/rawdata-datasets` 保持旧配对就绪过滤和响应字段不变。
`?include_unready=true` 返回全部 active 数据集，增加 `status`、`issues`、`readiness`，
以及顶层 `index`。旧 `status` 是配对索引状态，投递必须以 `readiness.submission_allowed` 为准。

`POST /api/v1/integration/rawdata-datasets/readiness` 使用 `analysis:read` scope：

```json
{"files":["run-001/S001_R1.fastq.gz","run-001/S001_R2.fastq.gz"]}
```

返回 `status`、`submission_allowed`、`reasons:[{code,message}]`、`snapshot`。
支持批量 `{"datasets":[{"key":"sample-key","files":["..."]}]}`，返回
`{"results":[{"key":"sample-key","status":"...","submission_allowed":false,"reasons":[],"snapshot":null}]}`。
每次至多 100 个不重复 key，每样本至多 64 条不重复规范相对路径。

新集成必须把成功返回的 snapshot 原样放到预检和投递请求的顶层 `rawdata_readiness`。
BWM 检查其 files 与实际 rawdata 输入清单完全一致，接受时固定快照，执行前再次验证。
快照的 `root_key` 固定为 API 接受端的索引键；worker 使用该键关联索引，但文件仍在
worker 自身的受管根目录内核验，因此 API 与 worker 的挂载绝对路径可以不同。
不带此字段的已有集成和历史任务沿用原规则；该可选协议不是全局强制切换。
就绪检查通过并不自动发起任务，也不意味着当前有空闲算力。

## 部署与兼容

新增自动生成的 `0034_rawdata_batch_readiness` migration，仅添加批次基线表，不修改已有任务。
需先备份数据库，经部署负责人批准后迁移，再更新 API、索引器和 worker，最后启用调用方。
旧索引器不会建立基线，故只升级 API 会一直显示等待索引确认。
历史目录没有标记不会被自动补标记；新协议启用后需人工确认已完成复制再补标记。
回退应用版本时保留新增表即可，旧版本不读取该表；不要为回退删除表或已有数据。
