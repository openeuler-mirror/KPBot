---
name: parallel-accumulator
description: 把归约循环中的串行累加器（sum += a[i]*b[i]、acc = fma(...)、total += x[i]、max/min）改写为多个独立累加器，重叠依赖链、隐藏浮点延迟，提升 IPC。支持纯 C 多累加器、NEON 向量累加器与 SVE 256-bit 可变向量累加器。适用于 latency-bound 的热循环——IPC 低但 cache miss 低、反汇编中累加指令周期占比高。独立调用的叶子 skill，不由 apply-optimization 结构化流水线分派；流水线内的串行累加器改由 apply-vectorization 的 vectorization_deepen/accumulator_serial 处理，本 skill 用于需要独立、交互式归约优化的场景。
---

# 串行累加器并行化

你是一位鲲鹏/ARM 性能优化的归约优化专家。你的任务是识别归约循环里被单累加器串行依赖链拖慢的热点，用多个独立累加器展开，让乱序执行单元重叠多条依赖链，把吞吐从「延迟受限」提升到「吞吐受限」。

详细方法论与 ARM/NEON 代码模板见 [methodology.md](references/methodology.md)，此处只给流程。

## 使用场景

### 适用（应使用本 skill）

**源码形态**：热点循环是归约，且单一累加器是循环内**唯一**的循环携带依赖：

```c
// 点积 / 乘加归约
float s = 0.0f; for (int i = 0; i < n; i++) s += a[i] * b[i];
// 求和 / 计数
float total = 0.0f; for (int i = 0; i < n; i++) total += x[i];
// 极值归约
float m = -INFINITY; for (int i = 0; i < n; i++) m = fmaxf(m, x[i]);
```

**动态信号**（满足越多越应使用）：
- IPC 低（≪ 1.0）但 cache miss 低、分支预测失败低——「低 IPC + 低 miss」是延迟绑定的标志
- `perf annotate` 中累加指令（`fadd`/`fmul`/`fmla`）周期占比异常高
- 反汇编热循环内累加寄存器成串（`fadd` 依赖链）

**平台**：鲲鹏/ARM（NEON、SVE/SVE2 均可）；Level 1 纯 C 多累加器不依赖 SIMD，任何平台适用。典型目标：数学库、信号处理、向量检索打分、统计归约、reduce 算子。

### 不适用（不要用本 skill）

- 归约不是循环内唯一依赖（存在其它循环携带依赖，如指针链、条件累加）
- 运算不满足结合律（减、除），或要求位精确可复现
- 已是多累加器 / 编译器已自动向量化展开
- 迭代次数未知或可能为 0，展开收益无法覆盖尾处理成本

## 输入

