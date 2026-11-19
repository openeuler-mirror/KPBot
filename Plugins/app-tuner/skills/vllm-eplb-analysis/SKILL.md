---
name: vllm-eplb-analysis
description: 用多个独立指标判断 vLLM MoE 推理服务到底有没有专家负载不均衡，再决定要不要上 EPLB。当用户问"是不是负载不均衡""EPLB 有没有必要开""balancedness 多少算不均衡""TTFT 长尾是不是专家热点""EPLB 开了没效果是不是本来就均衡"，或者要分析 EPLB 日志、vllm:num_requests_running / vllm:num_requests_waiting / vllm:time_to_first_token_seconds 等指标时，务必使用本技能——即使用户只说"帮我看下需不需要开 EPLB"。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: diagnostics
  domain: vllm_moe_eplb
  activation_mode: active
  side_effect_free: "true"
  version: "1.0.0"
---

# EPLB 不均衡分析（vLLM MoE）

## 为什么不能只信一个数

`balancedness = avg_tokens / max_tokens`（每层按 EP rank 求均值与最大值，再跨层求和）看起来很像
"失衡程度"，但单独用它做决定有两个坑：

1. **它只在已经打开 EPLB 时才有**（`eplb_config.log_balancedness`），而我们要回答的往往正是
   "还没开的时候该不该开"。
2. **它只描述专家拿到的 token 分布，不描述你的服务慢不慢**。0.93 的 balancedness 完全可能来自
   短期采样噪声或一次倾斜的 batch，此时上 EPLB 只会多占显存、多一次重排抖动。反过来，端到端 TTFT
   长尾也可能来自排队、KV 压力、通信，而不是专家热点。

所以本技能的核心是**交叉验证**：用互相独立的指标各自看一遍，只在这些指标同向、且失衡能持续
若干个窗口时才判定"真的不均衡"。

## 五个指标

| 编号 | 指标 | 看什么 | 怎么来 | 默认阈值 |
|---|---|---|---|---|
| I1 | balancedness | 各 EP rank 的专家 token 数 `avg/max` | 已开 EPLB 时的 `EPLB step:` 日志 | 中位数 < 0.90 记为失衡；0.90~0.95 只是边缘 |
| I2 | 在途请求倾斜 | `num_requests_running + num_requests_waiting` 按 engine | `vllm:num_requests_*`（label `engine`） | max/中位数 ≥ 1.5 |
| I3 | TTFT 倾斜 | 每个 engine 的 TTFT 均值/P95 | `vllm:time_to_first_token_seconds` 直方图 | max/中位数 ≥ 1.3 |
| I4 | prompt token 倾斜 | 各 engine 处理的 prompt token 数 | `vllm:prompt_tokens` 计数增量 | max/中位数 ≥ 1.2 |
| I5 | 持续性 | 失衡是否稳定存在 | 多窗口 balancedness 序列 | 至少 60% 的窗口处于非均衡区 |
| M0 | 显存余量 | KV cache 占用、换出次数、剩余显存、每个副本的显存成本 | `vllm:kv_cache_usage_perc`、`vllm:num_preemptions`、启动日志、设备工具 | 占用 ≥95% 或已发生换出 = 无余量；≥85% = 偏紧 |

阈值都可通过输入 JSON 的 `thresholds` 覆盖。**先按默认值跑一遍，再用现场基线校准**——EP 规模很小、
窗口很短时，比值天然更抖。

> I2/I3/I4 在还没开 EPLB 时就能采，是"要不要开"的主证据；I1 只有已经开着 EPLB 时才有，用来判断
> "当前的副本摆放是否仍然不够"。

## 判定矩阵

| 结论 | 条件 | 该做什么 |
|---|---|---|
| `imbalanced` | ≥2 个信号，且失衡持续 | 交给 `vllm-eplb-optimization` 开 dynamic |
| `borderline` | 只有 1 个信号，或信号多但不持续 | 先做 force 上限探针，再决定 |
| `balanced` | 没有信号 | **不要开** EPLB |
| `inconclusive` | 可用指标 < 2，或 balancedness 窗口 < 3 | 补采证据 |

