# 张量名 → 桶 的完整规则表

`analyze_feasibility.py` 的 `classify_tensor()` 按以下顺序匹配（先命中先归类），例外与理由在后。

## 识别顺序（伪代码）

```text
1. 名字含 shared_expert               → tp     (共享专家按 dense 走)
2. 名字含 experts. / experts_         → ep     (路由专家，含 MTP 层里的专家)
3. 含 e_score_correction_bias         → rep
4. 匹配 mlp.gate.weight$（非 gate_proj）→ rep    (router)
5. 含 q_a_proj / kv_a_proj            → rep    (MLA 降维投影)
6. 含 norm / 以 .bias 结尾            → rep
7. 其余                               → tp     (attention 主干 / embedding / lm_head / dense MLP，含 MTP 层主干)
```

**MTP 层（层号 ≥ `num_hidden_layers`，如 `model.layers.78.*` 当层数=78）不做特殊处理**：名字和普通层完全一样，按上面同样的规则走（attention→tp、experts→ep、norm→rep）。整层按「全卡复制」算会每卡凭空多算十几 GiB。

## 桶语义与除法

| 桶 | 每卡公式 | 典型张量 |
| --- | --- | --- |
| rep 全卡复制 | ×1（不随卡数变） | `*norm*`、`*.bias`、`mlp.gate.weight`（router）、`e_score_correction_bias`、`q_a_proj`、`kv_a_proj_with_mqa` |
| tp TP 切分 | ÷tp | `q_b_proj`、`kv_b_proj`、`o_proj`、MHA `qkv_proj`、`embed_tokens`（vocab 并行）、`lm_head`、dense `gate_proj/up_proj/down_proj`、`shared_expert`（含 MTP 层主干） |
| ep EP 切分 | ÷ep_group（EP 关闭时 ÷tp） | `mlp.experts.<n>.gate_proj/up_proj/down_proj`、`experts.experts_<n>.*`、专家量化 scale（含 MTP 层专家） |
| extra 额外 | ×1 每卡复制（保守） | **只收** `--weights-json` 手工 extra_gib 与日志校准回灌的未归类量（MTP 已走正常分桶，不进此桶） |

## 为什么要这样分

- **router / e_score_correction_bias 是标量门控**：每个 token 都要算全专家 logits，必须每卡一份；随 TP 变小它不缩。
- **MLA 降维投影（q_a/kv_a）作用在 latent 低维空间**：输入输出维度都很小（如 7168→2112），vLLM 按复制处理；真正大头 `kv_b_proj`（升维）才是 TP 切分。
- **embedding/lm_head 是 vocab 并行**：按词表切，每卡 1/tp。
- **EP 专家**：vLLM `--enable-expert-parallel` 下按 EP 世界大小（= tp × dp_per_group）均分，TP 变小不改变专家每卡量——这正是「TP 切小只涨零头」的来源。

## 例外与已知偏差

| 情况 | 行为 | 处理 |
| --- | --- | --- |
| MTP 层按普通层分桶 | attention→tp、experts→ep，与 vLLM 实际分片一致 | ✓ 正确行为；别改成整层复制（每卡虚增十几 GiB） |
| 量化 scale 命名跟随父张量（`weight_scale_inv` 等） | 随父张量进同桶 ✓ | 无需处理 |
| 独立命名的 MTP 模块（非 `layers.<n>` 序号） | 落入默认 tp 桶 → 差额由日志校准兜底 | 看「校准差额」行 |
| GQA/MHA 模型 | 无 `kv_lora_rank` → KV 随 TP 切（每卡 bytes/token ∝ 1/tp），报告结论与 MLA 相反 | 脚本自动判定，别套错 |
| EP 未启用 | MoE 专家按 TP 切 | 脚本自动把 EP 桶改除 tp |
| `Model weights take` 与计算差 >5% | 差额回灌 extra 桶并单列 | 用 `--overrides` 修正，不要硬信计算值 |

## 校准的意义

自动分桶是启发式。每卡权重的「计算值 vs 日志值」差 >5% 时，差额几乎一定是漏桶（embedding 副本、dtype 转换、量化 scale、独立命名的 MTP 模块）。报告第 3 节会单列「校准差额」——看到它大，先修正分桶再信候选 TP 的数字。
