# 无栈 trace：源码辅助的近似调用栈与 Free 归因

## 0. 前提

开始分析前，先主动向用户确认：模型名称、vllm 版本、vllm-ascend 版本、源码路径、同模型/版本/场景的 with-stack trace。用户不提供时先从 trace/环境自动探测；仍缺失才降级：只保证版本无关结论，模型结构/阶段/函数名标为默认 0.23.0 参考、未校准，`mapping_confidence=low`。

无栈 trace 没有 `python_function`，所以不存在真实 Python 调用栈。本方法用：

- trace 的设备内核时序
- host 侧 `cpu_op` / CANN API / enqueue/dequeue
- 同目录侧车（`kernel_details.csv`、`step_trace_time.csv`、`communication.json` 等）
- 用户提供的模型名与 vllm / vllm-ascend 版本
- 对应版本的 vllm / vllm-ascend 源码

近似重建 **P1/P2/P3、模型结构、调用栈层级和 Free 去向**。输出是 **operator/phase 级近似**，不是真实 Python 栈。

## 1. 先固定输入

使用前先确认：

- 模型：如 `Qwen3-VL-8B-Instruct`、`Mixtral-8x7B-v0.1`、`Qwen3.8-27B`、`DeepSeek-V4`
- vllm / vllm-ascend 版本：如 `0.23.0` / `0.23.0rc1`
- trace 场景：eager / FX eager / 整图回放 / 是否有 MTP/DSpark
- 可选：模型 `config.json`，用于核对 `num_hidden_layers`、专家数、注意力头数

版本和源码路径必须匹配；跨版本时行号、自定义 op 名、patch 包装层都可能漂移。

## 2. 从源码建立“算子 → 源码函数”映射

先看 trace 里实际出现了哪些 host 算子/设备内核：

```bash
python3 scripts/detect_steps.py <trace_view.json> --json
```

然后在 vllm / vllm-ascend 源码里搜索同名或近似名：

```bash
grep -rn "unquantized_gemm" vllm_ascend
grep -rn "unified_attention_with_output" vllm_ascend
grep -rn "triton_split_qkv_rmsnorm_mrope" vllm_ascend
grep -rn "FusedGdnGating\|RecurrentGatedDeltaRule\|ChunkGatedDeltaRule" vllm_ascend
grep -rn "MoeGatingTopK\|GroupedMatmul\|MoeTokenUnpermute" vllm_ascend
grep -rn "propose_draft\|_run_merged_draft\|qwen3_5_mtp\|dspark" vllm_ascend vllm
```

vllm 0.23.0 / vllm-ascend 0.23.0rc1 常见映射：

| trace 中名字 | 源码函数/模块 | 归属阶段 |
|---|---|---|
| `vllm::unquantized_gemm`、`MatMulV2` | vllm-ascend linear / unquantized gemm | P2 模型前向 |
| `vllm::unified_attention_with_output`、`FusedInferAttentionScore` | `attention_v1.py: forward_fused_infer_attention` | P2 Attention 层 |
| `vllm::triton_split_qkv_rmsnorm_mrope`、`split_qkv_rmsnorm_mrope_kernel` | vllm-ascend triton QKV/norm/RoPE | P2 QKV 前缀 |
| `atb::_npu_reshape_and_cache`、`ReshapeAndCacheNdKernel` | `attention_v1.py: reshape_and_cache` | P2 KV 写 |
| `aclnnFusedGdnGating`、`RecurrentGatedDeltaRule`、`ChunkGatedDeltaRule`、`CausalConv1d` | vllm-ascend GDN 实现 | P2 GDN 层 |
| `MoeGatingTopK`、`MoeInitRoutingCustom`、`GroupedMatmul`、`MoeTokenUnpermute` | vllm-ascend MoE 实现 | P2 MoE 层 |
| `HcclAllreduce`、`hcom_allReduce`、`vllm::all_reduce` | TP all_reduce | P2 通信 |
| `vllm::all_gather`、`HcclAllGather` | logits/vocab all_gather | P3 compute_logits |
| `aclnnApplyTopKTopP`、`Softmax`、`ArgMax`、`Sort` | vllm-ascend sampler | P3 采样 |
| `propose_draft_token_ids`、`_run_merged_draft`、`qwen3_5_mtp.py: forward` | MTP draft | P3 外置模型 |
| `dspark` / 稀疏/压缩内核（`compress`、`decompress`、`kv_lora`、`lightning`、`nsa`） | DeepSeek/稀疏模型实现 | 外置或压缩层 |

新模型/新版本出现未识别算子时，扩展 `nostack_analysis.py` 的分类提示，而不是把未知算子硬塞进已有类别。

## 3. 先切 step，再切阶段

### 3.1 step 边界

`detect_steps.py` 按精度降级：

1. `STEP_TIME`（`ascend_pytorch_profiler_*.db`）
2. `kernel_details.csv` 的 `Step Id`
3. `step_trace_time.csv` / `analysis.db:StepTraceTime`
4. `communication.json` / `analysis.db:CommAnalyzerTime`
5. 仅 trace：`aclrtSynchronizeDevice*` 结束 + host 首个事件

只有第 5 级时，step0 起点只能取“首个可观测 host 事件”，会有毫秒级误差。

### 3.2 P1/P2/P3

- **P1 输入准备**：step 起点 → 首个 forward 内核
- **P2 前向**：首个 forward 内核 → 最后一个 logits MatMul
- **P3 compute_logits + 采样/收尾**：其余到 step 结束

无栈时用以下信号：

