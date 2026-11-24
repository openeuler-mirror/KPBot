# TTFT 分阶段术语表与日志格式

本 skill 的表格与日志口径来自 TTFT breakdown 打点 patch（v3.2）与配套解析工具（两者均为环境级独立交付，不在本仓库，调用方式见其自身文档）。本文档是唯一的口径定义处：脚本与报告中的阶段名、字段名、日志行格式都以此为准。

## 1. 守恒式（数据质量的锚）

每条请求的 TTFT 满足主守恒式（打点 stats.py 内置同一守恒式）：

```text
ttft = net_lb + fe_inputq + fe_addreq + sched_qwait + sched_total
     + fwd_pure + draft + kv_xfer + shm_dq + other
```

单位一律 ms。`other` 是兜底项（detokenize、事件循环等未归类时间）。任何一行 `|ttft − Σ阶段| > max(2ms, 2%×ttft)` 视为守恒违例——不是错误数据就删，而是**数据质量信号**：违例占比 >5% 时整份数据的结论要打折扣，报告必须在「数据与运行态」一节明示。一行都检不了（全量行缺阶段列或 ttft 为 0）时守恒校验标**不可判定**，不当作 0% 违例假装通过。

## 2. 阶段定义与 TP 敏感性

「TP 敏感性」列回答：TP 切小（TP32→TP8，DP 变多）后这个阶段**预期**怎么变。写报告解读、判别收益点真伪时用这张表——如果一个「收益点」的成因与这里不符，要在报告里说明为什么。

| 阶段 | 含义 | 打点来源 | TP 敏感性（预期方向） |
| --- | --- | --- | --- |
| `net_lb` | client→proxy→P 引擎入口，含 LB_DISPATCH 分发耗时 | `[LB_DISPATCH]` + client_send_ts 透传 | 基本不变；DP 端口变多后 dispatch 查找略增（毫秒级） |
| `fe_inputq` | P 侧 frontend 输入队列等待 | input_processor 打点 | 随并发请求突发变化；DP 分流后应下降 |
| `fe_addreq` | add_request 处理（构造 EngineCoreRequest 等） | inputq_wait/add_req_proc 累计 | 不敏感 |
| `sched_qwait` | scheduler waiting queue 排队（**没轮到算**的时间） | request.py ttft_* 累计字段 | **TP 拆分收益的主战场**：P 并发实例数（DP）变多，burst 被分流，排队坍缩；TP32 跨机单实例时最肥 |
| `sched_total` | 出队 + 组 batch + 调度本身 | schedule_total_time | 不敏感；实例变多后单实例调度批更小，可能微增 |
| `fwd_pure` | 纯前向（不含钩子开销） | worker_fwd_pure_time（model_runner_v1 override） | TP 大→allreduce 轮次多、跨机通信贵；TP 小→每卡份额大但通信便宜。Prefill 计算密集时 TP8 通常略优于 TP32 |
| `draft` | MTP/投机采样 draft 段 | `[TTFT_DRAFT]` + `_ttft_in_draft` | 不敏感（draft 与 TP 布局基本无关） |
| `kv_xfer` | P→D KV 传输（put/load 两侧） | `[TTFT_KVXFER][put]`、`start_load_kv cost time` | DP 变多后路由/并发路径变化，可能微增；量级一般 <10ms |
| `shm_dq` | 引擎内 shm 队列出队 | `[SHM_DEQUEUE]` / `[SHM_DEQUEUE_BATCH]` | 调度频率变化会轻微影响；毫秒级 |
| `other` | 兜底（detokenize、first-token 事件等） | 守恒式残差 | 不敏感；突然变大 = 有阶段没被正确归因，查守恒违例 |

两个已知口径坑（继承自打点设计，不是 bug）：

- `defer_finalize=True` 下 producer 侧 `save_kv` 的 sentinel 恒 0、engine 侧 `load_kv` 恒 0——真实值在 pool_worker / kv_transfer 日志里，`kv_xfer` 以日志回填值为准。某侧整列恒 0 而另一侧非 0 的阶段，分析脚本会自动标「疑似未回填/口径不一致」并告警，该阶段 Δ% 记不可计算、判定仅用绝对阈值。
- Ray 聚合日志（log_to_driver）里 worker 行会丢原始文件号，SHM_DEQUEUE 行没有 req 字段——它按 batch 打点，进不了证据链（脚本不读入内存）；证据链引用时只能给时间戳，不给 req_id。

