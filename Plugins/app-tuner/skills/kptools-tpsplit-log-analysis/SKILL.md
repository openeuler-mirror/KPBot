---
name: kptools-tpsplit-log-analysis
description: >
  解析本地 TTFT 打点日志（一次压测一套 4 份）生成 TTFT breakdown Excel（毫秒）+
  文字结论，并按 ttft_table.csv 契约导出供下游收益对比消费。当用户提到以下场景时
  必须使用此技能：解析 TTFT、TTFT breakdown 出表、analyze_ttft.py、对账
  client_ttft、回传 return、TP-max 回填、多 P 合并 --merge-p、burst 分段
  --burst-gap、看 TTFT 各环节占比、TTFT 长尾 P90/P99、定位首 token 延迟在
  tokenize/queue/forward/KV 哪一环。即使用户只说"帮我把这轮压测的 TTFT 日志解析
  一下"或"看看 TTFT 拆解在哪一环"，也应使用此技能。只做本地解析出表，不连远端：
  日志还在远端时先转 kpbot-tpsplit-log-collector 采集回传（配套 PR #398 交付），
  打点 patch 部署归 kptools-tpsplit-log-trace（配套 PR #340 交付）。
license: Proprietary
compatibility: >
  本地 Windows/Linux（Python 3 + openpyxl，xlsx 输出必需）；不连远端、只读输入
  目录。输入为已回传本地的一套 4 份日志（P/D/proxy 日志 + 压测 result json，须
  同一次压测、压测需 --save-detailed；采集回传由 kpbot-tpsplit-log-collector
  完成或用户自行回传）；打点须已部署且输出 v3 格式（[TTFT][BREAKDOWN] /
  [TTFT_KVXFER] / [SHM_DEQUEUE] / [LB_DISPATCH] / [TTFT_DRAFT]）；ttft_table.csv
  契约见本 skill references/ttft_table_contract.md，下游收益对比由仓内
  kptools-tpsplit-report（#329 已合入）消费。
metadata:
  skill_type: tool_assisted
  category: diagnostics
  domain: ttft_log_analysis
  activation_mode: active
  version: "0.2.0"
---

# kptools-tpsplit-log-analysis：TTFT 日志解析出表

打点已部署、日志已回传本地时（采集与打点部署不在本 skill 范围内），本 skill 只做：
**本地解析 4 份日志 → Excel（8 Sheet）+ 文字结论 + ttft_table.csv**（全部毫秒）。

**采集与解析分离**：远端日志的定位/校验/暂存/回传归 kpbot-tpsplit-log-collector
（`fetch_logs.sh` 已随拆分迁出，配套 PR 交付）；本 skill 不 SSH、不碰远端，解析出表
由自带的 `scripts/analyze_ttft.py` 完成——不要手算环节占比或手写表格，脚本口径可
复现（守恒式/信封/回填全部有断言护住）。

**配套拆分说明**：本 skill 原「采集 + 解析」一体，现拆分为纯解析 skill；采集侧
（`fetch_logs.sh` 与采集流程、压测补数据步骤）迁至新 skill kpbot-tpsplit-log-collector
（配套 PR，建议同批合入——单独部署本版本期间不具备远端日志采集能力，用户自行
回传日志亦可）。拆分前后过渡期的行为由两侧 SKILL.md 的交接路径定义：本 skill 收到
「日志还在远端」的请求时指引采集侧，不代做采集；两 PR 改动不相交，任意先后合入
无冲突。采集产物契约（一套 4 份的命名/同轮对齐/标记计数口径）以 collector 侧的
`references/log-set-contract.md` 为权威；本文件的日志格式节与
`references/ttft_table_contract.md` 仍是解析列语义/守恒式/回填口径的仓内权威。

## 0. 脚本清单（全部已打包在 skill 内）

