# ttft_table.csv 机器可读契约（本文件为仓内权威定义）

`analyze_ttft.py --csv-out` 输出的 `ttft_table.csv` 遵循本契约。下游消费方
（TP 拆分收益对比等分析工具）以本文件为唯一口径定义处。

## 1. 文件格式

- 文件名惯例 `ttft_table.csv`，UTF-8，首行表头，每请求一行
- `ts`：first token 时刻（BREAKDOWN 行时间戳；epoch 秒输出 `YYYY-MM-DD HH:MM:SS.mmm`，
  相对时钟原样输出数值）。**相对秒形态的下游限制**：loader 只解析日期时间——
  ts 为相对秒时时间类过滤（如 `--warmup-seconds` 预热过滤）不生效、整轮保留
  预热请求；生产端检测到整轮相对秒会打 stderr 告警，交接时须向下游说明
- `request_id`：与日志行 `req=` 对齐，证据链连接键
- 所有 `*_ms` 字段单位 ms，保留 2 位小数

表头（14 列）：

```csv
ts,request_id,input_len,ttft_ms,net_lb_ms,fe_inputq_ms,fe_addreq_ms,sched_qwait_ms,sched_total_ms,fwd_pure_ms,draft_ms,kv_xfer_ms,shm_dq_ms,other_ms
```

## 2. 守恒式（与总耗时同边界）

```text
ttft_ms = net_lb + fe_inputq + fe_addreq + sched_qwait + sched_total
        + fwd_pure + draft + kv_xfer + shm_dq + other
```

**时间边界**：`ttft_ms = net_lb + 引擎侧 ttft`（即 client 发出 → 引擎首 token），
因此 Σ10 阶段 = ttft_ms 精确成立。注意 `ttft_ms` 比 BREAKDOWN 行的 `ttft=`
多一个 `net_lb`（引擎侧 ttft 从 arrival_time 起算、不含 client→proxy 网络与
分发）——对账或按 req 回溯原始日志行时，差值即 net_lb。

消费端容差建议：`|ttft_ms − Σ阶段| > max(2ms, 2%×ttft_ms)` 视为守恒违例
（数据质量信号，非错误数据即删）；`other` 为兜底项（tokenize/dispatch/
add_req/mm_hash/wait_exec/sample/return/out_delivery/residual 合计），
突然变大说明有阶段未被正确归因。

**守恒差值的唯一合法来源**：回填增量超出 step_other 信封的行（对应 v3 口径
`conserv_err > 0`，典型为冷启动/chunked-prefill 多步行）。此类行 `other` 钳 0
（不落负值），溢出量以守恒差值 `Σ阶段 − ttft_ms` 暴露——即回填阶段和超出
引擎实测 ttft 的毫秒数；解析器写 CSV 时对这类行打 stderr 告警。正常行
`Σ阶段 = ttft_ms` 精确成立、`other ≥ 0`。

## 3. 阶段映射（v3 打点列 → 10 阶段）

| 阶段 | 来源（v3 列） | 说明 |
| --- | --- | --- |
| `net_lb` | `lb_dispatch`（提供 `--lb` 时为 proxy 口径 client2proxy_ms + dispatch_ms，无钟偏；否则引擎侧 INFO 值，跨机可能有钟偏） | client→proxy→P 引擎入口 |
| `fe_inputq` | `add_req_iq` | frontend 输入队列等待 |
| `fe_addreq` | `add_req_proc` | add_request 处理 |
| `sched_qwait` | `queue_wait` | scheduler 排队 |
| `sched_total` | `sched_total` | 出队 + 组 batch + 调度 |
| `fwd_pure` | `forward` | 纯前向（整批墙钟） |
| `draft` | `mtp`（[TTFT_DRAFT] TP-max 回填） | MTP/投机采样 draft 段 |
| `kv_xfer` | `load_kv + save_kv`（pool_worker / [TTFT_KVXFER][put] TP-max 回填） | P→D KV 传输两侧 |
| `shm_dq` | `dequeue`（[SHM_DEQUEUE_BATCH] TP-max 回填，截断到 step_other 信封） | 引擎内 shm 出队 |
| `other` | `max(0, 引擎侧 ttft − Σ(引擎内 8 阶段))` | 兜底（正常恒 ≥ 0；回填溢出行钳 0 + 守恒差值暴露，见 §2） |

## 4. 缺列与多列

- loader 应容忍表头多列/缺列；缺的阶段按 0 处理并在报告中标注缺失
- `ttft_ms`、`sched_qwait_ms` 不可缺
- `input_len` 需打点行携带 `input_len=`，缺失输出**空单元格**（与 ts 缺失同约定，
  0 仅在打点确实为 0 时出现）——消费端不得把空值当有效长度（两轮都缺数据时
  不应据此报告输入分布一致），按缺失标注

## 5. 已知口径坑（继承自打点设计，非 bug）

- `defer_finalize=True` 下 engine 侧 `load_kv`/`save_kv` 为哨兵 0，真实值由
  pool_worker / kv_transfer 日志 TP-max 回填（本表 `kv_xfer` 已是回填后口径）
- SHM 打点行无 `req=` 字段（按 batch 归因），`shm_dq` 经批级 TP-max + 信封
  截断回填；未开 `VLLM_TTFT_SHM_LOG=1` 时为 0
- 旧日志（v1.5）口径与 v3 不一致，勿与本表混比
