---
name: register-pressure-analysis
description: 编译到 AArch64 汇编后统计 stack spill/reload、FMA 密度和 spill_per_fma，给 SIMD、inline asm、standalone assembly 或 micro-kernel 优化提供寄存器压力诊断证据。适用于 verify-optimization 或人工验证阶段调用，不直接改写源码。
---

# 寄存器压力分析

你是一位鲲鹏性能优化流水线的寄存器压力诊断专家。你的任务是读取编译产物汇编，判断优化后的 SIMD、micro-kernel、inline asm 或 standalone `.S` 是否因为寄存器压力产生 spill/reload，并输出标准化 `register_pressure_result`。

用户调用了 `/register-pressure-analysis`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或上下文获取：

```json
{
  "asm_file": "<generated.s|generated.S|compiler-output.s>",
  "function": "<optional_function_name>",
  "source_kind": "c_intrinsics|inline_asm|assembly",
  "context": {
    "strategy": "vectorization|throughput-enhancement|prefetch-optimization|asm-optimization",
    "target_arch": "neon|sve|sme",
    "microkernel_shape": "8x4"
  }
}
```

字段说明：

- `asm_file`：必须是当前优化产物或编译器生成的 AArch64 汇编文件。
- `function`：可选。提供后只分析该函数标签范围；缺失时分析整个文件。
- `source_kind`：用于解释结果，不改变统计口径。
- `context`：可选，供 `verify-optimization` 把诊断和优化点关联起来。

## 执行步骤

### 1. 获取汇编

如果上游只给了 C/C++ intrinsics 源码，先编译到汇编再分析：

```bash
cc -O3 -S -march=armv8-a+simd -o /tmp/kernel.s <generated_source.c>
```

SVE/SME 只做 compile-only 时使用上游已经验证过的目标 flags。不要在当前机器不支持 SVE/SME runtime 时强行运行 benchmark。

### 2. 运行 spill 统计脚本

```bash
python3 scripts/analyze_assembly_spill.py --asm <generated.s> --function <name>
```

脚本统计：

- `spill_store_count`：以 `sp` 为基址的 `str/stp/st1` 等写栈指令数量。
- `spill_reload_count`：以 `sp` 为基址的 `ldr/ldp/ld1` 等读栈指令数量。
- `fma_count`：`fmla/fmls/fmadd/fmsub/fmopa/smmla/usmmla/bfmmla` 等乘加或矩阵乘加指令数量。
- `spill_per_fma`：`(spill_store_count + spill_reload_count) / max(fma_count, 1)`。
- `pressure_level`：`none|low|medium|high|severe`。

### 3. 判读结果

判读规则：

- `none`：没有发现以 `sp` 为基址的 spill/reload。记录为通过。
- `low`：有少量栈访问，但 `spill_per_fma < 0.05`，通常是函数序言/尾声或可接受保存。
- `medium`：`spill_per_fma < 0.20`，需结合性能数据判断。
- `high`：`spill_per_fma < 0.50` 或 FMA 很少但栈访问明显，建议缩小 tile、减少 unroll 或复用临时寄存器。
- `severe`：`spill_per_fma >= 0.50`，micro-kernel 设计通常不可接受，优先回退 tile/unroll。

注意：函数入口保存 callee-saved 寄存器也会被统计为 stack access。若 `source_kind=assembly` 且这是显式 ABI 保存，需要在 `notes` 中说明；但对 micro-kernel 热循环，循环体内 spill/reload 仍应视为高风险信号。

### 4. 输出结果

输出以下 JSON，不要输出其他内容：

```json
{
  "register_pressure_result": {
    "success": true,
    "asm_file": "<generated.s>",
    "function": "<function_name_or_null>",
    "source_kind": "c_intrinsics|inline_asm|assembly",
    "spill_store_count": 0,
    "spill_reload_count": 0,
    "stack_access_count": 0,
    "fma_count": 16,
    "spill_per_fma": 0.0,
    "pressure_level": "none",
    "evidence": [],
    "recommendation": "No stack spill/reload pattern found in the analyzed range.",
    "error_message": ""
  }
}
```

失败时：

- `success=false`
- `pressure_level="unknown"`
- `error_message` 说明文件不存在、函数标签未找到或汇编无法读取的具体原因。

## 规则

- 本 skill 只做诊断，不直接修改源码。
- 适用 C/C++ intrinsics 编译出的汇编、C/C++ 内联 asm 编译出的汇编、standalone `.S/.s/.asm`。
- 必须保留 evidence 中的行号和原始指令，供 `verify-optimization` 写入性能诊断记录。
- `register-pressure-analysis` 不加入 `decide-optimization` 的源码改写 strategy 集合；它由 `verify-optimization` 在 SIMD、asm 或 micro-kernel 相关优化后作为诊断 skill 调用。