| 脚本/文件 | 用途 |
| --- | --- |
| `scripts/analyze_ttft.py` | 核心解析器：4 类日志 → Excel 8 Sheet + 终端结论 + CSV |
| `scripts/gen_synth_logs.py` | 合成 v3 打点日志（演示/构造测试数据） |
| `scripts/test_regression.py` | 回归自检：合成日志全断言（回填/信封/守恒/LB/CSV 契约），改动解析器后必跑 |
| `references/ttft_table_contract.md` | ttft_table.csv 机器可读契约（仓内权威定义） |

依赖：Python 3 + openpyxl（`python -m pip install openpyxl`）。

## 1. 日志格式（解析依据）

P 节点（`vllm/v1/` 打点）：

```plaintext
[TTFT][BREAKDOWN] req= ttft= tokenize_render= dispatch= add_req= mm_hash=
 queue_wait= wait_exec= load_kv= forward= sample= save_kv= step_other=
 out_delivery= residual= lb_dispatch= hash= ext_lookup= alloc= overhead=
 sched_total= update= emit= step_exec= preempt= fwd_tokens= fwd_steps=
 add_req_iq= add_req_proc=
```

D 节点：`[TTFT_DECODE][EngineCore] decode_forward= load= save= sample= num_total= num_gen= sched_total= decode_only=`
P KV 落盘线程：`[TTFT_KVXFER][put] req= total build_key lookup prepare sync put [finalize]`
P pool_worker（逐 TP）：`start_load_kv ... cost time`（带 `batch=` / `reqs=`）
shm 广播（`VLLM_TTFT_SHM_LOG=1`）：`[SHM_DEQUEUE] t= wait= read= mono= tid= total= count= avg=` / `[SHM_DEQUEUE_BATCH] batch=<sha16>-<step> tp=r/N wait= read= nreq= reqs= sub=main|draft|take_draft|aux`
MTP draft（逐 TP）：`[TTFT_DRAFT] batch=<sha16> draft_wall= reqs=`
proxy：`[LB_DISPATCH] req= wall_recv= client_send= client2proxy_ms= dispatch_ms= wait_p_ms= first_token_ms= prefiller= decoder=`
压测端 `--save-detailed`：每请求 client_send_ts / 首 token 墙钟（jsonl）。

**解析全部按键**（`(?:^|\s)key=value`），与字段顺序无关；旧日志缺列按 0。

## 2. 输入约定

- **一套 4 份**（同一次压测，落同一本地目录）：
  | 输入 | 文件 | 说明 |
  | --- | --- | --- |
  | P 日志 | `prefill.log`（breakdown + KV 传输 + shm/draft），多 P 每台一份 | 多 P 建议命名 `P<ip>_prefill.log` 区分 |
  | D 日志 | `decode.log` | |
  | proxy 日志 | `load_balance.log` | |
  | 压测结果 | `manual_*_dumpjsonl.json`（必须 `--save-detailed`） | 无 client_send_ts 无法对账 |
- 四份不是同一次压测（请求数对不上）时对账无意义——退回采集侧确认，不要硬解析
- **日志还在远端** → 本 skill 不做 SSH：转 kpbot-tpsplit-log-collector 采集回传
  （或用户自行回传）后再来；**打点还没部署/没输出** → 转 kptools-tpsplit-log-trace
- 输出 `-o TTFT_xxx_ms.xlsx` 文件名含机器/轮次，可追溯

## 3. 解析出表

```bash
python "<skill目录>/scripts/analyze_ttft.py" "P:prefill.log" -o TTFT.xlsx --one-burst \
    --decode "D:decode.log" --kvxfer "P:prefill.log" \
    --lb "PROXY:load_balance.log" \
    --detailed "BENCH:manual_xxx_dumpjsonl.json"
```

- 输入形如 `LABEL:path`：P 节点 LABEL 用机器 IP（如 102/103），D/PROXY/BENCH 用角色名
- `--one-burst`：整份日志当一个 burst；或 `--burst-gap N`（秒）按请求间隔分段
- `--decode` / `--kvxfer`：D 每步 decode、P KV 跨节点传输（`--kvxfer` 可多次；
  缺省 = P 日志本身）
