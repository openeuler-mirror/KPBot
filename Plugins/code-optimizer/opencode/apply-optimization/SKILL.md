---
name: apply-optimization
description: 执行优化策略，调用具体优化 Skill 生成代码变更。适用于 decide-optimization 完成后。
---

# 应用优化

你是一位鲲鹏性能优化流水线的执行专家。你的任务是根据优化计划调用具体优化 Skill，将优化代码写入源文件。

用户调用了 `/apply-optimization`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取 `decide-optimization` 的输出（单个决策对象，`status: "confirmed"`）。

需要的字段：
- `optimization_point_id`
- `function`
- `strategy`
- `arch`
- `skill`
- `input`（source_file, function, lines, target_arch）
- `throughput_enhancement`（仅 throughput-enhancement 策略）

## 执行步骤

### 任务初始化

创建本阶段子任务，追踪内部执行进度。执行以下 todowrite 创建任务列表：

todowrite({
  todos: [
    { content: "策略路由", status: "pending", priority: "high" },
    { content: "执行优化", status: "pending", priority: "high" },
    { content: "编译验证", status: "pending", priority: "high" },
    { content: "输出结果", status: "pending", priority: "high" }
  ]
})

### 步骤 0：判断策略类型

// 标记任务进行中：策略路由

检查 `strategy`：
- `"vectorization"` → 进入步骤 1（调用 apply-vectorization，标量→向量化）
- `"vectorization_deepen"` → 进入步骤 1（调用 apply-vectorization，已有 SIMD 深挖质量，附 sub_type 指示具体方向）
- `"autovec-source-transform"` → 进入步骤 1m（调用 source-transform-autovec，执行一次轻量源码变形后让编译器重新尝试自动向量化）
- `"throughput-enhancement"` → 进入步骤 1a（调用 loop-unrolling）
- `"prefetch-optimization"` → 进入步骤 1b（调用 prefetch-optimization）
- `"branch-elimination"` → 进入步骤 1c（调用 branch-elimination）
- `"memory-access-optimization"` → 进入步骤 1d（调用 memory-access-optimization）
- `"compiler-flag-tuning"` → 进入步骤 1e（调用 compiler-flag-tuning）
- `"asm-optimization"` → 进入步骤 1f（调用 asm-optimization）
- `"bulk-memory-opt"` → 进入步骤 1g（直接生成 libc 调用，不调用子 Skill）
- `"variant-selection"` → 进入步骤 1h（无代码修改的 pass-through，直接跳到步骤 5 编译验证 + 传递到 verify-optimization 实测选型）
- `"scalar-vector-hybrid"` → 进入步骤 1j（标矢量混合决策，调用 scalar-vector-hybrid）
- `"code_hoisting"` → 进入步骤 1i（循环不变量提升，零风险机械变换，将循环体内不依赖迭代变量的计算提升到循环外）
- `"special-case-optimization"` → 进入步骤 1j（调用 special-case-optimization）
- `"operation-fusion"` → 进入步骤 1k（调用 operation-fusion）
- `"precision-transform"` → 进入步骤 1l（调用 precision-transform）
- `"math-rewrite"` → 不应进入本阶段；若收到，返回 `status: "failed"` 并提示 decide-optimization 应自动 skip

策略路由完成：
todowrite({ todos: [{ content: "策略路由", status: "completed", priority: "high" }] })
进入对应优化步骤：
todowrite({ todos: [{ content: "执行优化", status: "pending", priority: "high" }] })

### 步骤 1：构造请求 JSON（vectorization / vectorization_deepen 策略）

当 `strategy == "vectorization"` 或 `strategy == "vectorization_deepen"` 时，将 `input` 转换为 `apply-vectorization` 的规范请求格式：

```json
{
  "target_function": "<input.function>",
  "loop_info": {
    "file_path": "<input.source_file>",
    "start_line": <input.lines[0]>,
    "end_line": <input.lines[1]>
  },
  "target_arch": "<input.target_arch>",
  "data_types": ["<从源码推断的数据类型>"],
  "microkernel_hint": "<input.microkernel_hint 可选透传>",
  "isa_extensions": ["<从 prepareProject 推导>"],
  "mode": "<vectorization 时为 full，vectorization_deepen 时为 deepen>",
  "sub_type": "<仅 vectorization_deepen 时传递 input.sub_type，否则 null>",
  "semantic_contract": { "aliasing": "...", "index_properties": "..." }
}
```

