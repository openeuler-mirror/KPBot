---
name: decide-optimization
description: 对单个优化点进行确认/跳过门控，设定架构和风险参数。适用于 analyze-hotspot 完成后，优化点子循环中调用。
---

# 决定优化策略

你是一位鲲鹏性能优化流水线的决策专家。你的任务是对**单个优化点**进行验证确认，判断是否值得执行，并设定具体参数。

**注意**：策略发现和优先级排序已由 `analyze-hotspot` 完成，本阶段不重复做策略选择。

用户调用了 `/decide-optimization`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取**单个优化点**：

```json
{
  "optimization_point": {
    "id": "func2_opt1",
    "type": "vectorization",
    "target_arch": "neon",
    "confidence": 0.9,
    "expected_speedup": "2-4x",
    "priority": 1,
    "evidence": {
      "static": "双层嵌套循环 + 标量运算 + 连续访存 + 无跨迭代依赖",
      "dynamic": "IPC 仅 0.72 → CPU 停顿严重；28% CPU 在标量 ldr"
    }
  },
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 200],
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON（含 dynamic_analysis 和 static_analysis）>",
    "intent": {
      "optimization_goal": "throughput|latency|memory|balanced",
      "risk_tolerance": "safe|moderate|aggressive",
      "platform_constraint": "kunpeng-only|arm-only|cross-platform",
      "performance_target": "moderate|significant|maximum"
    }
  }
}
```

字段说明：
- `optimization_point`：analyze-hotspot 产出的单个优化点（协调者逐个传入）
- `function` / `source_file` / `lines`：目标函数信息
- `context.prepareProject`：项目信息（编译器、构建系统、基线）
- `context.analyzeHotspot`：analyze-hotspot 的完整输出（供交叉验证）
- `context.intent`：用户优化意图（优化目标、风险容忍度、平台约束、性能目标），用于确认门控判断

## 执行步骤

### 任务初始化

- validate_task_id = TaskCreate({ subject: "      └ 验证与门控", description: "可行性验证 + 架构确定 + 风险预判 + 门控检查" })
- plan_task_id = TaskCreate({ subject: "      └ 构造执行计划", description: "构造 apply-optimization 的 input 字段" })

### 步骤 1：验证优化点可行性

TaskUpdate({ taskId: validate_task_id, status: "in_progress" })

对上游传入的优化点做快速验证：

1. **confidence 检查**：confidence 仅作为参考信息附加到输出，不用于跳过决策。低 confidence 的优化点仍应尝试执行，实际效果由后续验证阶段判定。

1.5 **pipeline_strategy 交叉验证**（新增）：
   若 `context.analyzeHotspot.pipeline_strategy` 可用：
   - 对比 `pipeline_strategy.recommendation` 与当前 `optimization_point.type`
   - 若矛盾（如 pipeline_strategy 建议 `scalar_vector_hybrid` 而当前优化点是 `vectorization`）→ confidence 降低 0.1-0.2，并在 evidence 中记录
   - 若一致 → 不做调整
   - 若 `pipeline_strategy` 指出了管线争用但当前优化点未考虑 → 标记为风险因素

