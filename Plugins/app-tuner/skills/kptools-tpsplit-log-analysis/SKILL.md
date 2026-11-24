---
name: kptools-tpsplit-log-analysis
description: >
  采集远端 P/D/proxy/压测端 TTFT 打点日志并解析生成 TTFT breakdown Excel（毫秒）+
  文字结论。当用户提到以下场景时必须使用此技能：采集日志、下载 prefill/decode/load_balance
  日志、解析 TTFT、生成 TTFT Excel、TTFT breakdown 出表、analyze_ttft.py、对账
  client_ttft、回传 return、TP-max 回填、多 P 合并 --merge-p、burst 分段 --burst-gap、
  看 TTFT 各环节占比、TTFT 长尾 P90/P99、定位首 token 延迟在 tokenize/queue/forward/KV
  哪一环。即使用户只说"帮我把这轮压测的 TTFT 日志解析一下"或"看看 TTFT 拆解在哪一环"，
  也应使用此技能。只做采集与解析出表，打点 patch 部署见 kptools-tpsplit-log-trace。
license: Proprietary
compatibility: >
  本地 Windows/Linux（Python 3 + openpyxl，xlsx 输出必需；fetch_logs.sh 需远端 bash）；
  SSH 访问 P/D/proxy 宿主机（优先 ssh-mcp-server 的 upload/execute-command/download，
  无 MCP 时回退本地 ssh/scp）；打点须已由 kptools-tpsplit-log-trace 部署（v3.2，
  [TTFT][BREAKDOWN] / [TTFT_KVXFER] / [SHM_DEQUEUE] / [LB_DISPATCH] / [TTFT_DRAFT]）；
  下游 kptools-tpsplit-report 消费 --csv-out 输出的 ttft_table.csv。
metadata:
  skill_type: tool_assisted
  category: diagnostics
  domain: ttft_log_analysis
  activation_mode: active
  version: "0.1.0"
---

# kptools-tpsplit-log-analysis：TTFT 日志采集 + 解析出表

打点已部署时（部署见 kptools-tpsplit-log-trace skill），本 skill 只做：
**采集 4 份日志 → 本地解析 → Excel（8 Sheet）+ 文字结论**（全部毫秒）。

**采集与解析分离**：日志在远端，产物统一为 4 份本地文件（P/D/proxy 日志 + 压测
result json），解析出表由 skill 自带的 `scripts/analyze_ttft.py` 完成——不要手算
环节占比或手写表格，脚本口径可复现（守恒式/信封/回填全部有断言护住）。

## 0. 脚本清单（全部已打包在 skill 内）

| 脚本 | 用途 |
| --- | --- |
| `scripts/analyze_ttft.py` | 核心解析器：4 类日志 → Excel 8 Sheet + 终端结论 |
| `scripts/fetch_logs.sh` | 远端宿主机侧定位/校验/暂存日志（bash，在远端执行） |
| `scripts/gen_synth_logs.py` | 合成 v3 打点日志（演示/构造测试数据） |
| `scripts/test_regression.py` | 回归自检：合成日志全断言（回填/信封/守恒/LB），改动解析器后必跑 |

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

## 2. 采集前确认参数

| 参数 | 说明 |
| --- | --- |
| P 日志 | `prefill.log`（breakdown + KV 传输 + shm/draft），多 P 每台一份 |
| D 日志 | `decode.log` |
| proxy 日志 | `load_balance.log` |
| 压测结果 | `manual_*_dumpjsonl.json`（必须 `--save-detailed`） |
| 输出 | `-o TTFT_xxx_ms.xlsx`（文件名含机器/轮次，可追溯） |

⚠️ 四份必须是**同一次压测**（请求数对齐），否则对账无意义；累计型日志用
`--one-burst` 整段处理，或按 `--burst-gap N`（秒）分段。

## 3. 采集步骤

1. **远端定位日志**：上传 `scripts/fetch_logs.sh` 到宿主机执行（ssh 连接优先
   ssh-mcp-server 的 upload/execute-command/download，无 MCP 时回退本地 ssh/scp）。
   `/data` 是共享挂载，容器内 `/data/.../p0-1112/log/prefill.log` 一类路径宿主机可见：

   ```bash
   bash fetch_logs.sh --root /data/<user>          # 列出四类日志 + 打点行数校验
   bash fetch_logs.sh --root /data/<user> --stage /tmp/ttft_fetch   # 暂存待下载
   ```

   Windows 写出的 .sh 带 CR：远端先 `tr -d '\r' < f > f2`（本 skill 脚本已是 LF，
   从别处同步的才需要）。脚本不内置任何节点/路径——root 一律显式传入。
2. **下载到同一本地目录**：P/D/proxy 日志 + 压测 result json（P 日志多台时建议
   命名 `P<ip>_prefill.log` 区分）。
3. **校验**：fetch_logs.sh 的输出里 grep 到 `[TTFT][BREAKDOWN]`、`[TTFT_DECODE]`、
   `[LB_DISPATCH]` 且 breakdown 行数 ≈ 压测请求数；`[SHM_DEQUEUE_BATCH]` 与
   `[TTFT_DRAFT]` 存在（否则 dequeue / mtp 列为 0）。
4. **压测（若还没数据）**：

   ```bash
   python utils/manual_benchmark_existing_service.py --config-files configs/npu-master.yaml \
     --config-key glm52-omni --base-url http://127.0.0.1:7200 --served-model-name glm52 \
     --seq-lens 8k5 --conc 16 --dataset-name dumpjsonl \
     --dataset-path omni_dump_100_fixed_8k512.jsonl --num-warmups 1 --skip-health-check --save-detailed
   ```

## 4. 解析出表

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

**CSV 导出（下游对接）**：加 `--csv-out ttft_table.csv` 额外输出一份
`kptools-tpsplit-report`（TP 拆分收益对比）skill 约定的表格：首行表头
`ts,request_id,input_len,ttft_ms,net_lb_ms,fe_inputq_ms,fe_addreq_ms,sched_qwait_ms,
sched_total_ms,fwd_pure_ms,draft_ms,kv_xfer_ms,shm_dq_ms,other_ms`，每请求一行。
v3 列 → 10 阶段映射：net_lb=lb_dispatch、fe_inputq=add_req_iq、fe_addreq=add_req_proc、
sched_qwait=queue_wait、sched_total=sched_total、fwd_pure=forward、draft=mtp、
kv_xfer=load_kv+save_kv、shm_dq=dequeue、other=ttft−Σ(引擎内 9 阶段)（兜底）。
注意：v3 引擎侧 ttft **不含** net_lb，因此 Σ10 阶段 − ttft = net_lb（几 ms 量级，
在下游守恒容差 max(2ms, 2%·ttft) 内）；input_len 需打点行携带，缺省输出 0。

## 5. 列口径（v3，当前生效）

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

## 6. 解读与已知坑

- **对账**：`client_ttft ≈ client2proxy(网络) + first_token(分发+P处理+P→D交接+D首
  token) + return(proxy→client)`；同机回环 return≈0，偏大=钟偏或真实网络。
- **D 侧 `num_total` 常为 0**（纯 decode）：`decode_forward` 由 EngineCore 墙钟兜底，正常。
- **`sample` 在 PP+last-rank 常 0**，prefill worker 预期为 0，不是 bug。
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
