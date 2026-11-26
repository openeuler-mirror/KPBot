# TerminalBench benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 逐
> case 对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（4 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| terminalbench_01_analyze_access_logs | 29 | 23/1/2/1/0 | 561,948 | 62.5 | 无 | agbench-terminalbench_01_analyze_access_logs:v1 |
| terminalbench_02_jsonl_aggregator | 27 | 23/1/1/0/0 | 422,177 | 66.7 | 无 | agbench-terminalbench_02_jsonl_aggregator:v1 |
| terminalbench_03_log_summary | 32 | 24/2/2/2/0 | 570,488 | 59.3 | 无 | agbench-terminalbench_03_log_summary:v1 |
| terminalbench_04_sql_query_optimization | 39 | 34/1/1/0/1 | 1,038,392 | 95.4 | 无 | agbench-terminalbench_04_sql_query_optimization:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py verify`，
判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **零跳过调用**：与 gaia 等 benchmark 不同，4 条会话的全部录制调用（exec/read/write/
  edit/apply_patch）都映射为沙箱内回放步骤——录制总时长可近似估计沙箱压力形态。
- **exec 密集**：合计约 104 个 exec 步（占比 ~84%），能力集中在日志分析、JSONL 聚合、
  摘要与 SQL 优化等数据处理型终端操作；归因重点看 exec 耗时分布与 busy/stall 子窗口。
  case 04 含 apply_patch（SWE patch 由导入器解析为原地补丁脚本，与 exec 共用串行命令通道）。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，terminalbench 压测子集目录必须**恰好**包含上表
4 条 `terminalbench_*.replay.json` 与对应 image-map 条目；混入其他 benchmark 轨迹会破坏
「同负载可比」口径（tier-A 用例的命令纯度检查项即为此设）。
