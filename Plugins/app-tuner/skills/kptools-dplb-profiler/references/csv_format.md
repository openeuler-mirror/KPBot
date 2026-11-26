# csv_format：dp_load_metrics CSV 输出格式定义

本文件是三个采集脚本（`extract_metric_v2.sh` / `extract_metric_pd.sh` / `extract_metric_prom.py`，含历史 `extract_metric_single.sh`）输出格式的定义——采集端产出必须严格符合本契约，下游 kptools-dplb-report 的 `generate_report.py` 按同一契约解析。**权威层级：kptools-dplb-report 的 `references/report_spec.md` §1 为 CSV 格式的唯一权威定义，本文件为其采集侧镜像（便于采集场景单独查阅）；两侧内容冲突时以 report_spec.md §1 为准，任一侧修改列名/口径时必须同步另一侧与上列采集脚本。**

## 1. CSV 格式定义

两种口径，下游自动识别：表头含 `ttft_sum` → **v2/Prom（30 数据列）**；否则含 `ttft_avg` → **原版（20 数据列）**。

CSV 首两列固定为 `timestamp`（人类可读本地时间 `YYYY-MM-DD HH:MM:SS`，下游兼容 epoch 秒与 ISO8601）与 `ip_port`（引擎标识，`ip:port`、`ip_port` 或 `ip:port.engine`，host/port/引擎分组按此解析）。**timestamp 语义因采集路径而异**：现场轮询（v2/PD/single）写入本轮轮询发起时刻（请求前取的 `ROUND_TS`，列值为该时刻端点返回的当前快照）；Prometheus 回采写入**查询求值时刻**（`query_range` 按 `--step` 对齐的求值点，取该时刻前最近一次抓取的样本；步长小于抓取间隔时同一底层样本会复现相邻多行，不得视为独立抓取事件、不得与现场日志精确对齐）。缺失值统一填 `-1`，下游视为缺失（绝不当 0）。

### 1.1 基础 20 列（两口径共有）

| # | 列名 | 来源指标 | 类型 | 说明 |
|---|------|---------|------|------|
| 1 | num_requests_running | vllm:num_requests_running | gauge | 运行中请求数 |
| 2 | num_requests_waiting | vllm:num_requests_waiting | gauge | 等待队列长度 |
| 3 | num_requests_swapped | vllm:num_requests_swapped | gauge | 换出请求数 |
| 4 | request_success_total | vllm:request_success_total（按 finished_reason 等标签求和） | counter | 成功请求累计 |
| 5 | request_failure_total | vllm:request_failure_total | counter | 失败请求累计，端点无此序列填 -1 |
| 6 | num_preemptions_total | vllm:num_preemptions_total | counter | 抢占累计 |
| 7 | prompt_tokens_total | vllm:prompt_tokens_total | counter | 输入 token 累计 |
| 8 | generation_tokens_total | vllm:generation_tokens_total | counter | 输出 token 累计 |
| 9 | request_validation_time_total | vllm:request_validation_time_total | counter | 请求校验耗时累计（秒），无则 -1 |
| 10 | kv_cache_usage_perc | vllm:gpu_cache_usage_perc（回退 vllm:kv_cache_usage_perc） | gauge | **0~1 比值**，非百分数 |
| 11 | cpu_cache_usage_perc | vllm:cpu_cache_usage_perc | gauge | 0~1 比值 |
| 12 | prefix_cache_hit_rate | 优先 vllm:prefix_cache_hits_total ÷ vllm:prefix_cache_queries_total（counter 累计比，queries=0 记 0）；回退旧版 gauge vllm:prefix_cache_hit_rate | gauge/派生 | 本地前缀缓存命中率，可能恒 0 |
| 13 | pending_prefix_cache_hit_rate | vllm:pending_prefix_cache_hit_rate | gauge | 无则 -1（vLLM v1 不导出） |
| 14 | external_prefix_cache_hit_rate | 优先 vllm:external_prefix_cache_hits_total ÷ vllm:external_prefix_cache_queries_total；回退旧版 gauge | gauge/派生 | 外部前缀缓存命中率 |
| 15 | process_start_time_seconds | process_start_time_seconds | gauge | 进程启动时间，用于 uptime 一致性检测 |
| 16 | ttft_avg | 派生：ttft_sum/ttft_count 采样点快照 | 派生 | 累计平均；sum 或 count 任一缺失、或 count=0 时填 -1 |
| 17 | queue_avg | 同上（vllm:request_queue_time_seconds） | 派生 | 累计平均；缺失口径同 ttft_avg；负 queue 已修正（见 1.3） |
| 18 | prefill_avg | 同上（vllm:prefill_time_seconds，回退 vllm:request_prefill_time_seconds） | 派生 | 累计平均；缺失口径同 ttft_avg；吸收负 queue 差值（见 1.3） |
| 19 | tpot_avg | 同上（vllm:request_time_per_output_token_seconds，回退 vllm:time_per_output_token_seconds） | 派生 | 累计平均；缺失口径同 ttft_avg |
| 20 | e2e_avg | 同上（vllm:e2e_request_latency_seconds，回退 vllm:request_latency_seconds） | 派生 | 累计平均；缺失口径同 ttft_avg |

