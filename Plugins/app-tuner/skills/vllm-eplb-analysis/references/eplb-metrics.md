# EPLB 不均衡指标参考

本文说明每个指标从哪来、怎么算、阈值为什么这样取，以及如何用现场基线校准。
所有 vLLM 源码位置基于 vLLM 0.34 检出（`vllm/distributed/eplb/eplb_state.py` 等）。

## 目录

- [1. balancedness（I1）](#1-balancednessi1)
- [2. 在途请求倾斜（I2）](#2-在途请求倾斜i2)
- [3. TTFT 倾斜（I3）](#3-ttft-倾斜i3)
- [4. prompt token 倾斜（I4）](#4-prompt-token-倾斜i4)
- [5. 持续性与窗口（I5）](#5-持续性与窗口i5)
- [6. 证据 JSON 完整字段](#6-证据-json-完整字段)
- [7. 阈值校准](#7-阈值校准)
- [8. 显存余量（M0）](#8-显存余量m0)
- [9. 代价与前置条件](#9-代价与前置条件)

## 1. balancedness（I1）

**定义**：每个 MoE 层先按 EP rank 求该 rank 收到的 token 总数，取 `mean(ranks)` 与 `max(ranks)`，
再跨层求和；`balancedness = avg_tokens / max_tokens`。1.0 表示各 rank 完全均匀。

源码：`vllm/distributed/eplb/eplb_state.py`

```python
def _compute_eplb_load_stats(num_tokens_per_rank):
    avg_tokens = num_tokens_per_rank.mean(dim=1).sum()
    max_tokens  = num_tokens_per_rank.max(dim=1).values.sum()
    return avg_tokens, max_tokens
```

日志格式（`_maybe_log_balancedness`，每 `log_balancedness_interval` 步一行）：

```
EPLB step: 400 for model dsv41: avg_tokens=8188.61, max_tokens=10365,
balancedness=0.7900, steps until the next rearrangement: 600
```

开关：`vllm/config/parallel.py` 中 `EplbConfig.log_balancedness`（默认 `False`）、
`log_balancedness_interval`（默认 `1`）。对应命令行：

```bash
--eplb-config.log_balancedness true
--eplb-config.log_balancedness_interval 1
```

**限制**：只有启用了 `--enable-eplb` 才有这段日志（`EplbState._maybe_log_balancedness`）。
因此它只能用于"已经开着 EPLB，复核摆放够不够"，不能作为"还没开、要不要开"的唯一依据。

## 2. 在途请求倾斜（I2）

指标：`vllm:num_requests_running`、`vllm:num_requests_waiting`（gauge，label `engine`）。
`engine` 对应调度器实例；DP 部署时每个 DP rank 一个 engine，因此可以直接看出"哪个 rank 在排队"。

源码：`vllm/v1/metrics/loggers.py`（`labelnames = ["model_name", "engine"]`）。

指标口径：`inflight[i] = running[i] + waiting[i]`，取 `max / median`。
一般服务里 waiting 才是排队证据，但 decode 阶段 running 也会贴着上限，两者相加更稳。

## 3. TTFT 倾斜（I3）

指标：`vllm:time_to_first_token_seconds`（histogram，按 engine 分标签）。
直方图有 `_bucket{le=...}`、`_sum`、`_count`，脚本用 `sum/count` 得到该 engine 的平均 TTFT，
需要 P95 时可用桶内插值（`hist_quantile`）。

为什么优先用均值而不是极值：单请求极值受请求长度影响，均值在窗口足够长时更能反映 rank 级差异。

## 4. prompt token 倾斜（I4）

指标：`vllm:prompt_tokens`（counter，label `engine`）。
若用 `/metrics` 原始计数，需要采两次求差分；脚本接受证据 JSON 里的 `rank_prompt_tokens` 直接给出
窗口内增量。prefill 场景下这是最直接的工作量倾斜证据。

## 5. 持续性与窗口（I5）

单窗口的 balancedness 波动很大（batch 组成、请求长度都影响）。脚本要求：

- 至少 `min_windows = 3` 个 balancedness 采样，否则判 `inconclusive`；
- `persistence = 落在 "<0.95" 区间的窗口占比`，`>= 0.60` 才算持续失衡。

窗口长度建议覆盖一个完整业务周期；压测场景至少 4 轮（含 1 轮预热）。

## 6. 证据 JSON 完整字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `service` / `window` | str | 报告标识 |
| `balancedness` | float[] | 多个窗口的 balancedness |
| `rank_waiting` / `rank_running` | float[] | 按 engine 的在途请求数，或 |
| `rank_ttft_ms` | float[] | 按 engine 的平均 TTFT（毫秒） |
| `rank_prompt_tokens` | float[] | 窗口内各 engine 处理的 prompt token 增量 |
| `rank_util_pct` | float[] | 各 rank 设备利用率（可选，用于排除非专家瓶颈） |
| `kv_cache_usage_pct` | float | KV cache 占用（0~100），可替代 `vllm:kv_cache_usage_perc` |
| `preemptions_delta` | int | 窗口内换出增量（门控用它） |
| `preemptions_total` | int | 进程累计换出次数；只作参考，不参与门控 |
| `free_mem_gib` | float | 剩余显存（GiB），可选 |
| `bytes_per_expert_gib` | float | 每个冗余专家在每个 EP rank 上的增量显存（GiB），DeepSeekV3 约 2.4 |
| `ep_size` | int | EP rank 数，用于把每 rank 余量换算成全局副本数 |
| `thresholds` | object | 覆盖默认阈值 |

`rank_*` 与对应的 Prometheus 指标同时存在时，证据 JSON 优先（便于人工复核口径）。

## 7. 阈值校准

默认值面向"EP rank 数 ≥ 4、窗口 ≥ 3、长稳流量"：

| 阈值 | 默认 | 取这个值的理由 |
|---|---|---|
| `balancedness_imbalanced` | 0.90 | 0.9 意味着最忙 rank 比平均多 11% token，已在专家层面明显倾斜 |
| `balancedness_healthy` | 0.95 | 0.95 时最忙 rank 只多 5%，通常不值得为它付出副本显存与重排抖动 |
| `queue_ratio` | 1.5 | 排队是离散量，小规模 rank 数下 1.5 倍才脱离噪声 |
| `ttft_ratio` | 1.3 | 端到端 TTFT 受多因素影响，1.3 倍差异才像结构性长尾 |
| `prompt_ratio` | 1.2 | token 计数噪声小，1.2 倍即可提示工作量倾斜 |
| `persistence` | 0.6 | 允许偶发窗口抖动，但不允许"偶尔一次"就上线 |

校准方法：先在**已知均衡**的基线流量上跑一遍，看各比值的自然分布，把阈值设到该分布的 P95 之外；
再在**人为不均衡**流量（重复长前缀）上确认能触发。两次都不满足时，宁可判 `inconclusive`。

## 8. 显存余量（M0）

**换出必须看增量，不能看累计值**：`vllm:num_preemptions` 是 Prometheus counter，自进程启动起只增不减。
"历史上换出过一次"在长跑服务里很常见，不代表现在缺 KV。正确做法是隔一个业务周期采两份 `/metrics`，
用 `--prometheus`（最新）与 `--prometheus-prev`（更早）做差分，或直接在证据里给 `preemptions_delta`。
只有 `preemptions_total` 时脚本不会因此判无余量，只在报告里提示去补差分。

**为什么不能看 设备剩余显存**：vLLM 启动时按 `gpu_memory_utilization` 预分配权重与 KV cache，之后
设备 free 通常接近 0。真正决定"还能不能加副本"的是 KV cache 的紧张程度，以及你愿意为副本让出多少 KV。

| 证据 | 来源 | 用法 |
|---|---|---|
| KV cache 占用 | `vllm:kv_cache_usage_perc`（gauge，0~1，按 engine） | 取各 engine 最大值；≥85% 偏紧，≥95% 无余量 |
| 换出增量 | `vllm:num_preemptions`（counter，按 engine），两份快照做差 | **增量 >0** 判无余量；只有一个累计值时不参与门控，只给提示 |
| 剩余显存 | 启动日志 "Available KV cache memory"/设备工具 | 可选；启动后通常很小，只作辅助 |
| 每副本成本 | 官方公式（见下）或实测 | 换算"这点余量最多放几个副本" |

**每副本显存成本**：

```
per_rank_extra_bytes = NUM_MOE_LAYERS × BYTES_PER_EXPERT × NUM_REDUNDANT_EXPERTS / NUM_EP_RANKS
```

`NUM_REDUNDANT_EXPERTS` 是全集群总数，所以"每个 EP rank 增加几个副本"= `NUM_REDUNDANT_EXPERTS / NUM_EP_RANKS`。
官方文档给出的 DeepSeekV3 参考值：每 EP rank 每多一个冗余专家约 2.4 GB（即上式在 DeepSeekV3 参数下的取值）。

**可负担副本数**（脚本 `affordable_redundant`）：

```
affordable = floor(free_gib / bytes_per_expert_gib) × ep_size
```

其中 `bytes_per_expert_gib` 指"一个冗余专家在每个 EP rank 上的增量"（对应上面 2.4 GB 的口径）。
`num_redundant_experts` 必须能被 `ep_size` 整除（vLLM 自动取值时也是向上取整到 ep_size 的倍数），
所以这里乘回 `ep_size`。

**判定**：

| memory.verdict | 条件 | 动作影响 |
|---|---|---|
| `no_headroom` | KV ≥95%，或窗口内换出增量 >0，或 free ≤0.5 GiB，或 affordable ≤0 | 动作改为 `free_memory_first`，不交接加副本 |
| `tight` | KV ≥85%，或 affordable < 8 | 可开 dynamic，但冗余专家按 affordable 收敛（`optimize_with_cap`） |
| `ample` | KV <85% 且窗口无换出 | 按失衡强度给参数 |
| `unknown` | 未提供任何显存证据 | 不阻塞结论，交接时提示上线前确认 |

## 9. 代价与前置条件

- 冗余专家占显存：`NUM_MOE_LAYERS * BYTES_PER_EXPERT * (NUM_TOTAL_EXPERTS + NUM_REDUNDANT_EXPERTS) / NUM_EP_RANKS`
  （官方文档示例：DeepSeekV3 每个 EP rank 每多一个冗余专家约 2.4 GB）。
- `--enable-eplb` 需要专家并行 `--enable-expert-parallel` 作为前提。
- vLLM 官方默认 `window_size=1000`、`step_interval=3000`、`num_redundant_experts=0`；
  大规模部署建议冗余专家取 32。压测周期短于 `step_interval` 时不会触发重排，验证必然看不到收益。

参考：`vllm/docs/serving/expert_parallel_deployment.md`（EPLB 配置与内存开销章节）。