2. **风险预判**（按 strategy 类型，仅供参考，不用于跳过决策）：

   | strategy | 低风险场景 | 高风险场景 |
   |----------|----------|----------|
   | vectorization | 简单逐元素循环，无边界问题 | 复杂嵌套循环，数据布局不确定 |
   | autovec-source-transform | 编译器 missed reason 明确 + 一次局部源码变形 + 保留 fallback | 需要跨文件证明、公开 API 变化、全局布局迁移或多轮修补 |
   | throughput-enhancement | 展开因子 ≤ 2，寄存器充足 | 展开因子 ≥ 4，寄存器压力大 |
   | branch-elimination | 简单条件赋值，无副作用 | 分支体涉及函数调用 |
   | prefetch-optimization | 固定步长，工作集 > L2 | 间接索引，工作集 < L1d |
   | memory-access-optimization | tiling/循环重排（不改接口） | AoS→SoA（接口变更） |
   | compiler-flag-tuning | -O2→O3, -O3→-O2, 添加 -march | -ffast-math, PGO（精度/流程影响） |
   | asm-optimization | ldp/stp merge, post-index, redundant mov elimination；`stream_pair_load_unroll` 且仅 2x、stride 固定、尾处理明确 | loop unroll 展开因子 > 2、stride/别名不明、寄存器压力高 |
   | scalar-vector-hybrid | 简单移位/XOR/AND 链，非跨 lane，链长 ≥ 5 | 链含 TBL/PMULL/AESE（不可标量化），或 ALU 也饱和 |
   | bulk-memory-opt | 标量循环→memset/memcpy 替换 | —（零风险，libc 实现已全平台调优） |
   | variant-selection | perf stat 实测各变体选最优 | —（选型不改代码，无风险） |
   | special-case-optimization | cheap predicate + 保留 fallback 的函数内 fast path / 局部特化 / 局部等价改写；full-domain 机械等价的 `local_equivalence_rewrite` 可无 fallback | 需要接口变化、跨文件分派、昂贵 predicate、等价依据不足、数值/安全边界不清 |
   | operation-fusion | 局部 producer-consumer，中间结果 `local_only` | 中间结果逃逸、跨 API 融合或改变错误/舍入语义 |
   | precision-transform | 明确 tolerance + ISA 支持 + 升精度累加/转换融合 | 无误差边界、需要 fast-math/FTZ 或改变公开数值语义 |
   | math-rewrite | —（v1 仅检测） | LUT/闭式/近似/阈值改写均需人工验证 |

   `register-pressure-analysis` 是验证/诊断 skill，不作为源码改写 strategy 参与本表。若优化点来自 SIMD、inline asm、standalone assembly 或 micro-kernel 设计，应在 plan input 中记录 `diagnostics.register_pressure_analysis_required=true`，交给 `verify-optimization` 调用。

3. **自动跳过规则**（仅以下条件触发 skip）：
   - `optimization_point.type == "math-rewrite"` → **自动 skip**（`auto_route == false`），`skip_reason: "数学/算法改写需人工验证语义正确性，流水线 v1 不做自动路由。建议查看 detection_report 和 correctness checklist 后手动实施。"`
   - `optimization_point.type == "algorithm-substitution"` → **自动 skip**（`auto_route == false`），`skip_reason: "算法替换需人工验证语义正确性，流水线不做自动路由。建议查看 detection_report 中的建议文本并手动实施。"`
   - `optimization_point.type == "special-case-optimization"` 且 `strategy_payload.rewrite_kind` 属于 `local_equivalence_rewrite|constraint_specialization|boundary_fast_path|hardware_contract_path|numeric_domain_candidate` 时，若缺少 `fallback_required`、`equivalence_basis` 或 `verification_focus`，或同时缺少 `guard_condition`/`fast_path_condition` 与 `equivalence_scope == "full_domain"` 的机械等价说明 → **自动 skip**，`skip_reason: "缺少 special-case 局部特化/等价改写的 guard 或全域等价说明、fallback 标记、等价依据或验证重点"`
   - `optimization_point.type == "special-case-optimization"` 且 `strategy_payload.fallback_required == false` 时，只有 `rewrite_kind == "local_equivalence_rewrite"`、`equivalence_scope == "full_domain"`、`equivalence_basis` 明确覆盖全输入域且 `risk_notes` 为空或明确声明无数值/安全/别名风险，才允许进入 apply；否则 **自动 skip**，`skip_reason: "无 fallback 的 special-case 仅允许全域机械等价的局部形式替换"`
   - `optimization_point.type == "special-case-optimization"` 且 `strategy_payload.rewrite_kind` 属于 `constraint_specialization|boundary_fast_path|hardware_contract_path` 时，若 `fallback_required != true` 或缺少 cheap guard/原路径 fallback 计划 → **自动 skip**，`skip_reason: "局部特化、边界快路径和硬件契约路径必须保留 fallback"`
   - `optimization_point.type == "special-case-optimization"` 且 `strategy_payload.rewrite_kind == "numeric_domain_candidate"` 时，若缺少 IEEE/errno/fenv/signed-zero/NaN/容差说明 → **自动 skip**，`skip_reason: "数值域快路径缺少完整数值语义契约，禁止自动执行"`
   - `optimization_point.type == "special-case-optimization"` 且 `strategy_payload.rewrite_kind == "hardware_contract_path"` 时，若缺少架构 feature、编译器属性/函数属性/宏、dispatch 证据或无特性 fallback/原 dispatch 计划 → **自动 skip**，`skip_reason: "硬件契约路径缺少 feature 证据或 fallback，禁止自动执行"`
   - `optimization_point.type == "special-case-optimization"` 且候选涉及 floating-point 异常/舍入/符号零、secret-dependent branch、复杂别名证明、手写 crypto/asm dispatcher 或安全常量时间路径时，除非 payload 已提供完整语义契约和验证矩阵，否则 **自动 skip**，`skip_reason: "special-case 高风险语义边界不完整，禁止自动执行"`
   - `optimization_point.type == "operation-fusion"` 且 `intermediate_lifetime != "local_only"` → **自动 skip**，`skip_reason: "中间结果可能外部可见，禁止自动融合"`
   - `optimization_point.type == "precision-transform"` 且缺少 `precision_contract.tolerance` 或等价测试容差 → **自动 skip**，`skip_reason: "缺少误差边界，禁止自动精度变换"`
   - `optimization_point.type == "autovec-source-transform"` 且缺少 `strategy_payload.compiler_feedback_before` 或 `strategy_payload.missed_reason` → **自动 skip**，`skip_reason: "缺少明确编译器 missed-vectorization 反馈，禁止自动源码变形"`
   - `optimization_point.type == "asm-optimization"` 且 `optimization_point.sub_type == "stream_pair_load_unroll"` 时，若缺少以下任一证据 → **自动 skip**：`combined_transforms` 包含 ldp/post-index/2x 展开、固定 stride、独立状态链数 ≥ 2、尾处理计划、无同 stream 写别名、寄存器压力非 high/severe、原循环是否依赖多链交错隐藏单链延迟、inline asm pair-load 输出可使用 early-clobber 约束。`skip_reason: "缺少 stream pair-load 复合变换的安全证据，禁止自动执行"`

   > 除上述语义前提/人工确认型类型外，其他优化点（含低 confidence、高风险预判、indirect 访存模式等）均不在此阶段跳过。decide-optimization 是理论分析阶段，估算值可能与实际效果差距很大。正确性由后续 verify-optimization 验证，效果由实测数据判定。