**M0 是门控，不是失衡信号**：它不参与 `verdict` 判定，但决定「失衡成立后能不能真的加副本」。
显存没有余量时，`verdict` 仍然是 `imbalanced`，动作会变成 `free_memory_first`。

`borderline` 为什么用 force 探针：force EPLB 把不均衡这件事从测量里拿掉，直接告诉你"完全均衡时能快
多少"。若 force 与 off 几乎一样，说明瓶颈不在专家摆放，再开 dynamic 也大概率是负收益；若 force 明显
更快，失衡就是真瓶颈，才值得上线 dynamic。

## 第一步：采证据

**指标（任何状态都能采）**

```bash
# 覆盖一个完整业务周期：每 10s 采一次，连续 6 次以上
for i in $(seq 1 6); do
  curl -s http://127.0.0.1:8000/metrics > metrics_$i.txt
  sleep 10
done
```
单个快照只能看瞬时倾斜。每一份都跑一次分析，结论一致才算稳定；只在某一份里出现的倾斜很可能是采样抖动。

**balancedness（仅当服务已开 EPLB）**

```bash
grep "EPLB step:" log/prefill_*.log | tail -n 20
```

若还没开 EPLB 但想拿到它，只能在模板里临时加 `--eplb-config.log_balancedness true`（需要
`--enable-eplb`）——这本身就会改变运行状态，所以更适合在"已经上了 dynamic、想复核效果"时用，
不适合拿来做上线前的唯一依据。

**压测侧（可选，用于交叉验证）**

`benchmark_serving.py --save-result --result-dir ...` 的逐请求 TTFT 分布，可以替代 I3 的指标口径；
两者口径不同，不要混算，各看各的结论是否一致即可。

## 显存余量（M0）

EPLB 的收益来自冗余专家副本，而副本占的是 **KV cache 的空间**。需要注意的是：vLLM 启动后会把显存
基本分配完，设备剩余显存 通常接近 0，所以"剩余容量"不能只看 free，要看这三件事：

1. **KV cache 占用**：`vllm:kv_cache_usage_perc`（0~1，按 engine）。长期 >85% 时加副本会先挤掉可用
   KV，长上下文请求最先受害。
2. **是否已经在换出**：`vllm:num_preemptions` 只要在增长，说明 KV 已经不够用，此时加副本是火上浇油。
3. **每个副本的成本**：官方公式
   `NUM_MOE_LAYERS × BYTES_PER_EXPERT × (NUM_TOTAL_EXPERTS + NUM_REDUNDANT_EXPERTS) ÷ NUM_EP_RANKS`，
   DeepSeekV3 每多一个冗余专家（每 EP rank）约 2.4 GB。用它能算出这点余量最多放几个副本。

把这几项写进证据 JSON：

```json
{
  "kv_cache_usage_pct": 88.0,
  "preemptions": 0,
  "free_mem_gib": 3.0,
  "bytes_per_expert_gib": 2.4,
  "ep_size": 16
}
```

脚本据此给出 `memory.verdict`（`ample` / `tight` / `no_headroom` / `unknown`）与
`affordable_redundant`（这点余量最多能放多少个冗余专家），并在 `imbalanced` 时决定是直接交接
`dynamic`、按余量收敛副本数，还是先释放显存。

> 没有提供显存信息时是 `unknown`：不会阻止结论，但交接里会提示上线前先确认余量。

## 第二步：跑分析

```bash
# 原始输入，脚本自己解析
python3 scripts/analyze_eplb_metrics.py \
    --eplb-log log/prefill_0.log --prometheus metrics.txt --out verdict.json

# 或先归一化成证据 JSON 再分析（便于复现与归档）
python3 scripts/analyze_eplb_metrics.py --evidence evidence.json --out verdict.json
```

证据 JSON 的最小形态：

