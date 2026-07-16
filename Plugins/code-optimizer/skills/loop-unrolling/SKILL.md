---
name: loop-unrolling
description: 对标量和 SIMD 代码执行循环展开优化，自行计算最优展开系数，提升流水线吞吐率。支持 C/C++ 和 ARM64 汇编（纯汇编 .s/.S 及内联 asm 块）。适用于 apply-optimization 调用。
---

# 循环展开优化

对标量或 SIMD 代码进行循环展开，充分利用鲲鹏流水线资源提升吞吐率。

用户调用了 `/loop-unrolling`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 50],
  "simd_type": "scalar|neon|sve|sme",
  "recommended_parallelism": 4,
  "language": "c_cpp|pure_asm|inline_asm",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>"
  }
}
```

- `simd_type`：当前代码类型（`scalar`/`neon`/`sve`/`sme`）
- `recommended_parallelism`：上游建议的目标并行度（参考值，本 skill 自行验证和调整）
- `language`：`c_cpp`（C/C++ 源码）/ `pure_asm`（纯汇编 .s/.S）/ `inline_asm`（C/C++ 中的 asm 块）

## 辅助脚本

```bash
# 微架构检测 → JSON（pipeline_width, vector_pipelines, vector_width_bits, l1_size_kb）
python3 scripts/detect_microarchitecture.py

# 展开系数计算（通用公式，作为交叉验证参考；Kunpeng 应优先用步骤 2 的资源模型）
python3 scripts/detect_microarchitecture.py | python3 scripts/calculate_unroll_factor.py

```

更多脚本说明见 `scripts/README.md`。

## Pipeline 指令查询契约

循环展开本身不需要查询 intrinsic；但如果展开后会新增、删除或改写 NEON/SVE intrinsic、inline asm 或汇编指令，必须按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 查询并记录 evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
```

重点确认 lane 数、类型形状、header、FEAT 依赖、SVE 是否保持 VL-agnostic，以及 NEON 展开后是否仍有显式尾处理。

## 执行步骤

### 步骤 0：检测代码类型并路由

检查 `source_file` 扩展名和内容：

| 条件 | language | 路径 |
|------|----------|------|
| `.s` 或 `.S` 扩展名 | `pure_asm` | 汇编展开路径 |
| `.c`/`.cpp`/`.h` 且含 `__asm__ volatile` / `asm volatile` | `inline_asm` | 汇编展开路径 |
| 其他 C/C++ 文件 | `c_cpp` | C/C++ 展开路径 |

- **汇编路径**：读取 `references/asm-unrolling.md`，执行 步骤 1A → 2 → 3A
- **C/C++ 路径**：读取 `references/cpp-unrolling.md`，执行 步骤 1B → 2 → 3B

### 步骤 2：计算最优展开系数

#### 2a. 获取微架构信息

优先从 `context.prepareProject` 中获取，若无则自行检测：

- **微架构文档**：Read `context.prepareProject.microarch_file` 获取执行端口数/FPU 吞吐/ROB 深度
- **指令性能数据**：按微架构查询关键指令的实际延迟/吞吐量，用于精确计算展开因子。
  - TSV110 (Kunpeng-0xd01)：`python3 skills/kunpeng_microarch/scripts/query_tsv110.py <指令名>`（207 条指令模式，注意：TSV110 数据不含 SIMD FMLA，需用 FMUL+FADD 组合延迟估算）
  - 0xd03 (Kunpeng-0xd03/0xd06)：`python3 skills/kunpeng_microarch/scripts/query_uarch_b.py <指令名>`（754 条助记符，含完整 ASIMD/SVE 指令）。例如 0xd03 上 FMLA=4c → 需展开至少 4 次才能隐藏延迟
- **降级方案**：
  ```bash
  lscpu | grep -E "Model name|CPU\(s\):|L1d cache|L1i cache|L2 cache|Flags"
  cat /proc/cpuinfo | grep -E "Features|model name" | head -20
  ```

#### 2b. 微架构资源模型

**SIMD 代码**（neon/sve/sme）— Kunpeng 向量流水线模型：

- SVE 256-bit 执行时占用 2 个 128-bit NEON 通道
- **2 条并发 SVE ≡ 4 条并发 NEON**（吞吐率等价）
- 统一用 **128-bit 等价通道数**衡量：每条 NEON = 1 通道，每条 SVE = 2 通道
- **不做 NEON→SVE 转换**，保持现有 SIMD 类型

**标量代码**（scalar）— Kunpeng 标量流水线模型：

- 标量与向量流水线独立
- Kunpeng-0xd01：4 发射，2 ALU + 2 FP/SIMD；乘法延迟 4-5 周期，加法 2-3 周期

#### 2c. 计算展开系数

**SIMD 代码**：

1. Kunpeng-0xd01 最大 4 个 128-bit 等价通道（4 NEON 或 2 SVE）
2. `current_128bit_lanes = current_parallelism × (simd_type == "sve" ? 2 : 1)`
3. `available_lanes = max_pipelines - current_128bit_lanes`
4. `available_lanes <= 0` → 流水线已满，返回 `success=false`
5. `unroll_factor = available_lanes / current_128bit_lanes`

**标量代码**：

1. 纯加减（低延迟）：展开 2-4；乘法/乘加（高延迟）：展开 4-8
2. `unroll_factor = pipeline_width / max(1, current_parallelism)`（Kunpeng-0xd01 `pipeline_width` = 4）
3. 归约操作建议展开 4-8，用多组独立累加器

**通用约束**：