- forward 起点：首个 `MatMul` / `FusedInferAttentionScore` / `FusedGdnGating`
- P3 起点：采样前的 `all_gather` / 最后一个 logits MatMul / 首个采样内核
- 没有 MTP 的 eager：P3 通常很短；有 MTP 的模型 P3 会包含多轮 draft

## 4. 模型结构与近似调用栈

### 4.1 层核心与层前缀

- **层核心**：`FusedGdnGating`、`FusedInferAttentionScore`、`MLA`、`Sparse`
- **GDN 层前缀**：首个 `CausalConv1d`
- **Attention 层前缀**：核心前最后一个 GEMM（QKV）
- 一层 = 前缀 → 核心 → O/MLP/MoE → Norm

### 4.2 层类型聚类

对每层提取算子签名（GDN/Attention/MoE/压缩/GEMM/Norm/Act 等计数），相同/近似签名聚为一类，输出：

- `Attention 层 ×N`
- `Attention 层（MoE）×N`
- `GDN 层 ×N`
- `Attention 层（压缩）×N`
- 同核心但算子不同的层追加差异，如 `GDN 层；无 IndexSelect_Slice ×N`

这样 DeepSeek V4 的不同压缩率层、混合 GDN/Attention 层不会被塞进同一个模板。

### 4.3 近似调用栈

报告里的“调用栈”是 **阶段 → 算子组** 的近似树，不是 Python 栈：

```text
P1 输入准备
  _update_states / input prep
  _prepare_inputs (slot mapping)
  _build_attention_metadata
P2 forward
  Attention/GDN 层 ×N
    QKV → Norm → Attention/GDN → O → MLP/MoE → Norm
  TP all_reduce / all_gather
P3 compute_logits / sampling / end_step
```

把看到的 host 算子/设备内核，按源码映射表挂到对应阶段和层型上；未识别算子保留短名，不强行归类。

### 4.4 外置模型 MTP/DSpark

有栈 trace 优先用 `python_function` 找外置模型窗口：

- `propose_draft_token_ids` / `_run_merged_draft` / `mtp` / `qwen3_5_mtp.py: forward`
- `dspark` / `eagle` / `medusa`

规则：

1. 取具体 forward / merged draft 窗口，不要取外层 `propose_draft` 的采样段
2. 连续轮次合并为一个窗口
3. 外置窗口从主模型结构中排除
4. 对窗口内设备内核复用同一套层核心/聚类逻辑，输出“单轮结构 + 轮数”

无栈时只能靠内核名中的 `mtp`/`dspark`/`draft` 线索做低置信度识别。

## 5. Free 归因

### 5.1 先算对 Free

不要直接使用 `step_trace_time.csv` 的 `Free`/`Stage`：它可能对应不同的 step 窗口。正确做法：

- 在实际 step 窗口内，用 Overlap Analysis 求：
  - `Computing` 并集
  - `Communication` 并集
  - `Free = step 时长 − (Computing ∪ Communication)`

`Computing + 未重叠 Communication + Free = 100%`。通信只作了解，不计入 Free。

### 5.2 每个 Free 区间归到哪

1. 判断 Free 落在 P1/P2/P3
2. 在该阶段内找最近设备内核（或最近高层 host 算子）
3. 用第 2 节的映射表归到源码函数
4. 按 step / 阶段汇总

例如：

```text
step0 Free 37.9ms
  P2 33.4ms
    triton_split_qkv_rmsnorm_mrope 22%
    unquantized_gemm/linear        21%
    forward 辅助(index/slice/cast) 18%
    reshape_and_cache              15%
    forward_fused_infer_attention  14%
    TP all_reduce/all_gather        5%
```

### 5.3 host 在干什么/阻塞到哪里

对每个 Free 区间看 host 侧是否还有事件：

- 有 `cpu_op`/CANN 事件：host 在持续下发或等待某个算子；结合最近算子判断是 forward 供数、input prep 还是 logits/通信等待
- 完全没有 host 事件：标记 host idle/blocked（可能是调度、Python 开销或等待外部输入）

这一步只能给出“host 在哪个阶段/哪类算子上”，不能给出具体 Python 函数。

## 6. 验证闭环（重要）

先独立完成无栈重建，再拿**同模型、同版本、同场景的 with-stack trace** 做对照：

1. **阶段窗口**：P1/P2/P3 起止误差
2. **模型结构**：层数、层型、算子序列是否与 with-stack 一致
3. **Free 去向**：Top 函数族是否一致，通信占比是否同量级
4. **外置模型**：MTP/DSpark 轮数、单轮结构是否合理

根据差异迭代：

- 阶段偏差大 → 调整 forward/P3 起点锚点
- 层数不对 → 检查层核心/前缀识别、模型 config
- 压缩/非压缩层没分开 → 扩展压缩类算子提示
- Free 归因偏差大 → 调整最近内核/阶段归属或 host 上下文

当前已验证样本：Qwen3-VL-8B（×36 Attention）、Mixtral-8x7B（MoE ×32）、Qwen3.8-27B（GDN/Attention 多簇 + MTP ×3 轮）。DeepSeek V4 尚无可用 trace，压缩层识别需用真实数据校准。

## 7. 输出置信度

- `exact`：step 边界来自 `STEP_TIME`
- `approx`：设备侧 step 边界 / trace sync / 阶段与模型结构近似
- `low`：只有 trace、无侧车，或外置模型仅靠内核名识别

报告必须写清：无 Python 栈；Free 归因是 operator/phase 级近似；模型结构是“本步实际执行的算子结构”，不是静态模型定义。
