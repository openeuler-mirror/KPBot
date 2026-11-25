# TP→DP 扩容可行性分析报告 — GLM-5.2-W4A8

- 生成: 2026-11-24 14:55:40 ｜ 工具: analyze_feasibility.py v0.2.0 ｜ 主机: prefill-node-01
- 权重来源: weights-json: glm52_weights.json
- 进程参数来源: script

## 1. 结论

| 项 | 值 |
| --- | --- |
| 结论 | ✅ 可行：TP 32→8，DP 5→20（×4.0），推荐 **TP8** |
| 每卡权重 | 22.00 → 22.90 GiB（+0.90，不是按 TP 倍涨）|
| KV 池/卡 | 27.70 → 26.80 GiB |
| 每 DP token 容量 | 330,959 → 320,204（-3.2%，≥ max_model_len=131,072）|
| 集群 KV 总量 | 1,654,795 → 6,404,088 token（×3.87）|

## 2. 运行态事实

| 项 | 值 | 来源 |
| --- | --- | --- |
| 集群总卡数 | 160 | 输入 --total-cards |
| EP 组大小 | 32 | 输入 --ep-group |
| 当前 TP/DP/PP | 32 / 5 / 1 | 实测（来源: script） |
| EP enabled | 是 | 实测(ps/script) |
| 单卡总显存 | 64.00 GiB | 实测 npu-smi（NPU_TOTAL_MB 别名） |
| 单卡已用 | 59.25 GiB | 实测 npu-smi（NPU_USED_MB 别名）（含全部占用）|
| gpu_memory_utilization | 0.9 | 实测(启动参数) |
| 单卡可用口径 | 54.00 GiB | 日志实测 Desired usage(total) |
| 激活 / 其他开销 | 2.5 / 1.8 GiB | 日志反推(合计) / 日志反推 Total−权重−KV |
| max_model_len | 131,072 | 实测 |
| 日志每卡权重 | 22.00 GiB | 日志实测 Model weights take / Actual usage for weights |
| 日志 KV 池/卡 | 27.70 GiB | 日志实测 Available KV cache / Actual usage for kv cache |
| 日志 Desired 总预算 | 54.00 GiB | 日志实测 Desired usage(total) |
| 日志 Actual 总占用 | 54.00 GiB | 日志实测 Total usage |
| 日志 Non-Torch | — GiB | 缺失 |
| 日志 torch peak | — GiB | 缺失 |
| 日志 KV token 数 | 331,000 | 日志实测 GPU/NPU KV cache size |
| KV cache dtype | auto | 实测=auto（按模型精度 bf16 2B 计） |
| 每 token 每卡 KV | 89,857 B | 日志推算 = KV池(日志)÷token数（日志实测 GPU/NPU KV cache size） |
| MLA(每卡复制 KV) | True | config kv_lora_rank / weights-json |
| 模型目录 | /data/models/GLM-5.2-W4A8 | 实测 |

## 3. 权重三分类拆解

| 桶 | 总量 GiB | 每卡@TP32 GiB | 占比 | 识别规则 |
| --- | --- | --- | --- | --- |
| 全卡复制 | 3.98 | 3.98 | 1.0% | norm/bias、router gate、e_score_correction_bias、MLA 降维投影(q_a/kv_a_proj) |
| TP 切分 | 9.60 | 0.30 | 2.5% | attention 主干(q_b/kv_b/o/qkv)、embedding/lm_head、dense MLP、shared_expert（含 MTP 层主干） |
| EP 切分 | 362.35 | 11.32 | 94.8% | 路由专家 experts.<n>.*（W4A8 打包字节，含 MTP 层专家） |
| 额外(未归类,每卡复制) | 6.40 | 6.40 | 1.7% | MTP 层按普通层规则分桶，不进此桶；此桶只收 --weights-json 手工值与日志校准回灌的未归类量（每卡复制，保守口径） |
| **每卡合计 [计算]** | — | **22.00** | 100% | 复制+额外+TP桶/tp+EP桶/ep_group |
| 日志实测每卡 | — | 22.00 | | Model weights take |
| 校准差额 | — | -0.00 | | 绝对差≤5%，未回灌 |

## 4. 候选 TP 试算表

| TP | 组内DP(ep_group/tp) | 集群DP(可容纳) | 每卡权重 GiB | KV池/卡 GiB | 每DP容量 token | 可行性 |
| --- | --- | --- | --- | --- | --- | --- |
| 32（当前） | 1 | 5 | 22.00 | 27.70 | 330,959 | —（当前） |
| 16 | 2 | 10 | 22.30 | 27.40 | 327,374 | ✅ 可行 |
| 8 | 4 | 20 | 22.90 | 26.80 | 320,204 | ✅ 可行 |

