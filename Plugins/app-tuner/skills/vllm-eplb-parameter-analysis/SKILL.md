---
name: vllm-eplb-parameter-analysis
description: 把 vLLM MoE 的 EPLB 诊断结论收敛成一套确定、可解释的参数方案（模式、ep_size、num_redundant_experts、window_size、step_interval、显存预算）。当用户问"EPLB 参数怎么定""冗余专家给几个""num_redundant_experts 多少合适""window_size/step_interval 取多少""ep_size 必须是整数倍吗""显存放不放得下副本""拿到 vllm-eplb-analysis 的结论后怎么落参数"时，使用本技能。只做参数决策，不改模板、不跑压测、不下最终收益结论。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: vllm_moe_eplb
  activation_mode: active
  side_effect_free: "true"
  version: "1.0.0"
---

# EPLB 参数分析（vLLM MoE）

本技能可**独立使用**：消费 `vllm-eplb-analysis` 的结论（若已有）与现场事实，产出一份
**可执行、可解释、已校验**的参数方案（`eplb_parameter_plan.json`）。它**不改任何文件**。
落盘可由现有 `vllm-eplb-optimization` 的 apply 脚本完成，或由人工按方案改模板；收益结论另做分析，
本技能不依赖任何尚未合入的技能。

```
vllm-eplb-analysis --verdict.json--> 本技能 --eplb_parameter_plan.json-->（落盘：现有 optimization 的 apply 脚本 / 人工）
```

## 输入契约（来自 vllm-eplb-analysis）

`verdict.json` 关键字段：

| 字段 | 用途 |
|---|---|
| `verdict` | `imbalanced / borderline / balanced / inconclusive` |
| `recommendation.action` | `optimize_dynamic / optimize_with_cap / free_memory_first / probe_ceiling_first / do_not_enable / collect_more` |
| `recommendation.handoff` | 建议起点：`mode / redundant_experts / window_size / step_interval / template_changes` |
| `recommendation.memory` | `verdict(ample/tight/no_headroom/unknown)`、`affordable_redundant` |

现场事实需要用户/环境补充：`ep_size`、`logical_experts`、单副本成本（`bytes_per_expert_gib` 或
`bytes_per_expert_per_layer_gib × num_moe_layers`）、`free_gib`、**压测计划步数**、**是否本站点分支**。
缺关键事实时不要猜，先问。

## 结论 → 参数方案（handoff 消费矩阵）

| analysis 结论 | 本技能产出的 `mode` | 说明 |
|---|---|---|
| `imbalanced` + `optimize_dynamic/optimize_with_cap`，`handoff.redundant_experts` 非空 | `dynamic` | 直接采用 handoff 的 N/W/S，再做整除/显存/窗口校验 |
| `imbalanced` + `optimize_with_cap`，`redundant_experts=null` | `dynamic`（待定） | 先补 `free_gib` + 单副本成本，按可负担上限定值；否则输出待测 |
| `imbalanced` + `free_memory_first` | `off` | 先释放/测量显存，不进入 dynamic |
| `borderline` | `force` | 先做上限探针（只测不上线） |
| `balanced` / `do_not_enable` | `off` | 不开，留档 |
| `inconclusive` / `collect_more` | `hold` | 先补证据 |

> 总门禁：**没有不均衡证据（`imbalanced`）不要产出 `dynamic`**。只凭"别人都开了"或单看一个
> balancedness 数字就上，是最常见的负收益来源。

## 参数取值规则

`scripts/plan_eplb_params.py` 把上面的矩阵与下列规则一次性算完；`scripts/recommend_redundancy.py`
用于单独核算某个候选值。

| 参数 | 规则 |
|---|---|
| `mode` | 见上表；`dynamic` 是唯一可上线形态，`force` 只做上限参照 |
| `ep_size` | `dynamic` 必填；`logical_experts` 必须能被它整除 |
| `num_redundant_experts` | 必须是 `ep_size` 的整数倍；非整倍**向上取整**并显式提示；0 不构成冗余副本（按 off 处理） |
| `window_size` / `step_interval` | 压测计划短于周期就永远不触发；`step_interval` 应落在压测步数内（脚本按计划步数收敛并告警） |
| 分支 | 上游 vLLM 不含 `DYNAMIC_EPLB`/`enable_force_eplb`，`dynamic/force` 会静默无效；非本站点分支直接判 `off` 并告警 |

### 冗余专家比例与显存

```
ratio            = num_redundant_experts / num_logical_experts
per_rank_extra   = num_redundant_experts / ep_size            （必须整除）
physical_per_rank= (num_logical_experts + num_redundant_experts) / ep_size
显存增量（每 rank）= per_rank_extra × 单副本成本
```