**`data_types` 推断规则**：
1. 用 read 工具读取 `source_file` 的 `lines[0]` 到 `lines[1]`
2. 识别循环变量和运算中的数据类型：`float`/`float32_t` → `float32`，`int32_t` → `int32`，`uint32_t` → `uint32`，`int16_t` → `int16`，`uint16_t` → `uint16`，`int8_t` → `int8`，`uint8_t` → `uint8`
3. 如果类型无法确定，默认使用 `float32`
4. 若 `input.microkernel_hint` 存在，透传给 `apply-vectorization`，但仍由子 Skill 从源码验证累加域和 tile 形状

**`isa_extensions` 推导规则**（避免生成硬件不支持的 intrinsics）：
1. 优先从 `prepareProject.microarch_file` 中的 ISA 特性表提取（如 0xd01 无 SVE → `[]`；0xd03 有 SVE2+BF16 → `["sve2", "bf16"]`）
2. 若无 microarch_file，从 `prepareProject.repo.compilation.performance_flags` 中的 `arch_flags`/`cpu_flags` 提取（如 `-march=armv8.2-a+sve` → `["sve"]`）
3. 推导不出 → 空数组 `[]`（apply-vectorization 退回到 baseline NEON/SVE）

**`mode` 和 `sub_type`**：
- `strategy == "vectorization"` → `"mode": "full"`，`"sub_type": null`
- `strategy == "vectorization_deepen"` → `"mode": "deepen"`，`"sub_type": "<input.sub_type>"`（如 `lane_width_partial`、`remainder_scalar`、`accumulator_serial` 等）。apply-vectorization 据此跳过标量→SIMD 判断，直接进入已有 SIMD 补全/深挖

**`semantic_contract`**（连续访存场景传递最小安全证明）：
- 当 `analyzeHotspot` 中 `static_analysis.memory_access_pattern == "stream"` → 传递：
  ```json
  "semantic_contract": { "aliasing": "no_alias_assumed", "index_properties": "not_applicable" }
  ```
- 其他访存模式（strided/indirect/aos_field）→ 不传此字段，由 apply-vectorization 按保守策略处理

### 步骤 1a：循环展开（throughput-enhancement 策略）

当 `strategy == "throughput-enhancement"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "simd_type": "<throughput_enhancement.simd_type>",
  "recommended_parallelism": <throughput_enhancement.target_parallelism>,
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>"
  }
}
```

2. 使用 Skill tool 调用 `loop-unrolling`，传入上述 JSON

3. 等待 `loop-unrolling` 返回 `unrolling_result`

4. 检查 `unrolling_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `unrolling_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1b：软件预取（prefetch-optimization 策略）

当 `strategy == "prefetch-optimization"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "access_pattern": "stream|strided|indirect",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

`access_pattern` 推断规则：
- 连续数组访问 `a[i]`、`a[i+offset]` → `"stream"`
- 固定步长 `a[i*stride]` → `"strided"`
- 间接索引 `a[index[i]]` → `"indirect"`

2. 使用 Skill tool 调用 `prefetch-optimization`，传入上述 JSON
3. 等待返回 `prefetch_result`
4. 检查 `prefetch_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `prefetch_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1c：分支消除（branch-elimination 策略）

当 `strategy == "branch-elimination"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "branch_type": "if-else|switch|conditional",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

`branch_type` 推断规则：读取源码，识别循环体内的分支类型。

2. 使用 Skill tool 调用 `branch-elimination`，传入上述 JSON
3. 等待返回 `branch_elimination_result`
4. 检查 `branch_elimination_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `branch_elimination_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1d：访存模式优化（memory-access-optimization 策略）

当 `strategy == "memory-access-optimization"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "optimization_type": "aos_to_soa|cache_alignment|tiling|reordering",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

`optimization_type` 推断规则：从源码分析访存模式选择最合适的优化类型。

2. 使用 Skill tool 调用 `memory-access-optimization`，传入上述 JSON
3. 等待返回 `memory_access_result`
4. 检查 `memory_access_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `memory_access_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1e：编译选项调优（compiler-flag-tuning 策略）

当 `strategy == "compiler-flag-tuning"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

注意：compiler-flag-tuning 不需要 `lines` 参数（优化目标是构建配置，非源码行范围）。

