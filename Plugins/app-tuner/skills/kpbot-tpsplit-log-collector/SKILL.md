---
name: kpbot-tpsplit-log-collector
description: >
  从远端 P/D/proxy/压测端宿主机定位、校验、暂存并回传 TTFT 打点日志（一次压测一套
  4 份：P prefill.log（多 P 每台一份）/ D decode.log / proxy load_balance.log /
  压测端 manual_*_dumpjsonl.json），产物统一落到本地同一目录交下游解析。当用户提到
  以下场景时必须使用本技能：采集/下载/回传 prefill/decode/load_balance 日志、
  fetch_logs.sh、--root/--stage/--expect-req、打点日志定位与行数校验、四份日志是否
  同一次压测、多 P 同名 prefill.log 暂存防覆盖、压测结果 json 回传。即使用户只说
  "把日志拉下来"或"去 /data 找一下这轮压测的打点日志"，也应使用此技能。本 skill
  只做采集与校验；解析出表（Excel/CSV/对账/回填）归 kptools-tpsplit-log-analysis，
  打点 patch 部署与生效排查归 kptools-tpsplit-log-trace（配套 PR #340 交付）。
license: Proprietary
compatibility: >
  本地 Windows/Linux（发起方）；SSH 访问 P/D/proxy 宿主机（优先 ssh-mcp-server 的
  upload/execute-command/download，无 MCP 时回退本地 ssh/scp）；fetch_logs.sh 在远端
  宿主机执行（需 bash 与 grep/find/du；压测请求数精确计数需宿主机有 python3，无则
  退化为键字面量计数并打告警）；打点须已部署且输出 v3 格式（[TTFT][BREAKDOWN] /
  [TTFT_KVXFER] / [SHM_DEQUEUE] / [LB_DISPATCH] / [TTFT_DRAFT]，部署工具随配套 PR
  交付）；Windows 写出的 .sh 带 CR，上传远端前先 tr -d '\r'（本 skill 脚本已按 LF
  交付）。
metadata:
  skill_type: tool_assisted
  category: diagnostics
  domain: ttft_log_collection
  activation_mode: active
  version: "0.1.0"
---

# kpbot-tpsplit-log-collector：TTFT 打点日志采集（定位/校验/回传）

打点已部署且跑过压测时，本 skill 只做一件事：**把一次压测的一套 4 份日志从远端
宿主机定位、校验、暂存并回传到本地同一目录**，交 kptools-tpsplit-log-analysis
解析出表。

**采集与解析分离**：日志在远端，产物统一为本地一套 4 份文件；解析出表由
kptools-tpsplit-log-analysis 的 `scripts/analyze_ttft.py` 完成。本 skill 不做任何
解析、不手算占比——采集端只负责把「齐、对、真」的日志交出去：

- **齐**：4 份（P/D/proxy/压测）都找得到；P 或压测 json 缺失直接判失败（退出码 1）
- **对**：四份来自同一次压测（P 侧 BREAKDOWN 总行数 ≈ 压测请求数，不符即告警）
- **真**：标记计数逐次精确（`grep -oF`）、同一文件跨多 root 不重复计数、多 P
  同名 prefill.log 不互相覆盖

## 0. 脚本清单

| 脚本/文件 | 用途 |
| --- | --- |
| `scripts/fetch_logs.sh` | 远端宿主机侧定位/校验/暂存日志（bash，在远端执行） |
| `references/log-set-contract.md` | 一套 4 份的采集产物契约（命名/同轮对齐/标记计数口径，采集侧权威） |

（解析器、合成日志生成器、回归自检均随 kptools-tpsplit-log-analysis 交付，本 skill 不带）

## 1. 采集前确认参数

| 参数 | 说明 |
| --- | --- |
| P 日志 | `prefill.log`（breakdown + KV 传输 + shm/draft），多 P 每台一份 |
| D 日志 | `decode.log` |
| proxy 日志 | `load_balance.log` |
| 压测结果 | `manual_*_dumpjsonl.json`（必须 `--save-detailed`） |
| 本地输出目录 | 四份落同一目录；多 P 建议命名 `P<ip>_prefill.log` 区分 |

⚠️ 四份必须是**同一次压测**（请求数对齐），否则下游对账无意义。

## 2. 采集步骤