- `--lb`：proxy `[LB_DISPATCH]`（client2proxy / dispatch_ms / wait_p_ms / first_token），
  同时用 proxy 单机口径回填 lb_dispatch（无钟偏，不需 NTP）
- `--detailed`：result json，与 `--lb` 按 `client_send_ts` 逐请求对账算回传
- `--merge-p`：多 P 日志合并成一套完整请求，明细新增 `IP` 列；不加则按机器分开
  （机器对比 delta Sheet 做机器对比）

多 P 示例：

```bash
python analyze_ttft.py "102:P102_prefill.log" "103:P103_prefill.log" -o TTFT_full.xlsx \
    --merge-p --one-burst --decode "D:D103_decode.log" \
    --kvxfer "102:P102_prefill.log" --kvxfer "103:P103_prefill.log" \
    --lb "PROXY:load_balance.log" --detailed "BENCH:manual_xxx_dumpjsonl.json"
```

**输出 Excel 8 个 Sheet**：总览 / Burst各环节 / 逐请求明细 / 机器对比(delta) /
D节点Decode / P节点KV传输 / LB分发 / 回传对账。终端另出文字结论（Burst、环节、
decode、LB、KV、对账、长尾 P90/P99、预热/冷启动警示）。

**CSV 导出（下游对接）**：加 `--csv-out ttft_table.csv` 额外输出一份 10 阶段表，
供下游 TP 拆分收益对比分析消费（仓内消费方：kptools-tpsplit-report，#329 已合入，
其表头 10 阶段列与本契约逐一匹配）。首行表头
`ts,request_id,input_len,ttft_ms,net_lb_ms,fe_inputq_ms,fe_addreq_ms,sched_qwait_ms,
sched_total_ms,fwd_pure_ms,draft_ms,kv_xfer_ms,shm_dq_ms,other_ms`，每请求一行。
**完整机器可读契约（表头/守恒式/阶段映射/容差/缺列约定）见
`references/ttft_table_contract.md`——该文件是仓内权威定义处**。
时间边界：`ttft_ms = net_lb + 引擎侧 ttft`，与 Σ10 阶段同边界（比 BREAKDOWN
行的 `ttft=` 多一个 net_lb，回溯日志行时注意差值）；正常行精确守恒且 `other`
（兜底项）≥ 0；回填增量超出 step_other 信封的行（对应 v3 conserv_err>0）
other 钳 0、溢出量以守恒差值暴露给下游质量检查并打 stderr 告警；
input_len 需打点行携带，缺省输出 0。

## 4. 列口径（v3，当前生效）

主守恒式（fwd_steps==1 精确）：`ttft = tokenize_render + dispatch + add_req + mm_hash
+ queue_wait + wait_exec + load_kv + forward + mtp + sample + save_kv + step_other +
out_delivery + residual`

- 逐请求明细 = 7 标识列（机器/IP/Burst/req/batch/ttft/cold_step）+ **14 主列** + INFO
  列（lb_dispatch/hash/ext_lookup/alloc/overhead/sched_total/update/emit/step_exec/
  dequeue(+main/draft/take_draft/aux)/return/add_req_iq/add_req_proc）+ `conserv_err/
  preempt/fwd_tokens/fwd_steps/fwd_ms_per_kT/lb_src`
- 恒等式：`Σ主列 - ttft = conserv_err >= 0`；`step_other = dequeue + return`；
  `dequeue = main + draft + take_draft + aux`；`add_req ≈ add_req_iq + add_req_proc`
- `ttft`（引擎侧）= 首 token 时刻 − arrival_time，**含 tokenize_render**，不含
  client→APIServer 网络
- `forward/load_kv/save_kv` = **整批墙钟**，批内每请求各计整批、不除批数、不乘 token share
- `lb_dispatch` 为 INFO：提供 `--lb` 时用 proxy 单机口径回填 `client2proxy_ms +
  dispatch_ms`，明细末列 `lb_src=proxy`；不再扣 residual