### 1.2 v2/Prom 额外 10 列（30 列口径）

| # | 列名 | 来源指标 | 说明 |
|---|------|---------|------|
| 21 | ttft_sum / ttft_count | vllm:time_to_first_token_seconds 的 _sum/_count | 窗口内平均 = Δsum/Δcount |
| 22 | queue_sum / queue_count | vllm:request_queue_time_seconds | 同上 |
| 23 | prefill_sum / prefill_count | vllm:prefill_time_seconds（回退 request_prefill_time_seconds） | 同上 |
| 24 | tpot_sum / tpot_count | vllm:request_time_per_output_token_seconds（回退 time_per_output_token_seconds） | 同上 |
| 25 | e2e_sum / e2e_count | vllm:e2e_request_latency_seconds（回退 request_latency_seconds） | 同上 |

原版 CSV = 首两列 + 20 基础列（共 22 列表头）；v2/Prom CSV = 首两列 + 30 数据列 + queue_fixed 标记列（共 33 列表头）。文档中「20 列 / 30 列」均指**数据列数**。

### 1.3 公共列口径

- **timestamp**：统一为人类可读本地时间 `YYYY-MM-DD HH:MM:SS`（三种采集路径一致）；**语义区分**——现场轮询为本轮轮询发起时刻（值为该时刻的端点快照），Prometheus 回采为查询求值时刻（`query_range` 按 `--step` 对齐，取求值点前最近一次抓取的样本；步长小于抓取间隔时同一样本复现相邻多行）；下游兼容 epoch 秒与 ISO8601（含时区），无法解析的行跳过并计入告警。
- **派生列（`*_avg`）缺失口径**：仅当同一采样轮次的 sum 与 count 都存在且 count>0 才计算平均值；sum 或 count 任一缺失、或 count=0 时一律写 `-1`。三种采集路径与 Prometheus 回采统一，避免 sum 缺失时把同一缺口解释成零延迟（`0`）而非缺失。
- **queue_fixed（v2/Prom 末列，可选）**：1 = 该采样点 queue_sum<0 被修正（已知 vLLM 导出 bug：queue 为负且 prefill 同步虚高）。修正口径：queue=0、prefill_sum += 原 queue_sum，三种采集路径一致；下游报告对被修正引擎在数据口径脚注标注。
- **缺失与未导出**：`-1` 为采集缺失兜底；全数据集 100% 缺失的列视为「该部署未导出」（如 vLLM v1 的 swapped / request_failure_total / request_validation_time_total / cpu_cache_usage_perc / pending_prefix_cache_hit_rate），下游不计入缺失率分母。