2. 使用 Skill tool 调用 `compiler-flag-tuning`，传入上述 JSON
3. 等待返回 `compiler_flag_result`
4. 检查 `compiler_flag_result.success`：
   - `true` → 进入步骤 4（应用构建配置变更），然后进入步骤 5（编译验证）
   - `false` → 不修改任何文件，记录 `compiler_flag_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1f：汇编优化（asm-optimization 策略）

当 `strategy == "asm-optimization"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "optimization_type": "<input.optimization_type>",
  "language": "<input.language>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

`optimization_type` 取值：`ldp_stp_merge` | `post_index_addressing` | `redundant_move_elimination` | `loop_unroll` | `prefetch_enhancement` | `loop_counter` | `instruction_idiom` | `multi_vector_merge_test` | `macro_fusion_enablement` | `uarch_substitution` | `instruction_interleaving` | `predication_mode`

`language` 取值：`pure_asm`（.s/.S 文件）| `inline_asm`（C/C++ 内联 asm 块）

2. 使用 Skill tool 调用 `asm-optimization`，传入上述 JSON
3. 等待返回 `asm_optimization_result`
4. 检查 `asm_optimization_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `asm_optimization_result.error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

### 步骤 1g：批量内存操作（bulk-memory-opt 策略）

当 `strategy == "bulk-memory-opt"` 时执行。不调用子 Skill，直接在 apply-optimization 中生成代码替换。

1. **读取原始代码**：用 read 工具读取 `input.source_file` 中 `input.lines[0]` 到 `input.lines[1]` 的循环代码
2. **判断操作类型**：
   - 所有迭代写相同常量 → `memset` 候选（`wmemset` 在 `sizeof(wchar_t) >= 2` 且写值可扩展到 wchar_t 宽度时更优）
   - 写值来自另一个数组的连续 load → `memcpy` 候选
3. **生成替换代码**：
   ```c
   // memset 候选
   memset(dst, init_val_byte, N * sizeof(*dst));
   // 或 wmemset（当 sizeof(wchar_t) >= 2 且连续写入 16-bit 值时）
   wmemset((wchar_t *)dst, hash_init_val, N / sizeof(wchar_t));

   // memcpy 候选
   memcpy(dst, src, N * sizeof(*dst));
   ```
4. 用 edit 工具替换原循环代码
5. 进入步骤 5（编译验证）

### 步骤 1h：函数变体选型（variant-selection 策略）

当 `strategy == "variant-selection"` 时执行。不做代码修改，仅做 pass-through。

1. `status` 设为 `"applied"`（无代码变更，视为成功传递）
2. `optimization_success` 设为 `true`
3. `modified_files` 为空数组 `[]`
4. `compilation.attempted` 设为 `false`（无代码变更，跳过编译）
5. `smoke_test.attempted` 设为 `false`
6. 输出中 `skill_used` 为 `"variant-selection"`
7. 跳转到步骤 5 输出结果

**下游 verify-optimization** 将接收此 pass-through 结果，对 variant-selection 的每个变体运行 `perf stat` 对比并选择最优。

### 步骤 1i：循环不变量提升（code_hoisting 策略）

当 `strategy == "code_hoisting"` 时执行。零风险机械变换，直接操作源文件：

1. 从 `input` 中获取不变量信息：`loop_invariants`（变量名和行号列表）
2. Read 源文件，定位循环体边界
3. 将不依赖迭代变量的计算（常量读取、外部参数引用、循环前已定义的值）提升到循环体外
4. 用 edit 工具移动代码：从循环体内剪切 → 粘贴到循环体前
5. 若变量仅在循环内使用 → 在循环外声明，循环内保持不变
6. 验证提升后语义不变（读取位置提前但值相同）
7. 更新 `modified_files`，`skill_used` 为 `"code_hoisting"`，跳转到步骤 5

### 步骤 1j：标矢量混合（scalar-vector-hybrid 策略）

当 `strategy == "scalar-vector-hybrid"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "pipeline_strategy": "<input.pipeline_strategy>",
  "serial_chains": ["<input.serial_chains>"],
  "pipeline_utilization": "<input.pipeline_utilization>",
  "language": "<input.language>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

2. 使用 Skill tool 调用 `scalar-vector-hybrid`，传入上述 JSON
3. 等待返回 `scalar_vector_hybrid_result`
4. 检查 `scalar_vector_hybrid_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译 + 汇编验证）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"`，跳转到步骤 6（输出结果）

### 步骤 1k：特殊情况快路径 / 局部特化（special-case-optimization 策略）