1. 向下取最近的 2 的幂（2, 4, 8, 16），最大 16
2. 寄存器压力检查：
   - 默认档：仅 caller-saved 寄存器（通用 11 个，SIMD 24 个）
   - 扩展档：加入 callee-saved（通用 +11 个，SIMD +8 个），需栈帧保护
   - **SIMD callee-saved 只保存低 64 位**（`d8-d15`），无需保存完整 128 位
   - 默认档不足 → 升级扩展档；扩展档仍不足 → 降低 `unroll_factor`
3. 缓存友好性：L1d < 32KB → factor × 0.5（向下取 2 的幂）；32-64KB 不变；>= 64KB 可适当增大

#### 2c-1. Micro-kernel internal full-unroll

对小 K 或固定 K 的计算密集型内核，可以在 micro-kernel 内部完全展开 K 循环，而不是只做普通外层循环展开。

允许进入 full-unroll 的场景：

- GEMM / GEMV / rank-k / outer-product micro-kernel 中 `K` 为编译期常量，或由 panel kernel 固定为很小的块深度。
- 短卷积核，例如 1x3、3x3、5x5 或 depthwise 小 kernel，窗口大小固定且边界已由外层处理。
- FIR/filter 的 tap 数固定且不大。
- Stencil 小半径，例如 radius=1/2/3，且边界 halo 已单独处理。
- 矩阵分解 panel kernel 中固定宽度 update，迭代次数小且寄存器预算通过。

必须拒绝或降级为普通展开的场景：

- `K` 是大运行时变量，完全展开会导致代码体积不可控。
- full-unroll 后 `calculate_register_budget.py` 或手工预算显示 `spill_risk=high|spill-likely`。
- 循环体包含不可展开的函数调用、I/O、原子、锁或跨迭代状态。
- 需要严格保持浮点左到右顺序且 full-unroll 会改变归约顺序。

full-unroll 生成要求：

- 完全移除 micro-kernel 内部 K 循环控制分支。
- 对每个展开的 K 步保持独立 load/FMA 节奏，优先与 `prefetch-optimization` 的 load/FMA interleaving 规则一致。
- 累加器必须跨 K 步保留在寄存器中，直到 tile 写回；不得每个 K 步写回内存再读回。
- 输出中记录 `full_unroll_applied=true`、`unrolled_k=<K>` 和 `code_size_risk`。

#### 2d. 确定最终展开方案

```
target_parallelism = current_parallelism × unroll_factor
target_128bit_lanes = target_parallelism × (simd_type == "sve" ? 2 : 1)
```

确保 `target_128bit_lanes <= max_pipelines`。

### 步骤 4：返回结果

源码替换由上游 `apply-optimization` 统一执行。

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "unrolling_result": {
    "success": true,
    "original_code": "<原始循环代码文本>",
    "unrolled_code": "<展开后的代码文本>",
    "unroll_factor": 2,
    "parallelism_before": 2,
    "parallelism_after": 4,
    "equivalent_128bit_lanes_before": 2,
    "equivalent_128bit_lanes_after": 4,
    "intrinsics_type": "scalar|neon|sve|sme",
    "language": "c_cpp|pure_asm|inline_asm",
    "accumulator_split": true,
    "full_unroll_applied": false,
    "unrolled_k": null,
    "code_size_risk": "low|medium|high",
    "modified_file": "<source_file_path>",
    "error_message": ""
  }
}
```

- `equivalent_128bit_lanes`：scalar=0, NEON=parallelism×1, SVE=parallelism×2, SME=parallelism×2
- `accumulator_split`：归约类操作必须为 `true`
- 失败时：`success=false`, `unrolled_code=""`, `error_message` 说明原因

`full_unroll_applied`、`unrolled_k`、`code_size_risk` 为可选扩展字段；旧消费者可忽略。若执行 micro-kernel internal full-unroll，必须填充三者。

## 规则

- **支持 C/C++ 和 ARM64 汇编**：C/C++ 路径处理 intrinsics 代码，汇编路径处理 .s/.S 纯汇编和内联 asm 块
- **汇编路径仅支持 ARM64**：GAS 语法（AT&T 风格），不支持 x86 汇编
- **支持标量和 SIMD 代码**：标量代码通过减少分支开销和暴露 ILP 获益，SIMD 代码通过利用更多向量流水线获益
- **SIMD 代码保持原有 intrinsics 类型不变**（Kunpeng 上 NEON/SVE 吞吐率等价，不做 NEON→SVE 转换）
- 展开后每份循环体必须使用**独立的临时变量/寄存器**，避免数据依赖
- 归约类操作的累加器必须拆分为多组独立累加器，循环结束后合并
- 正确处理余数元素，保证语义等价
- 寄存器压力检查：展开后不能超出可用寄存器数
- 汇编路径的寄存器分配需遵守 ARM64 调用约定：优先使用 caller-saved 寄存器（通用：`x8-x17`, `x30`；NEON/SVE：`v0/z0`-`v7/z7`, `v16/z16`-`v31/z31`）；`x18` 平台保留，不可使用
- 当 caller-saved 寄存器不足时，可通过在函数入口 `stp` 保存 callee-saved 寄存器（通用：`x19-x29` 完整 64 位；NEON/SVE：仅低 64 位 `d8-d15`），函数退出前 `ldp` 恢复，从而在函数内部使用全部寄存器
- 如果循环体内操作有不可拆分的数据依赖（循环携带依赖），返回 `success=false`
- 内联 asm 展开后必须更新 clobber 列表，包含所有新使用的寄存器
- **不做标量→SIMD 向量化**：若汇编循环为标量操作且需要向量化，应标记 `success=false` 并在 `error_message` 中建议使用 `apply-vectorization` skill
- **小 K full-unroll 属于本 skill 能力**：固定小 K、短卷积核、Stencil 小半径和 panel kernel 可完全展开；但需要寄存器预算和代码体积风险记录
- **SIMD callee-saved 只保存低 64 位**（`d8-d15`），AAPCS64 不要求保存高 64 位