- `residual = max(ttft − Σ主列, 0)`，全部回填后由 `_finalize_step_other()` 统一重算

**分析层回填（与日志同构，取 TP-max）**：

1. `_apply_real_loadkv`：按 batch（`start_load_kv` 行的 batch/reqs）取全 TP 最大值
   覆盖 `load_kv`（MLA 下 KV 跨 TP 复制，各 rank 加载完整批）
2. `_apply_draft_mtp`：`[TTFT_DRAFT]` 批内 TP-max → `mtp`
3. `_apply_real_savekv`：`[TTFT_KVXFER][put] total` 同 req 全 TP max → `save_kv`
4. `_apply_real_dequeue`：`[SHM_DEQUEUE_BATCH]` 批内全 TP wait max，跨步累加
   （fwd_steps 步内）；截断到 step_other，子拆分按比例缩放
5. `_finalize_step_other`：信封重推导 step_other（回填增量被 step_other 等量吸收，
   主列加总不变；超出部分落 conserv_err）→ 拆 dequeue/return → 重算 residual

解析器改动后必跑自检（合成 v3 日志全断言：回填/信封/守恒/LB/对账/burst/冷启动，
退出码 0 = 通过）：

```bash
python "<skill目录>/scripts/test_regression.py"
```

## 5. 解读与已知坑

- **对账**：`client_ttft ≈ client2proxy(网络) + first_token(分发+P处理+P→D交接+D首
  token) + return(proxy→client)`；同机回环 return≈0，偏大=钟偏或真实网络。
- **D 侧 `num_total` 常为 0**（纯 decode）：`decode_forward` 由 EngineCore 墙钟兜底，正常。
- **`sample` 在 PP+last-rank 常 0**，prefill worker 预期为 0，不是 bug。
- **某列整列为 0（如 dequeue/mtp）先怀疑数据源缺失**：去原始日志 grep 对应标记
  （`[SHM_DEQUEUE_BATCH]` / `[TTFT_DRAFT]`）——标记不存在 = 采集侧缺数据源
  （VLLM_TTFT_SHM_LOG 未开等），不是解析 bug；确认缺源后回
  kpbot-tpsplit-log-collector 的标记检出表对影响，决定是否重采。
- **`dequeue` 空闲期伪样本**：SHM 原始 wait 可能含引擎空闲阻塞（数十秒），解析层已
  截断到 step_other 信封；统计比较用 p50/p90。
- **首步冷启动**：`cold_step=1`（add_req<1ms 且 forward>2×中位数）应剔除或单列，否则
  forward 被首次 kernel/图开销拉高。
- **预热判据**：每请求 `fwd_tokens=12`（8k5+前缀池命中），明显 >12 说明池未预热，该轮不可比。
- **`fwd_steps>1`**（chunked prefill）：多步累计墙钟超出首 token 步信封的部分落在
  `conserv_err`。
- **跨机钟偏**：引擎侧 `lb_dispatch` 与 P→D 相减不可信，用 `--lb` 回填；多机压测建议
  NTP 对齐。
- 旧日志（v1.5）主列含 lb_dispatch/hash/ext/alloc/overhead/update，且 mtp 恒 0；已知
  artifact：少数行 conserv_err 可达数十 ms，属口径差异，勿与 v3 混比。
- `defer_finalize=True` 下 engine 侧 load_kv/save_kv 哨兵 0 属已知设计，真实值由
  pool_worker / kv_transfer 日志回填（见回填层）。

## 文件

- `scripts/analyze_ttft.py`：核心解析器（4 类日志 → Excel 8 Sheet + 终端结论 + CSV）
- `scripts/gen_synth_logs.py`：合成 v3 打点日志（演示/构造测试数据）
- `scripts/test_regression.py`：回归自检（改动解析器后必跑）
- `references/ttft_table_contract.md`：ttft_table.csv 机器可读契约（仓内权威定义）
- `evals/`：触发评测集（正/负例路由矩阵；采集类触发例为负例并路由到
  kpbot-tpsplit-log-collector）+ fixtures