## 3. 表格格式（TTFT breakdown 解析工具输出，CSV）

文件名惯例 `ttft_table.csv`，UTF-8，首行表头，每请求一行：

```csv
ts,request_id,input_len,ttft_ms,net_lb_ms,fe_inputq_ms,fe_addreq_ms,sched_qwait_ms,sched_total_ms,fwd_pure_ms,draft_ms,kv_xfer_ms,shm_dq_ms,other_ms
2026-11-23 10:15:32,req-000042,2048,412.35,3.21,1.02,0.55,380.12,12.30,9.85,2.11,1.05,1.14,1.00
```

- `ts` 是 first token 时刻（BREAKDOWN 行打印时刻）；`request_id` 与日志行里的 `req=` 对齐，是证据链的连接键。
- 所有 `*_ms` 字段单位 ms，保留 2 位小数。
- loader 容忍表头多列/缺列（缺的阶段按缺失处理并在报告中标注缺失列，缺列行不计入守恒校验），但 `ttft_ms`、`sched_qwait_ms` 不可缺；`input_len` 缺失保留为空（不算 0），任一侧无有效 `input_len` 时长度分布检查标不可判定。
- `config.json` 可带 `data_source` 字段声明数据来源（`synthetic`/`measured`），报告会透传该标记。

## 4. 日志行格式（打点 patch v3.2）

每份数据目录下常见三类文件：`p_engine.log`（引擎侧）、`proxy.log`（宿主机 proxy）、`bench.log`（压测端，可选）。

```text
# 每请求一行，守恒式全字段（证据链主引用行）
2026-11-23 10:15:32,114 INFO  [TTFT][BREAKDOWN] v3 req=req-000042 input_len=2048 ttft=412.35 net_lb=3.21 fe_inputq=1.02 fe_addreq=0.55 sched_qwait=380.12 sched_total=12.30 fwd_pure=9.85 draft=2.11 kv_xfer=1.05 shm_dq=1.14 other=1.00

# proxy 分发（net_lb 证据行）
2026-11-23 10:15:31,900 INFO  [LB_DISPATCH] req=req-000042 dst=dp:2/port:9902 cost_ms=3.21

# shm 出队（batch 级，无 req 字段）
2026-11-23 10:15:32,930 INFO  [SHM_DEQUEUE_BATCH] dq=6 objs=3 cost_ms=1.14

# KV 传输两侧（kv_xfer 证据行）
2026-11-23 10:15:33,870 INFO  [TTFT_KVXFER][put] req=req-000042 pool=p:2 blocks=64 cost_ms=1.05
2026-11-23 10:15:33,905 INFO  start_load_kv cost time: 0.98ms req=req-000042

# draft 段
2026-11-23 10:15:34,180 INFO  [TTFT_DRAFT] req=req-000042 draft_tokens=3 accepted=2 cost_ms=2.11
```

引用格式统一写 `文件名:行号`（例：`p_engine.log:4096`），报告里附原始行内容，不凭记忆转述。

## 5. 证据链三级结构

每个收益点/风险点必须成链，缺一级要在报告里明说缺哪一级、为什么：

```text
L1 Claim     —— 一句话结论，带幅度与方向："sched_qwait P90 从 812ms 降至 231ms（−71.5%）"
L2 支撑数据   —— 表格侧证据：before/after 该阶段的 mean/P50/P90/P99 对比行（数字可由 CSV 复算）
L3 日志摘录   —— 原始日志侧证据：代表请求（该阶段 best/median/worst 各 1 条）的 BREAKDOWN 行
                + 该阶段专属打点行（如 kv_xfer 点配 KVXFER put 行），带 文件名:行号 与 req_id
```

为什么三级：L1 是给决策者看的，L2 保证结论可复算，L3 保证表格数字能对回原始现场（表是解析出来的，解析可能有 bug；日志才是 ground truth）。只有 L1+L2 没有 L3 时，结论标「未回溯到原始日志」。