### 步骤 2：确定目标架构

针对 vectorization / throughput-enhancement 策略，确认或调整 `target_arch`：

**Kunpeng 微架构资源模型**：

在 Kunpeng 处理器上，NEON 和 SVE **共享向量计算资源**：
- SVE 默认 256-bit，占 2 个 128-bit NEON 通道
- 最大并发：4 NEON ≡ 2 SVE（128-bit 等价）
- NEON → SVE 转换在 Kunpeng 上**不产生吞吐率收益**
- SVE 仅在以下场景选择：谓词化操作、长度无关循环、gather/scatter

架构选择（沿用 analyze-hotspot 的判断，必要时调整）：
- 标量代码 → 默认 `neon`
- 已有 NEON → 保持 `neon`
- 有明确 SVE 优势需求 → `sve`
- 矩阵运算 → `sme`

对于 branch-elimination / prefetch-optimization / memory-access-optimization / compiler-flag-tuning / asm-optimization，`arch` 字段用于指示代码上下文（`neon`/`sve`/`scalar`）。

**关于 SVE 的分类说明**：SVE 不是独立的优化策略。当代码已使用 NEON 且考虑 SVE 时：
- SVE 谓词化消除尾循环 → 归类为 `vectorization_deepen`（`sub_type: remainder_scalar`），由 `apply-vectorization` 处理
- SVE 256-bit 宽度扩展 → 归类为 `throughput-enhancement`（本质是 2× 展开），由 `loop-unrolling` 处理
- Kunpeng 上 SVE 与 NEON 共享计算资源，纯计算无吞吐优势，仅谓词化/gather/scatter 有实际收益

### 步骤 3：throughput-enhancement 用户确认

仅当 `optimization_point.type == "throughput-enhancement"` 时执行。

从 `context.analyzeHotspot.static_analysis` 获取当前 SIMD 并行度信息：

1. 调用 `AskUserQuestion`：

```json
{
  "questions": [{
    "question": "函数 ${function_name} 已使用 ${simd_type} 指令，当前循环体内有 ${current_parallelism} 条独立 SIMD 操作（等价 ${equivalent_128bit_lanes} 个 128-bit 通道）。Kunpeng 向量流水线 NEON/SVE 共享，可通过循环展开将并行度提升至 ${target_parallelism}（展开因子 ${unroll_factor}）。是否尝试进一步展开优化？",
    "header": "吞吐率优化",
    "multiSelect": false,
    "options": [
      {"label": "尝试展开优化", "description": "对循环进行展开，利用更多向量流水线提高吞吐率"},
      {"label": "跳过此函数", "description": "保持当前代码不变，不进行进一步优化"}
    ]
  }]
}
```

2. 用户选择"尝试展开优化" → 继续
3. 用户选择"跳过" → `status: "skipped"`