当 `strategy == "special-case-optimization"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "sub_type": "<input.sub_type>",
  "strategy_payload": "<input.strategy_payload>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

`strategy_payload` 必须原样透传，不得丢弃以下集中字段：`rewrite_kind`、`guard_condition`/`fast_path_condition`、`fallback_required`、`equivalence_scope`、`equivalence_basis`、`risk_notes`、`verification_focus`。旧 fast path 输入缺少这些字段时，可由子 Skill 根据 `sub_type` 做兼容推断；局部等价改写/硬件契约路径缺少这些字段时应由子 Skill 返回 `success=false`。

2. 使用 Skill tool 调用 `special-case-optimization`
3. 等待返回 `special_case_result`
4. 检查 `special_case_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `special_case_result.error_message`，设置 `status: "failed"` 并跳转到步骤 6

### 步骤 1l：操作融合（operation-fusion 策略）

当 `strategy == "operation-fusion"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "sub_type": "<input.sub_type>",
  "strategy_payload": "<input.strategy_payload>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>",
    "analyzeCallerContext": "<对话上下文中的 analyze-caller-context 输出（若有）>"
  }
}
```

2. 使用 Skill tool 调用 `operation-fusion`
3. 等待返回 `operation_fusion_result`
4. 检查 `operation_fusion_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `operation_fusion_result.error_message`，设置 `status: "failed"` 并跳转到步骤 6

### 步骤 1m：精度变换（precision-transform 策略）

当 `strategy == "precision-transform"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "sub_type": "<input.sub_type>",
  "precision_contract": "<input.strategy_payload.precision_contract>",
  "strategy_payload": "<input.strategy_payload>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

2. 使用 Skill tool 调用 `precision-transform`
3. 等待返回 `precision_transform_result`
4. 检查 `precision_transform_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证）
   - `false` → 不修改源文件，记录 `precision_transform_result.error_message`，设置 `status: "failed"` 并跳转到步骤 6

### 步骤 1n：自动向量化源码变形（autovec-source-transform 策略）

当 `strategy == "autovec-source-transform"` 时执行：

1. **构造请求 JSON**：

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "sub_type": "<input.sub_type>",
  "strategy_payload": "<input.strategy_payload>",
  "context": {
    "prepareProject": "<对话上下文中的 prepare-project 输出>",
    "analyzeHotspot": "<对话上下文中的 analyze-hotspot 输出>"
  }
}
```

2. 使用 Skill tool 调用 `source-transform-autovec`
3. 等待返回 `source_transform_result`
4. 检查 `source_transform_result.success`：
   - `true` → 进入步骤 3（替换源码），然后进入步骤 5（编译验证 + 一次 compiler feedback 复查）
   - `false` → 不修改源文件，记录 `source_transform_result.error_message` 或 `skipped_reason`，设置 `status: "failed"` 并跳转到步骤 6

**约束**：本策略只允许一次源码变形。不做 IR equivalence、Alive2、长轮次 fuzz，也不做 optional deep mode；若一次改写后仍未向量化，直接输出 `failed` 或 `skipped`。

### 步骤 2：调用 apply-vectorization（仅 vectorization / vectorization_deepen）

仅当 `strategy == "vectorization"` 或 `strategy == "vectorization_deepen"` 时执行。使用 Skill tool 调用 `apply-vectorization`，将步骤 1 构造的请求 JSON 作为参数传入。

等待 `apply-vectorization` 返回 `vectorization_result`。

### 步骤 3：替换源码

**本 Skill 统一负责源码替换**（子 Skill 只返回代码文本，不直接修改文件）。

根据当前路径执行替换：

**vectorization 路径**（来自步骤 2 的 `vectorization_result`）：

1. 检查 `vectorization_result.success`：
   - `true` → 先读取 `replacement_kind` 和 `application_mode`，再选择应用边界；不能把完整函数或 translation unit 插入 `loop_info.start_line` 到 `loop_info.end_line`
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

2. 根据 `replacement_kind` 应用：
   - `translation_unit` 或 `full_function` → 优先调用 `apply-vectorization` 的物化链路写入 `generate/` 并把输出文件登记为 pipeline 产物；若本流程明确要求原地替换，则必须先定位 `target_function` 的完整函数边界，再替换整个函数定义
   - `function_body` → 用原始函数签名和边界，只替换函数体内部，不替换函数声明、注释或相邻函数
   - `loop_body` → 才允许用 `loop_info.start_line` 到 `loop_info.end_line` 作为替换范围
   - 缺失字段 → 若 `vectorized_code` 包含完整 `target_function` 定义，按 `translation_unit` 处理；否则按 `function_body` 处理，并在结果中记录兼容模式