单副本成本按 `NUM_MOE_LAYERS × BYTES_PER_EXPERT` 估算——这是"每个 rank 多一个专家槽位"的成本，
等价于全局增加 `ep_size` 个冗余专家；DeepSeekV3 参考值约 **2.4 GiB**。每 rank 增量 =
`(redundant / ep_size) × 2.4 GiB`，**不要**再用官方公式的 `÷ EP` 版本，否则会少算 `ep_size` 倍。

经验带（不是硬规则，最终以 force 上限 + 实测收益为准）：

| 比例 | 含义 | 建议 |
|---|---|---|
| < 1% | 几乎没有多余副本，削峰能力接近 0 | 要么不开，要么至少给到 2%~5% |
| 2% ~ 12% | 常规区间 | 从这里起步 |
| 12% ~ 15% | 偏高，KV cache 被明显挤占 | 确认收益后再加 |
| > 15% | 搬运量与显存代价都大 | 只在热点极稳定时短期使用 |

## 怎么用

```bash
# 一条链路：verdict.json + 现场事实 → 参数方案
python3 <skill>/scripts/plan_eplb_params.py \
    --verdict verdict.json --branch site-fork \
    --ep-size 16 --logical-experts 384 \
    --bytes-per-expert-gib 2.4 --free-gib 6 \
    --benchmark-steps 4000 \
    --out eplb_parameter_plan.json

# 只核算某个候选冗余数（比例、显存、可负担上限）
python3 <skill>/scripts/recommend_redundancy.py --logical-experts 384 --ep-size 16 \
    --redundant-experts 32 --bytes-per-expert-gib 2.4 --free-gib 3
```

校验门禁（不满足就不产出 `dynamic`）：
- `--branch` 必须显式给；`site-fork` 才允许 dynamic/force，`upstream` 判 off，未确认（默认 `unknown`）判 `hold`。
- `verdict` 与 `recommendation.action` 必须一致；不一致（如 verdict=balanced 但 action=optimize_dynamic）判 `hold`。
- dynamic 必须有显存证据（`--free-gib` + 单副本成本，或 analysis 的 `affordable_redundant`）；缺证据判 `hold`，不落未校验的副本数。
- 冗余数取整到 EP 倍数后仍要过显存预算；`affordable_redundant` 与 `max_affordable_redundant_experts` 都会参与收敛。

## 输出契约（供落盘/人工使用）

`eplb_parameter_plan.json`：

```json
{
  "schema_version": 1,
  "mode": "dynamic",
  "ep_size": 16,
  "redundant_experts": 16,
  "ratio_pct": 4.17,
  "per_rank_extra": 1,
  "memory": {"bytes_per_extra_expert_per_rank_gib": 2.4,
             "extra_per_rank_gib": 2.4, "fits": true,
             "max_affordable_redundant_experts": 32},
  "window_size": 1000,
  "step_interval": 1000,
  "rationale": ["..."],
  "warnings": ["..."],
  "template_changes": {"env": {}, "unset_env": [], "add_flags": [], "additional_config": {}}
}
```

`mode=hold` 或 `off` 时 `template_changes` 为空；下游据此不落盘或落 off。

## 边界

- **不写文件、不跑压测、不下收益结论**——落盘与实测由部署环节（现有 optimization 的 apply 脚本或人工）负责，
  收益结论另行分析。
- handoff 给的 N/W/S 是起点，不是结论；本技能负责按整除/显存/压测时长/分支把它变成可执行的确定值。
- 不给证据就要 `dynamic` 的诉求，先回 `vllm-eplb-analysis` 收证据。

## 排查

| 现象 | 处理 |
|---|---|
| `logical_experts 不能被 ep_size 整除` | EP 配置或逻辑专家数不对，先确认再定参数 |
| `num_redundant_experts` 非整倍 | 脚本向上取整并告警；确认取整后的实际值 |
| `显存不足` | 按 `max_affordable_redundant_experts` 收敛，或先降 KV 压力（max-model-len/并发/上下文） |
| `dynamic 无收益风险` | 检查 `window_size/step_interval` 是否大于压测步数；必要时按脚本收敛 |
| 上游分支 | `DYNAMIC_EPLB` 无效，判 off；force 也测不出上限 |

## 参考资料

- `references/eplb-algorithm.md`：机制层说明（副本分配、跨 rank 放置目标、权重搬运、窗口与周期、
  参数映射），不涉及任何具体实现的源码或内部路径。