1. **远端定位日志**：上传 `scripts/fetch_logs.sh` 到宿主机执行（ssh 连接优先
   ssh-mcp-server 的 upload/execute-command/download，无 MCP 时回退本地 ssh/scp）。
   `/data` 是共享挂载，容器内 `/data/.../p0-1112/log/prefill.log` 一类路径宿主机可见：

   ```bash
   bash fetch_logs.sh --root /data/<user>/<本轮目录>   # --root 必填（缺参直接退出，防误扫共享根）
   bash fetch_logs.sh --root /a --root /b              # 多根追加；同一文件（realpath 相同）只计一次
   bash fetch_logs.sh --root /data/<user>/<本轮目录> --stage /tmp/ttft_fetch   # 暂存待下载
   bash fetch_logs.sh --root /data/<user>/<本轮目录> --expect-req 100          # BREAKDOWN 总行数（多 P 求和）低于门槛 → 非零退出
   ```

   **先选定本轮目录/清单再采集**：`--root` 必须指到**本轮压测**的实际目录；同一用户
   目录下保留多轮压测日志时，整目录扫描会把两轮日志一起计数和暂存（即使 P 侧与压测
   请求总数恰好相等，也无法区分两轮）——先用 `ls`/`find` 列出候选目录与文件清单，
   由用户（或按本轮压测时间戳）选定本轮范围后再执行；范围不明确时停下来问，不要
   拿"目录里全都有"当成本轮。选定为文件清单时直接把文件作为位置参数传入（文件名
   须为 `prefill.log` / `decode.log` / `load_balance.log` / `manual_*_dumpjsonl.json`
   之一，无法识别的文件报错并计为校验失败），计数与暂存只算清单内文件：

   ```bash
   bash fetch_logs.sh /data/<user>/log/prefill.log /data/<user>/manual_xxx_dumpjsonl.json --stage /tmp/ttft_fetch
   ```

   Windows 写出的 .sh 带 CR：远端先 `tr -d '\r' < f > f2`（本 skill 脚本已是 LF，
   从别处同步的才需要）。脚本不内置任何节点/路径——root 一律显式传入；打点标记
   统计用 `grep -oF` 逐次计数（`-c` 只数行，单行多记录会漏计）；压测请求数按
   解析出的 JSON 记录计数（与 kptools-tpsplit-log-analysis 的 `analyze_ttft.py`
   load_bench 同口径，宿主机无 python3 时退化为键字面量计数并打告警），记录总数
   与含 `client_send_ts` 的有效数分开报告，缺字段记录逐文件告警；
   `--stage` 暂存文件名带完整来源路径（多 P 同名 prefill.log 不会互相覆盖），
   暂存用排他占位（noclobber）创建，并发采集冲突/已存在/复制失败均非零退出。
2. **下载到同一本地目录**：P/D/proxy 日志 + 压测 result json（P 日志多台时建议
   命名 `P<ip>_prefill.log` 区分，与解析侧 `LABEL:path` 输入对齐）。
3. **校验**：fetch_logs.sh 的输出里 grep 到 `[TTFT][BREAKDOWN]`、`[TTFT_DECODE]`、
   `[LB_DISPATCH]` 且 breakdown 行数 ≈ 压测请求数；`[SHM_DEQUEUE_BATCH]` 与
   `[TTFT_DRAFT]` 存在（输出中看不到这两个标记的计数行 = 该 P 日志无此打点，
   下游 dequeue / mtp 列将为 0——采集端检出提示即止，环境变量排查见打点部署
   skill 的启动 env 一节）。
4. **压测（若还没数据）——先取得授权，不自动发起**：日志链路缺数据时，先把缺项
   （缺 P / D / proxy / 压测 json 哪几份）返回给用户，并说明下一步动作会对目标服务
   发起 `--conc 16` 的真实压测负载；取得用户对**目标服务、负载与执行窗口**的明确
   授权后才补跑，未经授权不得自行发起压测。授权后的命令：

   ```bash
   python utils/manual_benchmark_existing_service.py --config-files configs/npu-master.yaml \
     --config-key glm52-omni --base-url http://127.0.0.1:7200 --served-model-name glm52 \
     --seq-lens 8k5 --conc 16 --dataset-name dumpjsonl \
     --dataset-path omni_dump_100_fixed_8k512.jsonl --num-warmups 1 --skip-health-check --save-detailed
   ```

   （压测工具在压测端工作区、非本 skill 自带；独立压测/吞吐评估诉求不归本 skill
   ——本步骤仅为日志链路「数据还没有」时的前置补数据，必须 `--save-detailed`）