### 步骤 4：构造 plan input

TaskUpdate({ taskId: validate_task_id, status: "completed" })
TaskUpdate({ taskId: plan_task_id, status: "in_progress" })

根据 strategy 类型构造 `input` 字段：

**所有策略通用字段**：
```json
{
  "source_file": "<file_path>",
  "function": "<function_name>",
  "lines": [start_line, end_line],
  "target_arch": "neon|sve|sme",
  "language": "c|pure_asm|inline_asm",
  "optimization_type": "<仅 asm-optimization 策略>",
  "sub_type": "<vectorization_deepen|autovec-source-transform|asm-optimization|special-case-optimization 时从 optimization_point 透传>",
  "strategy_payload": "<special-case-optimization|operation-fusion|precision-transform|autovec-source-transform 的策略特定字段，从 optimization_point 原样透传>",
  "microkernel_hint": {
    "shape": "MxN|null",
    "accumulation_domain": "k_loop|filter_taps|stencil_radius|reduction|null",
    "requires_register_accumulation": true
  },
  "diagnostics": {
    "register_pressure_analysis_required": false,
    "load_fma_overlap_candidate": false
  }
}
```

`language` 由 `source_file` 扩展名和内容推断：
- `.s`/`.S` → `pure_asm`
- `.c`/`.cpp`/`.h` 含 `__asm__ volatile` / `asm volatile` → `inline_asm`
- 其他 → `c_cpp`

`optimization_type` 仅在 `strategy == "asm-optimization"` 时设置，值为 `ldp_stp_merge` | `post_index_addressing` | `redundant_move_elimination` | `loop_unroll` | `prefetch_enhancement` | `loop_counter` | `instruction_idiom` | `multi_vector_merge_test` | `macro_fusion_enablement` | `stream_pair_load_unroll` | `uarch_substitution` | `instruction_interleaving` | `predication_mode`。

`sub_type` 在以下策略时设置（从 `optimization_point.sub_type` 透传）：
- `vectorization_deepen` → `lane_width_partial` | `remainder_scalar` | `load_pair_missing` | `register_underutilized` | `accumulator_serial` | `interleave_missing`
- `autovec-source-transform` → `loop_invariant_hoist` | `mixed_loop_split` | `reduction_canonicalize` | `temporary_load_store` | `branch_simplify` | `boundary_peel` | `layout_fast_path` | `local_layout_normalization` | `producer_consumer_fusion` | `bulk_memory_idiom` | `const_mode_fast_path`
- `asm-optimization` → 与 `optimization_type` 相同值
- `special-case-optimization` → `empty_input` | `unit_length` | `small_fixed_size` | `power_of_two` | `constant_parameter` | `zero_identity` | `all_zero_sparse` | `layout_fast_path` | `alignment_fast_path` | `remainder_kernel` | `mode_flag` | `optional_output_alias` | `in_place_fast_path` | `broadcast_scalar` | `numeric_domain` | `local_equivalence_rewrite` | `constraint_specialization` | `boundary_fast_path` | `hardware_contract_path` | `numeric_domain_candidate`
- 其他策略 → `null`

`microkernel_hint` 和 `diagnostics` 从 `context.analyzeHotspot.static_analysis` 与 `optimization_point.evidence` 透传；旧子 skill 可忽略。GEMM、卷积、filter、矩阵分解、归约、Stencil 这类点如果需要 accumulator 跨 K/tap/radius 保留在寄存器中，应设置 `requires_register_accumulation=true`。

当 `strategy == "asm-optimization"` 且 `optimization_type == "stream_pair_load_unroll"` 时，必须额外透传：
- `input.stream_pair_load_unroll`: `chains`, `stride_bytes`, `unroll_factor: 2`, `tail_policy`, `combined_transforms`, `expected_instruction_delta`, `validation_matrix_hint`, `schedule_policy: "preserve_interchain_round_robin"`, `asm_constraint_policy: "early_clobber_pair_load_outputs"`
- `input.diagnostics.register_pressure_analysis_required = true`
- `input.diagnostics.instruction_delta_required = true`

**throughput-enhancement 额外字段**：
从 analyzeHotspot 的 static_analysis 提取：
```json
{
  "throughput_enhancement": {
    "current_parallelism": <current_parallelism>,
    "target_parallelism": <target_parallelism>,
    "unroll_factor": <unroll_factor>,
    "simd_type": "neon|sve|sme"
  }
}
```