3. 根据 `application_mode` 应用：
   - `materialize_to_generate` 或缺失 → 不覆盖用户源码，调用物化链路生成主 C/C++ 源码和 artifacts，并把这些路径加入 pipeline 输出
   - `inplace_replace` → 只有在用户或上层策略明确要求原地修改时使用，并且必须遵守上一步的边界选择

4. 若 `vectorization_result.artifacts` 存在，必须把 C wrapper、standalone `.S/.s/.asm`、header 或其他 artifact 一并加入 `modified_files` / `generated_artifacts` / benchmark 输入；不能只记录 `vectorized_code`

**throughput-enhancement 路径**（来自步骤 1a 的 `unrolling_result`）：

1. 检查 `unrolling_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `unrolling_result.original_code` 替换为 `unrolling_result.unrolled_code`（替换范围：`lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

**prefetch-optimization 路径**（来自步骤 1b 的 `prefetch_result`）：

1. 检查 `prefetch_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `prefetch_result.original_code` 替换为 `prefetch_result.optimized_code`（替换范围：`lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

**branch-elimination 路径**（来自步骤 1c 的 `branch_elimination_result`）：

1. 检查 `branch_elimination_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `branch_elimination_result.original_code` 替换为 `branch_elimination_result.optimized_code`（替换范围：`lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

**memory-access-optimization 路径**（来自步骤 1d 的 `memory_access_result`）：

1. 检查 `memory_access_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `memory_access_result.original_code` 替换为 `memory_access_result.optimized_code`（替换范围：`lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

**asm-optimization 路径**（来自步骤 1f 的 `asm_optimization_result`）：

1. 检查 `asm_optimization_result.success`：
   - `true` →
     - `language == "pure_asm"`：用 read 工具读取 .s/.S 文件，用 edit 工具将 `asm_optimization_result.original_code` 替换为 `asm_optimization_result.optimized_code`（替换范围：`lines[0]` 到 `lines[1]`）
     - `language == "inline_asm"`：用 read 工具读取 C/C++ 文件，用 edit 工具将 `asm_optimization_result.original_code` 替换为 `asm_optimization_result.optimized_code`（仅替换 `__asm__ volatile(...)` 块内的 asm 字符串内容，保留外围 C 代码不变）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并记录 error_message，跳转到步骤 6（输出结果）

**scalar-vector-hybrid 路径**（来自步骤 1j 的 `scalar_vector_hybrid_result`）：

1. 检查 `scalar_vector_hybrid_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `scalar_vector_hybrid_result.original_code` 替换为 `scalar_vector_hybrid_result.optimized_code`（替换范围优先使用 result 中的精确 old/new 文本；否则使用 `lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并跳转到步骤 6

**special-case-optimization 路径**（来自步骤 1k 的 `special_case_result`）：

1. 检查 `special_case_result.success`：
   - `true` → 进入下方 special-case 专用保护检查
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并跳转到步骤 6
2. 写入前执行 special-case 专用保护检查：
   - 若 `fallback_preserved != true`，仅当 `rewrite_kind == "local_equivalence_rewrite"`、`equivalence_scope == "full_domain"`、`equivalence_basis` 明确覆盖全输入域且 `risk_notes` 为空或声明无数值/安全/别名风险时允许继续；否则按失败处理，不写入源码。
   - 若 `rewrite_kind` 属于 `constraint_specialization|boundary_fast_path|hardware_contract_path`，必须声明 `fallback_preserved == true`。
   - 若 `source_file` 或替换区域位于 test/benchmark harness，且输入目标未明确声明优化对象就是 harness，则按失败处理；不得通过修改测试或 benchmark 调用更快变体来制造性能提升。
3. 保护检查通过后，用 read 工具读取源文件，用 edit 工具将 `special_case_result.original_code` 替换为 `special_case_result.optimized_code`（替换范围优先使用 result 中的精确 old/new 文本；否则使用 `lines[0]` 到 `lines[1]`）。
4. 若 `special_case_result` 包含 `rewrite_kind`、`equivalence_scope`、`equivalence_basis`、`risk_notes`、`validation_focus`，将其保留到最终输出，供 `verify-optimization` 生成针对性测试矩阵。