## 3. 采集产物契约（摘要）

- 一套 4 份 = P（多 P 每台一份）/ D / proxy / 压测 json，落本地同一目录
- 同轮对齐：P 侧 BREAKDOWN 总行数（多 P 求和）≈ 压测记录数；偏差为软告警
  （不改变退出码），出现时先确认四份是否同一轮压测
- 命名细则、标记计数口径、`--expect-req` 门槛、暂存规则：见
  `references/log-set-contract.md`（采集侧权威）；下游解析列语义/守恒式/回填
  口径的权威在 kptools-tpsplit-log-analysis 侧，两侧互不重复定义

## 4. 常见坑（采集侧）

- **多 P 求和口径**：`--expect-req` 按全部 P 日志 BREAKDOWN 行数**之和**校验——
  多 P 每份只占总请求数一部分，逐文件卡门槛会把合法采集误判为失败
- **扫描根必填**：不给 `--root` 直接退出（退出码 2）——`/data` 是共享挂载，默认
  递归整个根会把其他用户目录下可读的同名日志也扫进来；获准采集的目录须显式传入，
  多根追加，亦可传文件清单（见采集步骤 2）
- **同名覆盖**：直接 scp 多台 P 的 prefill.log 到同一目录会互相覆盖——用
  `--stage`（暂存名含完整来源路径，`/` 与 `:` 替换 `__`）或下载后重命名
  `P<ip>_prefill.log`
- **Windows CR**：本仓库 .gitattributes 强制 .sh 为 LF；从其它工作副本同步脚本
  后远端执行报 `\r` 相关错误时，先 `tr -d '\r'`
- **[SHM_DEQUEUE_BATCH] 计数行缺失 ≠ 脚本漏检**：脚本只打印非 0 标记——看不到
  该行即该 P 日志无 shm 打点（VLLM_TTFT_SHM_LOG=1 未设置/未生效，Ray actor 的
  env 来自 raylet、启动 shell export 不生效），下游 dequeue 列将为 0；是否重采
  由用户决定
- **压测 json 必须带 `--save-detailed`**：没有 `client_send_ts` 的 result json
  无法对账
- **宿主机无 python3**：压测请求数退化为键字面量计数（有偏差告警），需要精确
  门槛校验时先确认采集机环境

## 边界与注意

- 本 skill 只做采集与校验：解析出表（Excel 8 Sheet / ttft_table.csv / 对账 / 长尾
  归因）转 kptools-tpsplit-log-analysis（配套 PR #399）；打点 patch 部署与「打点
  没输出」排查转 kptools-tpsplit-log-trace（配套 PR #340）；TP 拆分前后收益对比转
  kptools-tpsplit-report
- **配套拆分说明**：本 skill 由 kptools-tpsplit-log-analysis 的采集侧拆出
  （`fetch_logs.sh` 与采集流程迁入本 skill）。**脚本与拆分前并非逐字节一致**：
  迁入后按检视意见有修复——BSD/macOS wc 输出填充（count_marker 与无 python3 兜底
  两处）、`--root` 必填（防误扫共享根）、`--stage` 排他创建（防并发覆盖）、压测
  记录总数/有效 client_send_ts 数双口径、空数组偏差告警；采集语义零变化，差异
  以 git 历史为准。与解析侧收窄 PR（#399）改动不相交、任意先后合入无冲突；
  建议同批合入，避免采集触发面在过渡期与解析侧重叠
- 只读远端（find/grep/du、cp 仅写入 `--stage` 指定的暂存目录）：不改配置、不重启
  引擎、不动集群；不内置任何节点/路径列表

## 文件

- `scripts/fetch_logs.sh`：远端定位/校验/暂存（上传宿主机执行，用法见脚本头注释）
- `references/log-set-contract.md`：一套 4 份采集产物契约（命名/同轮对齐/计数口径）
- `evals/`：触发评测集（正/负例路由矩阵）；`evals/fixtures/`（含 noshm 变体）可
  本地搭假 root 跑 fetch 校验流程