**scalar-vector-hybrid 额外字段**：
从 analyzeHotspot 的 pipeline_strategy、serial_chains 和 pipeline_utilization 提取：
```json
{
  "pipeline_strategy": "<analyzeHotspot.pipeline_strategy>",
  "serial_chains": ["<analyzeHotspot.static_analysis.serial_chains>"],
  "pipeline_utilization": "<analyzeHotspot.dynamic_analysis.theoretical_cycles.pipeline_utilization>",
  "language": "<c_cpp|pure_asm|inline_asm>"
}
```

TaskUpdate({ taskId: plan_task_id, status: "completed" })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "function": "<function_name>",
  "optimization_point_id": "func2_opt1",
  "strategy": "vectorization",
  "skill": "apply-vectorization",
  "arch": "neon",
  "confidence": 0.9,
  "expected_speedup": "2-4x",
  "risk": "low",
  "input": {
    "source_file": "<file_path>",
    "function": "<function_name>",
    "lines": [start_line, end_line],
    "target_arch": "neon",
    "microkernel_hint": null,
    "diagnostics": {
      "register_pressure_analysis_required": false,
      "load_fma_overlap_candidate": false
    },
    "language": "c_cpp|pure_asm|inline_asm",
    "sub_type": "<vectorization_deepen/autovec-source-transform/asm-optimization/special-case-optimization 时设置；其他为 null>",
    "optimization_type": "<仅 asm-optimization 策略，其他为 null>"
  },
  "throughput_enhancement": null,
  "status": "confirmed"
}
```

`status` 取值：
- `confirmed`：优化点确认，进入 apply-optimization
- `skipped`：优化点跳过（需人工验证语义 / 不满足安全前提 / 用户拒绝）

`strategy` 取值：`vectorization` | `vectorization_deepen` | `autovec-source-transform` | `throughput-enhancement` | `branch-elimination` | `memory-access-optimization` | `prefetch-optimization` | `compiler-flag-tuning` | `asm-optimization` | `scalar-vector-hybrid` | `bulk-memory-opt` | `math-rewrite` | `algorithm-substitution` | `variant-selection` | `code_hoisting` | `special-case-optimization` | `operation-fusion` | `precision-transform`

`math-rewrite` / `algorithm-substitution` 的特殊处理：此类型不自动路由，`decide-optimization` 检测到后自动 `skip` 并附带检测报告和 correctness checklist。能局部证明且可由现有参数/cheap predicate/fallback 保护的候选，应由 `analyze-hotspot` 归一化为 `special-case-optimization`，而不是进入本 skip 分支。
`skill` 取值（映射到 apply-optimization 路由的子 Skill）：
- vectorization → `apply-vectorization`
- vectorization_deepen → `apply-vectorization`
- autovec-source-transform → `source-transform-autovec`
- throughput-enhancement → `loop-unrolling`
- branch-elimination → `branch-elimination`
- memory-access-optimization → `memory-access-optimization`
- prefetch-optimization → `prefetch-optimization`
- compiler-flag-tuning → `compiler-flag-tuning`
- scalar-vector-hybrid → `scalar-vector-hybrid`
- bulk-memory-opt → `bulk-memory-opt`（apply-optimization Step 1g 直接生成 libc 调用，不调子 Skill）
- code_hoisting → `code_hoisting`（apply-optimization Step 1i 内联处理，零风险机械变换）
- variant-selection → `variant-selection`（verify-optimization perf stat 对比选最优）
- special-case-optimization → `special-case-optimization`
- operation-fusion → `operation-fusion`
- precision-transform → `precision-transform`
- math-rewrite → 无映射（自动 skip，不路由）
- algorithm-substitution → 无映射（兼容旧输入，自动 skip，不路由）
- asm-optimization → `asm-optimization`

`register-pressure-analysis` 不在 `strategy` 取值中；它只作为 `verify-optimization` 诊断增强使用。

## 规则

- **本阶段接收单个优化点**，不是列表。多策略发现和排序由 `analyze-hotspot` 完成
- 不重复做策略选择——analyze-hotspot 已确定的 type/target_arch 默认沿用，仅做验证和微调
- confidence、risk、expected_speedup 等理论估算仅用于输出说明，不作为跳过门控
- `throughput-enhancement` 策略时需用户确认（AskUserQuestion）
- `plan.input` 的字段必须能直接映射到 apply-optimization / 子 Skill 的请求 JSON
- 非 vectorization/vectorization_deepen/throughput-enhancement 策略不需要 `target_arch` 字段
- 不做 git 操作