判定口径：KV池/卡 > 0 且 每DP容量 ≥ max_model_len = 131,072（缺 max_model_len 时候选一律 unknown，不给 ok）。
「集群DP(可容纳)」= 集群总卡数 ÷ (TP×PP) 的容量口径；「当前 DP」基线一律取 facts 实测 DP_SIZE（见第 2 节），不用容量反推值。
每 DP token 容量来源：日志推算 = KV池(日志)÷token数（日志实测 GPU/NPU KV cache size）。

## 5. 单卡容量校验（分项相加 vs 可用）

| TP | 权重 | 激活 | 其他 | KV池 | 合计 | 可用 | 差 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 32（当前） | 22.00 | 2.5 | 1.8 | 27.70 | 54.00 | 54.00 | 0.00 |
| 16 | 22.30 | 2.5 | 1.8 | 27.40 | 54.00 | 54.00 | 0.00 |
| 8 | 22.90 | 2.5 | 1.8 | 26.80 | 54.00 | 54.00 | 0.00 |

## 6. DP 扩容收益

| TP | 每DP卡数 | 集群DP(可容纳) | 组内DP | 每DP容量 token | 集群KV总量 token | vs当前 |
| --- | --- | --- | --- | --- | --- | --- |
| 32（当前） | 32 | 5 | 1 | 330,959 | 1,654,795 | 1.00（实测 DP_SIZE） |
| 16 | 16 | 10 | 2 | 327,374 | 3,273,741 | ×2.00 |
| 8 | 8 | 20 | 4 | 320,204 | 6,404,088 | ×4.00 |

MLA：KV 不随 TP 切分，每 DP 容量≈每卡 KV 池；集群 KV 总量随 DP 数近似线性增长。

## 7. 风险与前提

- **每 DP KV 容量 -3.2%**：单 DP 能并发的满长请求数下降；max_model_len 边界余量见第 4 节。
- **带宽/通信**：DP×↑ 后 EP all-to-all 与 DP 广播的通信对数增加，跨机带宽是否成为新瓶颈需压测验证（带宽/负载指标另行采集，本 skill 不覆盖）。
- **PD 分离**：P/D 两侧 max_model_len 与 KV 需求不同，需分别跑本测算再合并结论；D 侧另有 KV 接收 buffer。
- **KV 传输**：Mooncake/AscendStore 的 tp/dp 必须与新拓扑严格一致（错配表现为 KV 路由错乱/丢 KV）；这是部署变更的一部分，由部署流程统一处理，本 skill 只读不部署。
- **碎片与保留**：CANN/HCCL buffer、大块分配碎片未计入（保留口径 3.6 GiB 为假设值）；实际 KV 池以启动日志为准。
- **本测算为静态容量口径**：不预测吞吐/时延变化，性能结论需上板压测。

## 8. 建议启动参数 diff（仅文本，不执行）

```diff
  # 当前 → 建议（TP 32 → 8）
- --tensor-parallel-size 32
+ --tensor-parallel-size 8
- --data-parallel-size 5          # 现状 = 实测 DP_SIZE（单实例口径）
+ --data-parallel-size 4          # 组内 DP = ep_group 32 ÷ TP8（单组启动命令）
  # 集群布局: 160 卡 ÷ 32/组 = 5 组 × 组内 4 DP = 集群 20 DP
  --enable-expert-parallel          # 保持；EP 组仍为 32 = 8×4
  # 其余参数沿用；卡分配/端口/分流等部署细节由部署侧统一安排（本 skill 只读不部署）
```

**执行前须用户确认；本 skill 只读，不改动任何线上配置。**

## 9. 假设与未测项

- gpu_memory_utilization = 0.9（实测(启动参数)）
- 进程参数来源 = script
- 单卡可用口径：日志实测 Desired usage(total)
- 单卡保留 3.6 GiB（假设默认 3.6 GiB（驱动/CANN/HCCL 保留））
- 激活 2.5 GiB、其他 1.8 GiB（日志反推(合计) / 日志反推 Total−权重−KV）
- MLA = True（config kv_lora_rank / weights-json）——MHA 模型 KV 随 TP 切，结论会不同
- 每 token KV 89,857 B（日志推算 = KV池(日志)÷token数（日志实测 GPU/NPU KV cache size）；config 公式未含 DSA indexer 等附加缓存，实测反推优先）
- 未测项：跨机带宽、EP all-to-all 时延、KV 传输吞吐、碎片率、PD 分离 D 侧 buffer。

---

*本报告由 analyze_feasibility.py v0.2.0 生成；数字来源标记：实测 / 日志推算 / 计算 / 假设 / 输入。*
