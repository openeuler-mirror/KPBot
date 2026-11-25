# 显存预算口径与 KV 公式

报告第 2/4/5 节所有数字的口径定义。**每卡视角**（TP 组内每张卡各自要装的东西），不是全集群视角。

## 日志行格式（双格式都要认）

| 值 | vLLM 标准格式 | vLLM-Ascend 格式 |
| --- | --- | --- |
| 每卡权重 | `Model weights take X GiB` | `Actual usage: X GiB for weights` |
| KV 池 | `Available KV cache memory: X GiB` | `Actual usage: X GiB for kv cache` |
| 可用总预算 | —（无对应行） | `Desired usage: X GiB for total` |
| 实际总占用 | —（无对应行） | `Total usage: X GiB` |
| KV token 数 | `GPU KV cache size: N tokens` | `NPU KV cache size: N tokens` |

`Model weights take` / `Actual usage ... for weights` 都是 **device profiling 的每卡值**（拿它和「总权重」比会差 tp 倍）。

## 单卡可用（usable）的取值优先级

```text
优先级1（日志实测）  : usable = 日志 Desired usage(total)          ← vLLM 自己的预算，最可信
优先级2（日志推算）  : usable = 日志权重 + 日志KV + 激活 + 其他
优先级3（推算兜底）  : usable = npu-smi总显存 × gpu_memory_utilization − 保留(默认 3.6 GiB)
```

优先级 3 的口径是「64 GiB×util」这种总量推算，比 vLLM 实际可用可能**多 2-3 GiB**（驱动/CANN/HCCL 保留不止默认值时），只做兜底，报告会标注降级。单卡总显存读 facts 的 `NPU_TOTAL_MB` 别名（与采集卡号无关）或任意 `NPU<id>_TOTAL_MB`（多卡键取最小值代表单卡规格，并注明取自第几号卡）；覆盖 `utilization`/`reserved_gib` 后该分支会按覆盖值重算。

## 非 KV 开销（激活+其他）

**优先实测反推，不要拍默认值**：

```text
开销 = 日志 Total usage − 日志权重 − 日志KV      （实测反推）
拆分 : 激活取假设值(≤反推值)，余量归"其他"
兜底 : 激活 2.5 GiB + 其他 1.8 GiB（缺 Total 行时）
```

`npu-smi` 的「已用」包含一切，只能当现状参考，不能当 vLLM 预算用。老卡只有 `HBM Usage Rate(%)` 时（无 MB 值），已用=总显存×rate 推算，可用口径走日志。

## 每卡权重

```text
每卡权重(tp) = 复制桶 + 额外桶(未归类,每卡复制) + TP桶/tp + EP桶/ep_group   （EP 关闭时 EP桶/tp）
```

W4A8 的 safetensors nbytes 就是打包后真实字节，直接读 header，不要按 bf16 反推总量。MTP 层与普通层同规则分桶，不整层算复制。

## KV 公式

### MLA（`kv_lora_rank` 存在，GLM/DeepSeek 系）

```text
每 token 每卡 KV 字节 = (kv_lora_rank + qk_rope_head_dim) × dtype字节 × 本卡层数    （与 TP 无关）
                      （PP>1 时 本卡层数 = ceil(num_hidden_layers / PP)；缺层数 → 容量标未知）
每 DP token 容量     = KV池/卡 ÷ 每token每卡KV字节
集群 KV 总量         = 集群DP数 × 每DP容量        （随 DP 数近似线性增长）
```

MLA 的 latent KV **每卡复制**：TP↓ 不减小每卡 KV；DP↑ 让集群 KV 总量按 DP 数增长——这是 TP 切小的另一重收益。

### MHA/GQA

```text
每 token 每卡 KV 字节(tp) = 2 × kv_heads × head_dim × dtype字节 × 本卡层数 ÷ tp    （随 TP 切）
                            （缺 num_hidden_layers → 容量标未知，不按单层算——否则会低估数十倍）
每 DP token 容量          = KV池/卡 × tp ÷ 每token每卡KV字节(tp)
```

MHA 的 KV 随 TP 切分，TP 切小后每卡每 token KV 变大、每 DP 组总容量维持——结论与 MLA 完全不同，报告会自动按 config 判定。

### 每 token 字节的来源（优先级）