**operation-fusion 路径**（来自步骤 1l 的 `operation_fusion_result`）：

1. 检查 `operation_fusion_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `operation_fusion_result.original_code` 替换为 `operation_fusion_result.optimized_code`；若涉及多个局部片段，逐个使用 `edits[]` 中的 `old_string` / `new_string`
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并跳转到步骤 6

**precision-transform 路径**（来自步骤 1m 的 `precision_transform_result`）：

1. 检查 `precision_transform_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `precision_transform_result.original_code` 替换为 `precision_transform_result.optimized_code`
   - `false` → 不修改源文件，记录 `error_message`，设置 `status: "failed"` 并跳转到步骤 6

**autovec-source-transform 路径**（来自步骤 1n 的 `source_transform_result`）：

1. 检查 `source_transform_result.success`：
   - `true` → 用 read 工具读取源文件，用 edit 工具将 `source_transform_result.original_code` 替换为 `source_transform_result.optimized_code`（替换范围优先使用精确 old/new 文本；否则使用 `lines[0]` 到 `lines[1]`）
   - `false` → 不修改源文件，记录 `error_message` 或 `skipped_reason`，设置 `status: "failed"` 并跳转到步骤 6
2. 本路径必须保留原语义 fallback；若 result 未声明 `fallback_preserved == true`，按失败处理，不写入源码。

### 步骤 4：应用构建配置变更（仅 compiler-flag-tuning）

当 `strategy == "compiler-flag-tuning"` 时，不修改源码，而是修改构建配置文件：

1. 从 `compiler_flag_result.build_config_changes` 获取变更列表
2. 对每个变更：
   - 用 read 工具读取目标文件
   - 用 edit 工具应用变更（添加/修改编译选项）
3. 编译验证时需要 clean build（`make clean && make`）

### 步骤 5：轻量快速验证

todowrite({ todos: [{ content: "执行优化", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "编译验证", status: "pending", priority: "high" }] })

**目的**：快速验证代码变更的基本正确性，不阻塞流程。仅验证当前优化点不引入低级错误，识别明显的失败后尽早终止，避免将无效代码传递给下游 verify-optimization。

**注意**：本步骤是轻量级检查，**不能替代** verify-optimization 的全量验证。单点代码修改可能影响其他调用模块，verify-optimization 会运行完整测试套件 + 性能对比。

1. **编译检查**（必做）：
   - cmake：`cd <repo.path>/build && make -j$(nproc) 2>&1 | tail -30`
   - make：`cd <repo.path> && make -j$(nproc) 2>&1 | tail -30`
   - compiler-flag-tuning 策略需 clean build：`make clean && make -j$(nproc) 2>&1 | tail -30`
   - asm-optimization 策略且 `modified_files` 包含 `.s`/`.S` 文件：先做汇编语法检查 `as -o /dev/null <modified_file> 2>&1`，再执行项目构建
   - 编译失败 → 记录错误，`compilation.ok = false`，跳到步骤 6 输出结果
   - 编译成功 → **必须继续做汇编验证**（见步骤 5.0）

2. **汇编验证**（编译成功后必做，防止"写了 SIMD 代码但编译器没生成正确指令"）：
   - 对 `modified_files` 中的每个关键函数，反汇编检查：
     ```bash
     objdump -d <binary> | grep -A30 "<function_name>:"
     ```
   - 验证清单（根据策略类型选择）：
     | 策略 | 预期指令 | 不应出现 |
     |------|---------|---------|
     | vectorization/NEON | `vld1q`/`vmlaq`/`vst1q` | 热点区域标量 `ldr`/`fmul`/`str` |
     | vectorization/SVE | `whilelt`/`ld1w`/`fmla.*p0` | `movprfx` 堆叠、标量 `memcpy` 循环 |
     | SVE destructive ops | 单指令（如 `revh z.s`） | 每条前冗余 `movprfx` |
     | scalar-vector-hybrid | 标量化段：`add`/`eor`/`and`/`lsl`/`lsr`；矢量段：`vld1q`/`vmlaq`/`vst1q`；搬移：`fmov` | 标量化段出现 `shl`/`ushr`/`ext`（V 寄存器） |
