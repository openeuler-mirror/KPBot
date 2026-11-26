# 一套 4 份：TTFT 打点日志采集产物契约（本文件为采集侧权威）

kpbot-tpsplit-log-collector 的交付物与校验口径。下游 kptools-tpsplit-log-analysis
按本契约消费；**解析列语义/守恒式/回填口径的权威在该 skill**（其 SKILL.md 日志格式
节 + `references/ttft_table_contract.md`），两侧互不重复定义——本文件只管「采到
什么、怎么校验、怎么命名」。

## 1. 交付物定义

一次压测一套 4 份，落本地同一目录：

| 份 | 文件 | 定位/校验标记 | 说明 |
| --- | --- | --- | --- |
| P 日志 | `prefill.log` | `[TTFT][BREAKDOWN]` | 多 P 每台一份；另含 KVXFER / pool_worker / shm / draft 标记 |
| D 日志 | `decode.log` | `[TTFT_DECODE]` | |
| proxy 日志 | `load_balance.log` | `[LB_DISPATCH]` | |
| 压测结果 | `manual_*_dumpjsonl.json` | `client_send_ts` | 压测必须带 `--save-detailed` |

- **多 P 命名**：下载后 `P<ip>_prefill.log`（或 `P102_prefill.log` 机器号风格），
  与解析侧 `LABEL:path` 输入对齐；不区分会在合并解析（`--merge-p`）时拿错标签
- 四份必须**同一次压测**（请求数对齐），否则下游对账无意义

## 2. 校验口径（fetch_logs.sh 实现）

- **标记计数**：`grep -oF` 逐次计数（`-c` 只数行、单行多记录会漏计）；同一文件
  （realpath 相同）跨多 root 只统计/暂存一次
- **扫描根/清单**：`--root` 必填，缺参退出码 2（`/data` 是共享挂载，默认递归整个根
  会把其他用户目录下可读的同名日志也扫进来，须显式指定获准采集的目录）；位置参数
  亦可传文件清单，按文件名分派类型（`prefill.log` / `decode.log` /
  `load_balance.log` / `manual_*_dumpjsonl.json`，无法识别的文件报错并计为校验
  失败），计数/暂存只算清单内文件——多轮压测残留时先选定本轮目录或文件清单再采集
- **压测请求数**：按解析出的 JSON 记录条数计（`[` 开头整文件按 JSON 数组解析、
  否则 jsonl 逐行，每条 dict 记录计 1——与解析侧 `analyze_ttft.py` load_bench 同
  口径，不数字符串出现次数）；**记录总数与含 `client_send_ts` 的有效数分开报告**，
  缺字段记录逐文件告警（避免"总数恰好相等但全部缺字段、对不上账"的盲区）；
  宿主机无 python3 时退化为 `"client_send_ts":` 键字面量计数并打告警
- **同轮对齐**：P 侧 BREAKDOWN 总行数（多 P 求和）≈ 压测记录数；两侧文件均找到
  即比较（压测文件为空数组/零条记录同样触发偏差告警），与记录总数或有效时间戳数
  任一不等即**软告警**（不改变退出码），含义 = 四份可能不是同一轮压测、日志截断
  或压测缺 `--save-detailed`
- **完整性门槛**：`--expect-req N` 按 BREAKDOWN 多 P **求和**校验（每份只占总数
  一部分，逐份卡门槛会把合法多 P 采集误判为失败），低于门槛退出码 1；参数非
  非负整数退出码 2
- **暂存**：`--stage DIR` 目标名 = 来源完整路径（`/` 与 `:` 替换为 `__`），多 P
  同名 prefill.log 不互相覆盖；目标以排他占位（noclobber，O_EXCL 语义）创建，
  并发采集进程冲突、目标已存在或复制失败 → 非零退出，不静默覆盖另一进程的结果
- **缺失判定**：P 日志或压测 json 缺失 → 退出码 1（关键输入）；D/proxy 缺失仅
  打告警、不判失败

## 3. 标记存在性与下游影响

| 标记缺失（计数为 0，输出无该标记行） | 下游列 | 采集侧动作 |
| --- | --- | --- |
| `[SHM_DEQUEUE_BATCH]` | dequeue 及其子列全 0 | 检出提示：VLLM_TTFT_SHM_LOG=1 未设置/未生效（Ray actor 的 env 来自 raylet，启动 shell export 不生效）；是否重采由用户决定 |
| `[TTFT_DRAFT]` | mtp/draft 列为 0 | 同上（打点未开或该部署无 MTP） |

下游某列整列为 0 时，先回本表对标记是否存在，再怀疑解析器。

## 4. 打点前提

日志须为 v3 打点格式（`[TTFT][BREAKDOWN]` 等行格式定义见解析侧
kptools-tpsplit-log-analysis 的 SKILL.md 日志格式节）。打点 patch 的部署与生效
排查归 kptools-tpsplit-log-trace（配套 PR 交付）。
