---
name: special-case-optimization
description: 为通用热点函数生成特殊情况快路径、局部特化和局部等价改写，包括边界快路径、固定约束专用路径、形式替换、硬件契约路径和数值域候选。适用于 apply-optimization 调用。
---

# 特殊情况与局部特化优化

你是一位通用 kernel 局部特化专家。你的任务是在不改变公开语义的前提下，为热点函数生成 cheap predicate + fast path / local rewrite + 原 fallback 的最小代码改动。

本 Skill 是 `special-case-optimization` 的唯一执行中心。不要新增策略名、不要按库名/函数名/benchmark 名路由，也不要把局部等价改写拆到独立 Skill。所有此类机会都应通过 `strategy_payload` 描述为通用能力族。

## 输入

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "sub_type": "empty_input|unit_length|small_fixed_size|power_of_two|constant_parameter|zero_identity|all_zero_sparse|layout_fast_path|alignment_fast_path|remainder_kernel|mode_flag|optional_output_alias|in_place_fast_path|broadcast_scalar|numeric_domain|local_equivalence_rewrite|constraint_specialization|boundary_fast_path|hardware_contract_path|numeric_domain_candidate",
  "strategy_payload": {
    "rewrite_kind": "boundary_fast_path|constraint_specialization|local_equivalence_rewrite|hardware_contract_path|numeric_domain_candidate",
    "fast_path_condition": "<兼容旧字段：条件表达式或证据>",
    "guard_condition": "<cheap predicate 或局部证明>",
    "fallback_required": true,
    "equivalence_scope": "guarded_path|full_domain",
    "equivalence_basis": "<局部语义等价依据>",
    "risk_notes": "<numeric/security/aliasing/hardware 风险>",
    "verification_focus": ["guard hit", "fallback hit", "boundary cases"]
  },
  "context": {
    "prepareProject": {},
    "analyzeHotspot": {}
  }
}
```

## 执行步骤

1. Read `source_file` 的目标函数和调用上下文，确认候选属于函数内局部改写或局部特化。
2. 读取 `strategy_payload.rewrite_kind`，按下方“集中能力族”选择执行规则；旧输入缺少 `rewrite_kind` 时，根据 `sub_type` 映射到最接近的能力族。
3. 确认 guard 或局部证明可由已有参数、调用点、编译期常量、架构证据或 cheap predicate 支撑；不得为了触发快路径全量扫描热输入。
4. 默认保留原逻辑作为 fallback。只有 `rewrite_kind == "local_equivalence_rewrite"`、`equivalence_scope == "full_domain"`、局部机械等价完全明确且无数值/安全/别名风险时，才允许无 fallback 替换，并必须写明 `fallback_preserved=false` 的理由。
5. 遇到以下情况返回 `success=false`：需要改公开接口、跨文件分派、全局状态、复杂别名证明、昂贵 predicate、硬件特性无证据、或无法保留必要 fallback。
6. 生成 `optimized_code`，并在 `guard_condition`、`fallback_preserved`、`equivalence_basis`、`validation_focus` 中解释保护条件和验证重点。

## 集中能力族

| rewrite_kind | 适用场景 | 执行边界 | 默认行为 |
|---|---|---|---|
| `boundary_fast_path` | 空输入、短输入、固定小规模、尾块、整块路径、setup 成本占比高 | guard 必须来自参数或已有元数据 | 生成 guard + fast path + fallback |
| `constraint_specialization` | 尺寸、模式、布局、数据类型、步长、块大小等约束稳定 | 约束必须可局部证明，不能猜测调用分布 | 生成专用路径，fallback 覆盖通用路径 |
| `local_equivalence_rewrite` | 查表、位运算、算术表达式、指令惯用法等形式可互换 | 只允许局部机械等价；涉及溢出、舍入、异常语义时拒绝 | 优先最小替换；高风险时只返回候选失败 |
| `hardware_contract_path` | 架构能力明确时启用更合适的指令、访存或函数属性 | 必须有 feature/编译器/dispatch 证据，并保留无特性 fallback | 生成受保护路径或提示交由 compiler/variant 相关策略 |
| `numeric_domain_candidate` | NaN/Inf/zero/saturation/阈值等数值域特例 | 必须明确 IEEE、errno/fenv、signed zero、NaN payload 和测试容差 | 默认 `success=false` 只报告候选；证据完整才允许 fast path |

旧 `sub_type` 与能力族映射：
- `empty_input`、`unit_length`、`small_fixed_size`、`remainder_kernel` → `boundary_fast_path`
- `power_of_two`、`constant_parameter`、`layout_fast_path`、`alignment_fast_path`、`mode_flag`、`broadcast_scalar`、`optional_output_alias`、`in_place_fast_path` → `constraint_specialization`
- `zero_identity`、`all_zero_sparse` → `constraint_specialization`；若需要数据相关扫描则拒绝
- `numeric_domain` → `numeric_domain_candidate`

## 通用执行配方

以下配方只描述源码结构和语义契约，不按库名、函数名或 benchmark 名触发。每个配方都必须返回最小局部改动；若无法满足拒绝条件之外的安全前提，返回 `success=false` 并保留候选说明。

| recipe | 触发条件 | 允许动作 | 拒绝条件 | 验证重点 |
|---|---|---|---|---|
| `constant_table_to_immediate` | 小型固定表仅在当前函数内服务 mask、shift、rotate、byte lane、bit interleave/deinterleave；表项无运行时变化，索引范围固定 | 删除或旁路常量表，把表项替换为立即数、`constexpr`、局部宏、内联常量或等价位运算；可加 always-inline 属性但不改公开 API | 移位宽度、整数溢出、符号扩展、别名读取或表地址外部可见不明确 | 等价输入域边界、极值、随机样本；`equivalence_scope` 可为 `full_domain` |
| `simd_idiom_rewrite` | shuffle、temporary array、splat/set、counter construction、rotate 等 SIMD 形式可由同 ISA 更直接表达；lane 映射固定 | 使用等价 intrinsic、shift-insert、reinterpret、dup/combine 或直接 vector construction；只替换局部 idiom | lane 映射无法逐 lane 证明、依赖未定义对齐、改变异常/舍入/饱和语义、寄存器压力明显升高 | lane mapping、边界 lane、反汇编关键指令、targeted+aggregate benchmark |
| `delegation_to_specialized_impl` | ISA/template/feature specialization 最终委托 scalar/base/NONE，且 specialization 的数据类型、尺寸或布局在入口处可得 | 在 specialization 内生成局部专用循环/helper，保留原 scalar/base fallback；可对小集合固定尺寸拆分分支 | 需要改变模板公开接口、跨文件重排 dispatch、复杂别名证明或多轮重构 | specialized guard hit、fallback hit、多个尺寸；记录 per-case regression |
| `dispatch_constraint_path` | 已有 dispatch/switch/if 提供 dtype、layout、mode、block size、interpolation、metric 等稳定枚举，热组合仍走通用慢路径 | 在 dispatch 附近或目标函数入口加窄作用域 fast path；只处理被 guard 精确覆盖的组合，其他路径保持原实现 | guard 来自猜测分布、需要全局布局迁移、需要改公开 API 或会改变边界/填充语义 | 被 guard 组合的 targeted benchmark、其他组合 aggregate/fallback、边界模式 |
| `fixed_block_group_path` | public `len`/`nblocks`/`num_inputs` 可 cheap 判断大输入，算法天然按固定 block 处理，已有 tail/small 处理 | 加 `len >= threshold`、整块数或固定组数 fast path；按 2/4/8 block group 展开或专用化，尾部/小输入走原逻辑 | guard 依赖 secret data、counter/状态推进边界不清、溢出/回绕不明、破坏 constant-time 约束 | guard hit、guard miss、threshold-1/threshold/threshold+1、tail、状态/计数器边界 |
| `hardware_contract_attribute` | 目标架构或 dispatch 明确支持 feature，但源码因宏、编译属性或构建 flag 未启用对应指令/访存契约 | 使用 source-local function attribute、受保护分支、已有 feature macro 分流或保留原 dispatch；优先局部属性，不强推全局 flag | 无 feature 证据、编译器不支持属性、无 fallback/原 dispatch 计划、会在非目标机器非法指令 | feature 命中路径、无 feature 计划、编译日志/宏/反汇编证据、targeted+aggregate benchmark |

配方选择规则：
- `constant_table_to_immediate` 和 `simd_idiom_rewrite` 归入 `local_equivalence_rewrite`；只有 full-domain 等价明确时才允许 `fallback_preserved=false`。
- `delegation_to_specialized_impl` 和 `dispatch_constraint_path` 归入 `constraint_specialization`，默认必须保留 fallback。
- `fixed_block_group_path` 归入 `boundary_fast_path`，guard 只能依赖 public 参数或已有元数据。
- `hardware_contract_attribute` 归入 `hardware_contract_path`，必须记录 feature/编译器/dispatch 证据。

## 通用示例库

以下例子来自常见高性能库形态，只作为泛化模式；不要把领域或仓库名写进路由条件。

| 场景 | sub_type | rewrite_kind | guard condition 示例 | fast path / rewrite 思路 | 注意事项 |
|---|---|---|---|---|---|
| 空工作/空描述符 | `empty_input` | `boundary_fast_path` | `n == 0`、`desc.size() == 0`、`shape.total_size() == 0` | 直接 return，或返回空 view/空结果 | 必须确认没有计数器、状态推进、错误码等副作用 |
| 单元素/1x1 | `unit_length` | `boundary_fast_path` | `n == 1`、`rows == 1 && cols == 1` | 用闭式结果或极小路径替代通用分解/迭代 | 注意初始化输出和单位矩阵语义 |
| 短长度回退 | `small_fixed_size` | `boundary_fast_path` | `len < vector_width`、`len < block_size` | 避免向量/多路 setup，直接调用 scalar/base 路径 | 若短长度很少出现，不要污染主路径 |
| 整块/尾块分离 | `remainder_kernel` | `boundary_fast_path` | `len % block != 0`、`eob <= threshold` | full-block 主循环加 tail/低 eob 专用路径 | 覆盖 0、1、block-1、block、block+1 测试 |
| 全零/无非零/稀疏已知 | `all_zero_sparse` | `constraint_specialization` | `non_zero_count == 0`、`eob == 0`、已有 sparse metadata | 跳过 residual/transform/乘加，只更新必要元数据或输出零 | 不为了判断全零扫描热输入；涉密数据禁止数据相关提前退出 |
| identity/no-op 操作 | `zero_identity` | `constraint_specialization` | `transform == identity`、`scale == 1`、`add == 0`、`apply_filter == false` | 转为 copy/move/ref，或跳过 transform/filter | 需确认 alias、引用计数和边界填充语义 |
| 模式/算法常量 | `mode_flag` | `constraint_specialization` | `metric == L2`、`mode == real`、`layout == NHWC`、`flag` 固定 | 拆成专用分支，移除热循环内 mode 判断 | guard 应来自参数、调用点或稳定配置 |
| 对齐路径 | `alignment_fast_path` | `constraint_specialization` | `is_aligned_pointer(p)`、`((uintptr_t)p & 15) == 0` | 使用 aligned load/store 或更宽访存，保留 unaligned fallback | 不用不可靠的 `assume_aligned` 制造 UB |
| 连续/可 squashed layout | `layout_fast_path` | `constraint_specialization` | `stride == element_size`、低维 stride 连续、无 padding | 把多维 window 视作 1D tight loop | fallback 覆盖跨步、padding、broadcast |
| direct vs indirect 指针 | `layout_fast_path` | `constraint_specialization` | `!is_indirect`、直接 stride 可用 | 走直接指针递增，跳过指针表读取 | 间接输入/输出必须保留 fallback |
| 可选输出别名 | `optional_output_alias` | `constraint_specialization` | `rx == NULL && tx != NULL` 或 `out1 == NULL` | 将缺省输出路由到已有输出，或跳过未请求输出 | 只有 API 明确允许 optional output 才能做 |
| 原地计算资格 | `in_place_fast_path` | `constraint_specialization` | 单一消费者、无 accessor、shape/quant info 相同、`stride == 1`、无 padding | 复用输入 buffer 作为输出，避免临时分配和 copy | 需要证明外部不可见中间状态 |
| broadcast/scalar 操作数 | `broadcast_scalar` | `constraint_specialization` | `dim == 1`、`broadcast_shape` 有效、标量参数 | 标量 load once/splat，避免按元素重复索引 | broadcast 不兼容时不能默默 fallback 为错误结果 |
| 局部形式替换 | `local_equivalence_rewrite` | `local_equivalence_rewrite` | 固定 mask/shift、固定表大小、等价表达式输入不变 | 在函数内替换为更少指令、更少访存或更直接的表达式 | 必须证明溢出、别名、舍入和副作用不变 |
| 硬件契约路径 | `hardware_contract_path` | `hardware_contract_path` | ISA feature、编译属性、dispatch 证据明确 | 在受保护分支中使用更合适的指令/访存/函数属性 | 无 feature 路径必须保留 |
| 精确数值域 | `numeric_domain` | `numeric_domain_candidate` | `isnan(x)`、`isinf(x)`、`x == 0`、`abs(x) > threshold` | 返回 IEEE 规定常量、饱和值或闭式边界结果 | 默认高风险；需保留 NaN、符号零、errno/fenv 语义 |

## GEMM 示例库

以下只作为示例，不作为专用路由名。触发仍然必须来自通用 `sub_type`，并且必须保留 fallback。

| GEMM 场景 | sub_type | guard condition 示例 | fast path 思路 | 注意事项 |
|---|---|---|---|---|
| 空输出矩阵 | `empty_input` | `M == 0 || N == 0` | 直接 return，不触碰 C | `K == 0` 不等价于直接 return，仍需处理 `beta * C` |
| `alpha == 0` | `constant_parameter` | `alpha == 0` | 跳过 A/B 乘积，只处理 `C = beta * C` | `beta == 1` 可 return；`beta == 0` 可清零 C；其他 beta 需 scale C |
| `beta == 0` | `constant_parameter` | `beta == 0` | 不 load 旧 C，直接写 `alpha * A * B` | 可减少一次 C 读和一次 FMA；需确认 NaN/异常语义是否可接受 |
| `beta == 1` | `constant_parameter` | `beta == 1` | 跳过 beta 乘法，执行 `C += alpha * A * B` | 保留非 1 fallback |
| `alpha == 1` 或 `alpha == -1` | `constant_parameter` | `alpha == 1` / `alpha == -1` | 跳过 alpha 乘法，或把加法变成减法 | 浮点异常/符号零敏感时需保守 |
| 小矩阵 | `small_fixed_size` | `M <= 4 && N <= 4` 或固定 shape | 生成小尺寸直写/展开路径 | 不要污染大矩阵主路径；fallback 保留原 kernel |
| MR/NR 尾块 | `remainder_kernel` | `M % MR != 0 || N % NR != 0` | 为 M/N tail 生成小 remainder kernel | 只处理边界块；完整块仍走主 kernel |
| K 尾部 | `remainder_kernel` | `K % KC != 0` 或 `K < KC` | 小 K/tail K 专用路径，减少通用 pack/loop 开销 | 如果会改变归约顺序，需验证误差 |
| 2 的幂尺寸/步长 | `power_of_two` | `(K & (K - 1)) == 0` 或 `ldc` 为 2 的幂 | 用更简单的地址计算、mask、对齐假设或无尾路径 | 2 的幂也可能导致 cache 冲突，必须 benchmark |
| 连续布局 | `layout_fast_path` | `lda == K && ldb == N && ldc == N` 或 layout flag 表明 contiguous | 拆出 contiguous row/column-major fast path | 非连续 stride 必须回 fallback |
| column-major specialization | `layout_fast_path` | `layout == ColumnMajor` 或调用点固定 | 避免每次分支 layout，生成列主序路径 | 不能猜测布局；必须来自参数/调用点/测试 |
| A 或 B 为零矩阵 | `zero_identity` | 显式 flag、metadata 或调用点证明 zero | 跳过乘积，只处理 `beta * C` | 不要为了判断 zero 扫描整个矩阵，除非已有 cheap 标志 |
| A 或 B 为单位矩阵 | `zero_identity` | 显式 flag、metadata 或固定调用点 | `A=I` 时近似转为 `C = alpha*B + beta*C`；`B=I` 类似 | 维度和 layout 必须匹配；保留 fallback |
| 对角矩阵 | `zero_identity` | 显式 diagonal flag 或压缩对角存储 | A 对角时按行缩放 B；B 对角时按列缩放 A | 不要从 dense 矩阵运行时全量扫描判断 |
| 对称矩阵 | `zero_identity` | 显式 symmetric flag 或专用调用点 | 选择只读一侧或转到对称路径 | GEMM 语义下不能随意假设对称；需要接口或调用点证明 |

## 输出

```json
{
  "special_case_result": {
    "success": true,
    "sub_type": "<sub_type>",
    "rewrite_kind": "<strategy_payload.rewrite_kind 或根据 sub_type 推断>",
    "guard_condition": "<cheap predicate>",
    "fallback_preserved": true,
    "equivalence_scope": "guarded_path|full_domain",
    "equivalence_basis": "<局部语义等价依据>",
    "original_code": "<原始函数或片段>",
    "optimized_code": "<加入 fast path 后的代码>",
    "modified_region": "function|loop|branch",
    "validation_focus": ["fast path 命中", "fallback 命中", "边界尾部"],
    "risk_notes": "<保留的风险或拒绝原因>",
    "error_message": ""
  }
}
```

## Compatibility

- Compatible with the existing kpbot-code-optimizer/apply-optimization v1 contract: callers read `special_case_result.success`, `original_code`, `optimized_code`, `fallback_preserved`, `rewrite_kind`, `equivalence_scope`, `equivalence_basis`, `risk_notes`, and `validation_focus`.
- This skill does not introduce a new top-level status, score, or grading field. Failures remain `special_case_result.success=false` with `error_message`, which apply-optimization maps to `status: "failed"`.
- New strategy details must stay under `strategy_payload` or `special_case_result`; adding required top-level fields is a breaking change and requires updating kpbot-code-optimizer prompt contracts first.

## 规则

- 不做源码写入；只返回原始文本和优化文本。
- 不为特定领域命名策略；领域只能作为示例证据。
- fast path 条件必须可局部证明，且 fallback 必须保持原语义。
- `fallback_required == false` 只允许用于 `local_equivalence_rewrite` + `equivalence_scope == "full_domain"` + 无数值/安全/别名风险；旧输入缺少 `equivalence_scope` 时按 `guarded_path` 处理。
- 优先生成最小改动；不能为了小规模路径重构整个函数。
- 不修改 benchmark/test harness 来制造性能提升，除非输入目标明确声明正在优化 benchmark/test harness 本身。
- 不根据库名、函数名、benchmark 名触发规则；触发必须来自结构特征和语义契约。
- `strategy_payload.equivalence_basis` 为空时，不允许执行 `local_equivalence_rewrite`。
- `hardware_contract_path` 必须有明确架构/编译器/dispatch 证据；否则返回 `success=false`。
- 对安全/密码学/认证比较等 constant-time 代码，不允许引入依赖秘密数据的提前返回或分支。
- 数值域快路径只在 IEEE、异常、舍入、符号零和测试容差均明确时应用；否则只报告候选。