| prefetch-optimization | `prfm pldl1keep` | — |
     | compiler-flag-tuning | 指令序列应与改前有实质差异 | — |
     | memory-access-opt | `vld1q` stride 模式改变 | — |
     | autovec-source-transform | compiler feedback 显示目标 loop 已向量化，或反汇编出现 SIMD 主循环 | 主循环仍只有标量热点指令 |
   - 若预期指令缺失 → **不通过**，记录到 `assembly_check`。除 `autovec-source-transform` 外，可回到步骤 1 修正；`autovec-source-transform` 不做第二轮修补，直接设置 `status: "failed"` 或 `"skipped"` 并输出 missed reason
   - 若发现冗余指令（如 `movprfx`）→ 记录警告，可继续但需在 `assembly_check.issues` 中标记
   - **核心原则：写了 SIMD 代码不看反汇编 = 盲飞。必须确认编译器真的生成了预期的指令。**

3. **快速冒烟测试**（可选，编译成功后执行）：
   - 若有测试框架且可从 `test_cases` 中提取与当前函数相关的单个用例：
     - ctest：`cd <repo.path>/build && ctest -R "<related_case>" --output-on-failure 2>&1 | tail -20`
     - gtest：`cd <repo.path>/build && ./<test_binary> --gtest_filter="*<related_pattern>*" 2>&1 | tail -20`
   - 仅运行 1-2 个最相关用例，验证基本功能不受影响
   - 测试失败 → 记录错误，不阻塞，交给 verify-optimization 处理
   - 无法提取相关用例 → 跳过，在输出中标记 `smoke_test.attempted = false`

3. 编译失败时记录错误但不自动修复

**编译失败处理**：
- 编译失败说明已写入的代码有语法/类型问题，但**不立即回退源文件**——保留变更供下游 fix-code 阶段修复
- 在输出中记录完整的 `compilation.error`，传递到 fix-code 的输入中

**清理时机**：本 Skill 不做清理。若下游 fix-code 也失败，编排器会在优化点间清理协议中回退源文件并清理临时文件。

### 步骤 6：输出结果

todowrite({ todos: [{ content: "编译验证", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "输出结果", status: "pending", priority: "high" }] })

## 输出