1. 日志推算：`KV池(日志) ÷ GPU/NPU KV cache size tokens`（最可信）
2. 日志推算：`Maximum concurrency × max_model_len`（vLLM 的并发反推；并发数保留小数，如 2.50x → 2.50）
3. `--overrides {"kv_bytes_per_token": N}` 手工给
4. config 公式兜底（按 facts `KV_CACHE_DTYPE` 实测值计字节：fp8 系=1B，auto/缺失按 bf16=2B 并标注假设）——**注意会漏 DSA indexer 等附加缓存，偏低约 8%**，报告会标注「实测口径优先」

## 可行性判定

```text
KV池/卡 > 0                    （显存装得下权重）
且 每DP token容量 ≥ max_model_len（至少放得下 1 条满长请求，否则业务必挂）
且 ep_group % tp == 0           （拓扑整除）
```

容量余量建议留 ≥ 2× max_model_len（并发 2 条满长请求）再谈吞吐。

## PD 分离注意

P（Prefill）与 D（Decode）的 `max_model_len`、KV 生命周期、激活形态都不同：

- 分别用各自日志跑两次本测算，再合并结论
- D 侧还有 KV 接收 buffer（Mooncake/AscendStore），需额外计入「其他」项
- PD 两侧 TP 拓扑可以不同（常见：P 侧 TP8，D 侧 tp=1），但 Mooncake `tp/dp` 必须与 P 侧实际拓扑一致

## facts 键契约（采集端 ↔ 分析端对齐表）

两份脚本共同维护的键归属，新增键必须两侧同步登记（防「采而不读/读而不采」）：

| 键 | 采集端 | 分析端 |
| --- | --- | --- |
| `HOST_NAME` / `COLLECT_TIME` | 产出 | 仅报告头部展示 |
| `NPU_TOTAL_MB` / `NPU_USED_MB` / `NPU_HBM_RATE_PCT` | 产出（卡号无关别名） | **消费**（单卡显存口径，别名优先） |
| `NPU<id>_TOTAL_MB` / `NPU<id>_USED_MB` / `NPU<id>_HBM_RATE_PCT` | 产出（按 `--npu` 卡号） | **消费**（无别名时按前缀扫描，取最小规格并注明卡号） |
| `NPU_LOCAL_COUNT` | 产出 | 仅采集参考（宿主机可见卡数） |
| `PARAM_SOURCE` | 产出（script/ps/none） | **消费**（ps 时报告告警） |
| `VLLM_ARGS` | 产出（白名单脱敏，不含 api-key 等） | 仅报告参考，不参与计算 |
| `TP_SIZE` / `DP_SIZE` / `PP_SIZE` / `EP_ENABLED` | 产出 | **消费**（拓扑与当前基线；值可带成对引号，两侧都剥） |
| `EP_GROUP_SIZE` | 产出（EP 启用且 TP/DP 可解析时 = 实例 TP×DP，否则输出空键） | **消费**（EP 组优先级第 2 位） |
| `GPU_MEMORY_UTILIZATION` / `MAX_MODEL_LEN` / `KV_CACHE_DTYPE` | 产出 | **消费**（util 口径 / 满长校验 / KV 字节数） |
| `MODEL_DIR` / `CONFIG_EXISTS` / `CONFIG_<字段>` | 产出 | `MODEL_DIR`/`CONFIG_EXISTS` 报告展示；`CONFIG_*` 仅采集参考（分析端读模型目录的 config.json） |
| `SAFETENSORS_COUNT` | 产出 | **消费**（与本地分片读取数交叉核对，不符→权重不完整；模型自带 `model.safetensors.index.json` 时索引 weight_map 也作为独立清单核对，不依赖 facts） |
| `LOG_MODEL_WEIGHTS_GIB` / `LOG_AVAILABLE_KV_GIB` / `LOG_DESIRED_TOTAL_GIB` / `LOG_ACTUAL_TOTAL_GIB` / `LOG_KV_TOKENS` / `LOG_MAX_CONCURRENCY` | 产出（数值取自指标短语之后，防时间戳污染） | **消费**（可用口径优先级 / 校准 / token 容量） |
| `LOG_NON_TORCH_GIB` / `LOG_TORCH_PEAK_GIB` | 产出 | **消费**（Non-Torch 在 Total 行缺失时兜底「其他」开销；两者均入报告第 2 节） |
| `RESERVED_GIB` / `ACTIVATION_GIB` / `OTHER_GIB` | 不产出 | 手工可补键（缺省走假设默认值并用 `--overrides` 同名覆盖） |

## 碎片与未计项

HCCL 通信 buffer、CANN 上下文、大块分配碎片、静态图 workspace 都未精确计入。实际 KV 池以启动日志为准；本测算的定位是**拓扑决策的静态容量校验**，不是逐字节对账。