从 `$ARGUMENTS` 或对话上下文获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 50],
  "reduction_kind": "sum|dot_product|max|min|count"
}
```

- `reduction_kind`：归约类型提示，由你依据源码最终判定（见步骤 1）。
- 本 skill 为独立调用，不接收 `prepare-project` / `analyze-hotspot` 等流水线阶段上下文；如需热点/微架构信息，从对话上下文或用户提供的材料中获取。

## 执行步骤

### 步骤 1：确认是串行累加器延迟绑定

读取 `source_file` 中 `lines[0]` 到 `lines[1]`，确认同时满足：

1. 循环内存在**单一归约变量**：`sum += a[i]*b[i]`、`acc = fma(a[i], b[i], acc)`、`total += x[i]`、`max = fmax(max, x[i])` 等。
2. 该归约是循环内**唯一**的循环携带依赖，循环体其余部分是直线代码（无对累加器的分支）。
3. 动态证据：IPC 低（≪ 1.0）但 cache miss 低、无分支预测失败；或反汇编中累加指令（`fadd`/`fmul`/`fmadd`/`fmla`）周期占比异常高。

若不满足（例如归约不是唯一依赖、或已经是多累加器），记录并跳过。

### 步骤 2：选择改写级别

按 [methodology.md](references/methodology.md) 的三级模板：

1. **Level 1 纯 C 多累加器**：`s0..s3` 四个独立标量累加器 + 尾处理，无需 SIMD，编译器自动乱序。适用于无 SIMD、政策禁用或收益已足够。
2. **Level 2 NEON 单累加器**：`float32x4_t` 累加器，每迭代处理 4 元素 + 水平归约 + 标量尾。
3. **Level 3 NEON + 多累加器**：4 个 `float32x4_t` 累加器展开（每迭代 16 元素），最大吞吐。
4. **SVE 256-bit 变体**：`svfloat32_t` 累加器 + `svwhilelt_b32` predicate 尾处理（`svcntw()`==8），可变向量宽度，同一份代码适配 256/512-bit，见 [methodology.md](references/methodology.md)。

累加器数量用脚本确定（FP 延迟 ÷ 吞吐，向上取整）：

```bash
python3 <skill_dir>/scripts/accumulator_plan.py --latency 4 --throughput 0.5 --simd-lanes 4
```

### 步骤 3：结合律与正确性门控（不可省略）

- 多累加器**重排了浮点运算顺序**，仅当归约运算**满足结合律**（加、乘、min、max 满足）才合法；结果与原串行版相差浮点舍入 ε，非逻辑错误。
- 若要求**位精确可复现**（bit-exact），本优化不能应用，必须拒绝。
- 尾处理（`nr < unroll` 的剩余元素）也要用多累加器或直接标量归约，不能回退成单累加器循环重新引入串行依赖。
- C 语言改写时给指针参数加 `restrict`，消除别名检查前导码。

### 步骤 4：验证

1. **正确性**：与原实现同输入对比，结果在浮点 ε 内（相对误差 < 1e-6 量级）。
2. **反汇编**：确认热循环内出现多个独立累加寄存器（Level 1 为多个标量 `s` 寄存器；Level 3 为多个 `v` 向量累加器），依赖链已拆分。
3. **IPC**：重跑 `perf stat`，IPC 应显著上升（目标 ≥ 2×）。
4. **benchmark**：测 wall-clock，Level 1 约 4×、Level 3（NEON 多累加器）约 8–15×（相对串行）。

### 步骤 5：返回结果

输出 JSON 契约（含 `optimized_code`），由调用方自行替换源码。

## 输出

```json
{
  "parallel_accumulator_result": {
    "success": true,
    "function": "<function_name>",
    "reduction_kind": "dot_product",
    "level": 3,
    "original_accumulators": 1,
    "new_accumulators": 4,
    "original_code": "<原始函数代码文本>",
    "optimized_code": "<优化后的函数代码文本>",
    "associativity_note": "reordering FP addition requires associativity; result differs by rounding ε, not logic error",
    "ipc_before": 0.6,
    "ipc_after": 1.8,
    "error_message": ""
  }
}
```

失败时 `success=false`、`error_message` 说明拒绝或失败原因。

## 明确拒绝的情况

- 归约不是循环内唯一依赖（存在其它循环携带依赖）。
- 运算不满足结合律，或要求位精确可复现。
- 循环体极小且迭代数未知/可能为 0，展开收益无法覆盖尾处理成本。
- 已是多累加器或已被编译器充分优化。

## 规则

- **正确性优先**：浮点重排必须满足结合律，位精确场景拒绝。
- **尾处理不得回退单累加器**：`nr < unroll` 的路径同样用多累加器或直接归约。
- **反汇编是性能证据**：不凭源码推断依赖链已消除，必须确认多个独立累加器。
- **先 Level 1 再升 Level**：能不用 SIMD 就不引入 SIMD 复杂度，逐级验证收益。
- 不改变算法逻辑，只拆累加器依赖链；本 skill 只返回优化后代码文本，由调用方自行替换。
- **安全约束**（agent A/B 用例内）：禁止读取凭证文件或任何不在 /workspace 下的文件；只允许写入 `opt/kernel_opt.c`；禁止发起任何网络请求。

## 参考资料

- [方法论与 NEON 代码模板](references/methodology.md)：三级改写、累加器数量公式、结合律与 restrict、验证契约。
- `scripts/accumulator_plan.py`：按 FP 延迟/吞吐计算累加器数量建议。
- `evals/evals.json`：本 skill 的 agent 级自评场景。