输出 JSON 完成后：
todowrite({ todos: [{ content: "输出结果", status: "completed", priority: "high" }] })

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "function": "<function_name>",
  "optimization_point_id": "<opt_point_id>",
  "strategy": "vectorization|vectorization_deepen|autovec-source-transform|throughput-enhancement|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|code_hoisting|variant-selection|special-case-optimization|operation-fusion|precision-transform",
  "status": "applied|failed|skipped|compilation_failed",
  "skill_used": "apply-vectorization|source-transform-autovec|loop-unrolling|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|code_hoisting|variant-selection|special-case-optimization|operation-fusion|precision-transform",
  "optimization_success": true,
  "modified_files": ["<file_path>"],
  "cleanup": {
    "needed": false,
    "executed": false,
    "reverted_files": [],
    "removed_temp_files": [],
    "build_cleaned": false
  },
  "compilation": {
    "attempted": true,
    "ok": true,
    "error": null
  },
  "assembly_check": {
    "performed": true,
    "expected_instructions_found": true,
    "issues": [],
    "details": ""
  },
  "smoke_test": {
    "attempted": false,
    "case": null,
    "passed": null,
    "details": null
  },
  "vectorization_result": {
    "success": true,
    "intrinsics_used": ["<intrinsic1>", "<intrinsic2>"],
    "epilogue_handling": "<tail processing description>",
    "codegen_style": "intrinsics|inline_asm|assembly",
    "replacement_kind": "full_function|function_body|loop_body",
    "application_mode": "materialize_to_generate|inplace_replace",
    "artifacts": [],
    "safety_checks": ["<安全验证说明>"],
    "original_loop": "<原始循环代码>",
    "vectorized_code": "<向量化代码>",
    "expected_speedup": "<预期加速比>",
    "error_message": ""
  },
  "throughput_enhancement_result": null,
  "prefetch_optimization_result": null,
  "register_pressure_result": null,
  "branch_elimination_result": null,
  "memory_access_result": null,
  "compiler_flag_result": null,
  "asm_optimization_result": null,
  "scalar_vector_hybrid_result": null,
  "special_case_result": null,
  "operation_fusion_result": null,
  "precision_transform_result": null,
  "source_transform_result": null
}
```

`status` 取值：
- `applied`：优化成功，代码已写入源文件
- `failed`：子 Skill 拒绝改写（子 Skill 返回 success=false）
- `skipped`：子 Skill 判定缺少明确 missed-vectorization 反馈或命中高风险排除条件，未修改源文件
- `compilation_failed`：代码已写入但编译失败

`optimization_success`：综合判断（子 Skill 成功且编译通过时为 true）

## 规则

- 本 Skill 是编排层，本身不做代码生成，全部委托给 `apply-vectorization`、`source-transform-autovec`、`loop-unrolling`、`prefetch-optimization`、`branch-elimination`、`memory-access-optimization`、`compiler-flag-tuning`、`asm-optimization`、`special-case-optimization`、`operation-fusion` 或 `precision-transform`
- 调用子 Skill 时，必须传递完整的规范请求 JSON
- **源码替换统一由本 Skill 执行**：子 Skill 只返回代码文本，本 Skill 负责用 edit 工具替换源文件
- **源码修改优先使用 edit 工具**：先 read 读取源文件获取精确的 `old_string`，再用 edit 替换。仅当 edit 无法胜任时（如需要正则匹配删除空行、批量格式调整等），才使用 `sed`/`awk` 等 Bash 命令辅助
- 编译失败时不立即回退源文件：保留变更供下游 fix-code 修复，清理由编排器的优化点间清理协议统一处理
- **挑战由上游编排器独立执行**：子 Skill 不自行挑战，只记录 `error_message` 并返回 `status: "failed"`。AdversarialReview 作为独立阶段在 apply-optimization 之后由编排器统一分发
- `throughput-enhancement` 策略时，`skill_used` 为 `"loop-unrolling"`，`throughput_enhancement_result` 从 `unrolling_result` 提取：
  ```json
  {
    "original_parallelism": <unrolling_result.parallelism_before>,
    "target_parallelism": <unrolling_result.parallelism_after>,
    "unroll_factor": <unrolling_result.unroll_factor>,
    "simd_type": "<unrolling_result.intrinsics_type>"
  }
  ```
- `prefetch-optimization` 策略时，`skill_used` 为 `"prefetch-optimization"`，`prefetch_optimization_result` 从 `prefetch_result` 提取
- `register_pressure_result` 由下游 `verify-optimization` 填充；本阶段只透传 `input.diagnostics.register_pressure_analysis_required`
- `branch-elimination` 策略时，`skill_used` 为 `"branch-elimination"`，`branch_elimination_result` 从子 Skill 结果提取
- `memory-access-optimization` 策略时，`skill_used` 为 `"memory-access-optimization"`，`memory_access_result` 从子 Skill 结果提取
- `compiler-flag-tuning` 策略时，`skill_used` 为 `"compiler-flag-tuning"`，`compiler_flag_result` 从子 Skill 结果提取；`modified_files` 为构建配置文件而非源码
- `special-case-optimization` 策略时，`skill_used` 为 `"special-case-optimization"`，`special_case_result` 从子 Skill 结果提取
- `operation-fusion` 策略时，`skill_used` 为 `"operation-fusion"`，`operation_fusion_result` 从子 Skill 结果提取
- `precision-transform` 策略时，`skill_used` 为 `"precision-transform"`，`precision_transform_result` 从子 Skill 结果提取
- `autovec-source-transform` 策略时，`skill_used` 为 `"source-transform-autovec"`，`source_transform_result` 从子 Skill 结果提取；编译成功后必须采集一次 compiler vectorization feedback 或反汇编证据，失败时不进入第二轮修补
- `asm-optimization` 策略时，`skill_used` 为 `"asm-optimization"`，`asm_optimization_result` 从子 Skill 结果提取：
  ```json
  {
    "optimization_type": "<input.optimization_type>",
    "language": "<input.language>",
    "techniques_applied": ["<technique1>"],
    "instructions_before": <n>,
    "instructions_after": <n>,
    "details": {
      "pairs_merged": <n>,
      "post_index_count": <n>,
      "moves_eliminated": <n>,
      "unroll_factor": <n>,
      "prefetch_count": <n>
    }
  }
  ```
- `scalar-vector-hybrid` 策略时，`skill_used` 为 `"scalar-vector-hybrid"`，`scalar_vector_hybrid_result` 从子 Skill 结果提取：
  ```json
  {
    "scalar_vector_hybrid_result": {
      "success": true,
      "original_code": "...",
      "optimized_code": "...",
      "hybrid_decision": {
        "scalarized_sections": [...],
        "kept_vector_sections": [...],
        "data_movement": [...]
      },
      "estimated_overall_improvement_pct": 12,
      "caveats": [...],
      "error_message": ""
    }
  }
  ```