```json
{
  "service": "dsv41-prefill",
  "window": "2026-11-19T09:00Z / 10min / 4 windows",
  "balancedness": [0.78, 0.81, 0.76, 0.79],
  "rank_waiting": [22, 3, 4, 2, 3, 5, 4, 3],
  "rank_running": [8, 8, 8, 8, 8, 8, 8, 8],
  "rank_ttft_ms": [2380, 910, 940, 880, 960, 920, 900, 930],
  "thresholds": {"queue_ratio": 1.5}
}
```

`rank_*` 数组按 engine 顺序给；有 `rank_prompt_tokens` 就多一条 I4；有 `rank_util_pct` 时脚本会额外
提示"各 rank 都打满且差异很小"这种非专家瓶颈。

## 第三步：按模板出报告

```markdown
# EPLB 不均衡分析 — <service> — <time window>

## 结论
<imbalanced | borderline | balanced | inconclusive> — 一句话理由

## 证据
| 指标 | 观测值 | 阈值 | 是否命中 |
|---|---|---|---|
| I1 balancedness | 0.785 (4 窗口, 持续 100%) | < 0.90 | 是 |
| I2 在途请求倾斜 | 2.61× | ≥ 1.5 | 是 |
| I3 TTFT 倾斜 | 2.57× | ≥ 1.3 | 是 |
| I4 prompt 倾斜 | 未采集 | — | — |
| M0 显存余量 | KV 43% / 无换出 / ample | 占用 <85% 且无换出 | 通过 |

## 判断
为什么这些信号足以/不足以判定失衡；有哪些替代解释（排队、KV、通信、采样太短）。

## 建议
<do_not_enable / probe_ceiling_first / optimize_dynamic(参数) / collect_more>
下一步命令与验证口径。
```

脚本输出的 `verdict.json` 就是这个报告的事实底稿，结论与证据要能对上；不要在报告里补充脚本没观测到的数字。

## 第四步：交接给优化技能

结论为 `imbalanced` 时，按 `recommendation.handoff` 执行：

```bash
python3 <skill>/scripts/apply_eplb.py --template <P节点 run_dp_template.sh> \
    --mode dynamic --redundant-experts <N> --window-size <W> --step-interval <S>
```

结论为 `borderline` 时，先跑上限探针（只测量，不上线）：

```bash
python3 <skill>/scripts/apply_eplb.py --template <P节点模板> --mode force
# 压测对比 off vs force；差距明显再切 dynamic，测完还原 off
```

结论为 `balanced` 时，不要动配置。把"为什么不该开"写进报告——这同样是有价值的交付。

显存相关的分支：

- `recommendation.memory.verdict == "no_headroom"` → 不要加副本，先降 KV 压力（`max-model-len`、并发、
  上下文长度），或按 `affordable_redundant` 把副本数降到显存能容纳的量。
- `optimize_with_cap` → 按 `handoff.redundant_experts` 执行；这个数值已经按余量压过，不要擅自加回去。


## 常见误判

| 现象 | 实际原因 |
|---|---|
| balancedness 很低，但 TTFT 很均匀 | 专家 token 数倾斜被并行/流水掩盖；先查 rank 级延迟再决定 |
| TTFT 长尾，但 balancedness 很高 | 瓶颈在排队、KV cache、通信或个别请求长度，不是专家摆放 |
| 只在压测前 1~2 个窗口看到失衡 | 冷启动/预热，按 I5 持续性要求会自动降级为 borderline 或 balanced |
| 混合在线流量下测不出收益 | 流量本身接近均衡；验证要用不均衡数据集（见优化技能的验证章节） |
| 所有 rank 利用率都 95%+ 且差异很小 | 整体算力打满，加副本不会变快 |

## 参考资料

- `references/eplb-metrics.md`：指标定义、日志/metrics 字段来源（附 vLLM 源码位置）、阈值取值理由与校准方法
- 机制原理（为什么冗余副本能削峰、放置目标为什么看逐 step 峰值）：同仓安装时见
  `../vllm-eplb-optimization/references/eplb-algorithm.md`；单独安装本技能时，向用户索取该参考文件或直接按上文判定规则执行。
