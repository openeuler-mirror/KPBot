---
name: apply-vectorization
description: 将 C/C++ 或 AArch64 汇编中的循环评估并改写为 ARM NEON、SVE 或 SME 向量化方案，输出规范化 JSON 结果。适用于实现或评审 apply-vectorization 及旧版 vectorize-loop 流程、把候选循环转换为 NEON intrinsics、inline asm、standalone assembly、SVE 谓词化循环、sum/dot reduction 或 SME streaming / ZA-gated 代码，判断循环是否可安全向量化，或生成“主向量循环 + 尾处理” 的 ARM 优化代码草案。适用于 Codex、Claude Code、OpenCode 等支持目录式 SKILL.md 的代理环境。对普通递推、副作用、不规则访存和不支持的数据类型应明确拒绝；已存在 intrinsics、SIMD 或汇编时先按源码形态分类，不直接退出；sum/dot reduction 只在满足本 skill 的专门规则时允许。
---

# 应用向量化

## 路径约定

本 skill 的脚本使用相对路径。执行前必须先定位 skill 目录：

**自动定位方式（推荐）**：
```bash
# 通过脚本自身位置推导 skill 目录
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
```

**或在 skill invoked 时使用**：
```bash
cd .opencode/skills/apply-vectorization
bash scripts/detect_isa_features.sh
python3 scripts/query_arm_intrinsics.py ...
```

如果该 skill 被复制到目标项目的其他 skill 目录，请进入包含本 `SKILL.md` 的目录后再运行脚本；所有 docs、references、scripts 和 assets 都按本目录相对路径解析。

Python 脚本已内置路径定位（使用 `Path(__file__).resolve().parent`），可直接调用。

## 适用范围

这个 skill 只负责两件事：

- 判断给定 request 是否值得、且是否能够安全向量化
- 在满足前置条件后，生成 canonical `vectorization_result`

明确不放弃优化的场景：

- 已经出现 SIMD 指令、intrinsics、inline asm、standalone assembly，或源码只做了部分向量化，都不是拒绝优化的理由
- 编译器生成汇编里出现 NEON/SVE/SME 指令，只能作为 benchmark 对比证据；不得以“编译器已自动向量化”作为 `success=false` 或跳过手写向量化的理由
- 对部分向量化代码，必须继续检查未覆盖的标量循环、标量尾处理、未向量化的内层热点、低并行度累加链、访存布局、寄存器压力、约束/clobber 和 ABI 边界
- 只有命中语义安全拒绝条件、源码形态与显式 `codegen_style` 冲突、目标 ISA 不支持，或无法完成必要编译/链接验证时，才允许放弃生成优化结果

它不负责：

- 直接改 Git
- 直接提交 benchmark 结论
- 在 request 缺失、环境不支持、或语义不清时硬造代码
- 用弱符号 stub、伪运行时或“远端应该有”来掩盖编译、链接或 ABI 问题

## 先读什么

优先顺序已经改成”结构化知识库优先”，不要再先翻散文文档：

### 必读文档（任何向量化任务都需要）

- 结构化入口：`references/arm_intrinsics_db/index.json`
- 多级手册入口：`docs/arm-intrinsics-manual/README.md`
- 输入输出契约：`references/input-output-contract.md`
- 代码生成形态选择：`docs/codegen-style-guide.md`
- register accumulation：`docs/register-accumulation-guide.md`
- micro-kernel 设计：`docs/microkernel-design-guide.md`
- 寄存器分配策略器：`scripts/select_register_allocation.py`
- SME `ZA` inline asm 主参考：`docs/sme-za-inline-asm-guide.md`
- SME `ZA/tile` 决策：`docs/sme-za-tile-guide.md`
- reduction 决策：`docs/reduction-guide.md`
- 来源与刷新策略：`docs/arm-official-source-map.md`
- 使用入口：`docs/arm-isa-usage-guide.md`
- 脚本链路：`scripts/README.md`

### 条件读取文档（按场景需要）

- **SME ZA 路径**：先读 `docs/sme-za-tile-guide.md`（ZA/tile 决策），若需生成 ZA inline asm 再读 `docs/sme-za-inline-asm-guide.md`
- **Reduction 模式**：`docs/reduction-guide.md`（sum/dot 归约循环）
- **ACLE 源码争议**：`docs/arm-official-source-map.md`（确认 intrinsic/attribute 权威来源时）
- **集成验证**：`references/integration-and-verification.md`（代码生成完成后读取，用于集成与验证边界检查）
- **脚本调试**：`scripts/README.md`（调试脚本链路时读取）
- **ISA 用法参考**：`docs/arm-isa-usage-guide.md`（查询 ISA 使用规范时）

### 复杂模式扩展文档

- 间接寻址处理：`docs/indirect-addressing-handling.md`
- SVE 通算场景：`docs/sve-general-compute-guide.md`
- 多层循环向量化：`docs/multi-loop-vectorization.md`
- 算子模式识别：`docs/operator-patterns.md`
- NEON 汇编模板：`docs/neon-asm-patterns.md`
- 宽度跳转表：`docs/width-dispatch-patterns.md`
- 计算密集型 kernel：`docs/register-accumulation-guide.md`、`docs/microkernel-design-guide.md`

如果你需要确认某个 intrinsic、instruction、attribute 或代码片段是否符合当前规则，优先执行：

```bash
python3 scripts/query_arm_intrinsics.py lookup --name vld1q_f32
python3 scripts/query_arm_intrinsics.py lookup --instruction FMOPA --isa sme
python3 scripts/query_arm_intrinsics.py lookup --instruction PMULL --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group crypto --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group compression --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group inline-asm --isa sme --json
python3 scripts/query_arm_intrinsics.py validate-snippet --file ./candidate.c --isa sve --json
```

## 核心输入输出

输入必须符合 canonical request JSON。至少需要：

- `target_function`
- `loop_info.file_path`
- `loop_info.start_line`
- `loop_info.end_line`
- `target_arch`
- `data_types`

可选字段：

- `isa_extensions`: ISA 扩展特性数组，如 `["dotprod", "i8mm", "sve2"]`
- `codegen_style`: 代码生成形态，`"auto"` (默认)、`"intrinsics"`、`"inline_asm"` 或 `"assembly"`
- `optimization_level`: 旧兼容字段，`"intrinsics"` 映射到 `codegen_style="intrinsics"`，`"asm"` 映射到 `codegen_style="inline_asm"`
- `width_dispatch`: 是否使用宽度跳转表，布尔值（默认 false）
- `semantic_contract`: 语义证明对象，包含 `aliasing`、`index_properties`、`math_mode`、`requires_bit_exact`、`allows_reassociation`

`codegen_style=auto` 的选择规则：

- C/C++ 标量源码默认生成 `intrinsics`
- C/C++ 中已有 `asm/__asm__` 时生成或改进 `inline_asm`
- `.S/.s/.asm` standalone 汇编源码生成 `assembly` artifacts

实际输出形态三选一：`intrinsics | inline_asm | assembly`。

ISA 扩展层次：

- **NEON baseline**: 默认，使用基础 NEON intrinsics
- **NEON + dotprod**: 使用 `usdot`、`vdot` 等 dot product 指令
- **NEON + i8mm**: 使用 `smmla`、`usmmla`、`summla` 等矩阵乘指令
- **SVE baseline**: 使用 `svld1`、`svst1`、`svwhilelt` 等
- **SVE2**: 使用 `svwhilelt`、`svzip`、`svunzip` 等 SVE2 扩展
- **SME streaming**: 使用 `__arm_streaming` 函数属性
- **SME ZA/tile**: 使用 ZA tile 和 outer-product 指令

输出必须始终返回：

```json
{
  "vectorization_result": {
    "success": true,
    "modified_file": "...",
    "original_loop": "...",
    "vectorized_code": "...",
    "codegen_style": "intrinsics",
    "replacement_kind": "full_function",
    "application_mode": "materialize_to_generate",
    "artifacts": [],
    "accumulation_pattern": null,
    "microkernel_shape": null,
    "register_budget": null,
    "spill_risk": null,
    "register_allocation_plan": null,
    "candidate_register_allocations": [],
    "selected_register_allocation": null,
    "fallback_register_allocations": [],
    "underutilization_risk": false,
    "verification_required": false,
    "verification_actions": [],
    "intrinsics_used": ["..."],
    "epilogue_handling": "...",
    "expected_speedup": "...",
    "safety_checks": ["..."],
    "error_message": ""
  }
}
```

`codegen_style`、`replacement_kind`、`application_mode`、`artifacts`、`accumulation_pattern`、`microkernel_shape`、`register_budget`、`spill_risk`、`register_allocation_plan`、`candidate_register_allocations`、`selected_register_allocation`、`fallback_register_allocations`、`underutilization_risk`、`verification_required` 和 `verification_actions` 是扩展字段。旧消费者可忽略它们；新流程必须在 `safety_checks` 中说明实际选择的 `codegen_style`、替换粒度和选择原因。`assembly` 输出必须通过 `artifacts` 提供 `.S/.s/.asm` 产物，`vectorized_code` 只放 C wrapper 或主入口 C 源码。

## 强制工作流

执行顺序必须固定；不要跳步。

### 1. 先探测本地环境

必须先跑：

- `scripts/detect_isa_features.sh`
- `scripts/detect_compiler_support.py`
- `scripts/preflight_benchmark_env.py --arch <target_arch>`

只要本机 ISA、编译器、或 preflight 任何一项不满足，就直接拒绝，返回 `success=false`。

### 2. 校验 request

必须保留并执行 request 校验，按 `references/input-output-contract.md` 检查：

- 字段是否齐全
- `target_arch` 是否是 `neon|sve|sme`
- `loop_info.file_path/start_line/end_line` 是否可落到真实源码
- `data_types` 是否与待优化循环一致
- `neon` 是否明确 `vector_width = 128`
- `codegen_style` 是否是 `auto|intrinsics|inline_asm|assembly`
- `semantic_contract` 若存在，是否只表达可证明语义；未知别名、未知索引唯一性、未知数学重排许可都按未知处理

request 有任何问题就拒绝，不进入代码生成。

### 3. 读取源码并判断能否优化

必须直接读取 `loop_info.file_path` 对应源码，并判断：

- 是否是连续或可证明安全的 unit-stride 访存
- 是否存在跨迭代依赖
- 源码形态：C/C++ 标量、已有 intrinsics、已有 inline asm、或 standalone assembly
- 是否存在副作用、I/O、锁、原子、不可证明安全的辅助调用
- 是否存在间接寻址、scatter/gather、别名冲突、输出冲突
- 是否存在 GEMM、卷积、filter、矩阵分解、归约或 Stencil 风格的 register accumulation / micro-kernel 机会

只要命中任何拒绝条件：
- 返回 `success=false`，在 `error_message` 中说明拒绝的具体原因
- **不要做 inline self-challenge**：共享上下文的自挑战不够严苛。挑战由编排器在 apply-optimization 之后通过独立的 AdversarialReview 阶段执行，对所有优化点（成功和失败）进行全面审核

已存在 intrinsics、SIMD 类型或汇编时不要直接拒绝：

- 已有 intrinsics：默认进入评审、补全或局部改进，不能退回标量，也不能因为“已有 SIMD”直接返回无需优化
- 已有部分向量化：继续寻找标量剩余路径、未向量化的内层循环、尾处理、低并行度累加链或访存布局优化空间
- 已有 inline asm：进入 `inline_asm` 路径，检查约束、clobber、尾处理、寄存器使用和 ABI
- standalone assembly：进入 `assembly` 路径，检查符号、AAPCS64 调用约定、callee-saved 寄存器、目标 ISA、链接边界和可扩展的向量化空间
- 只有源码形态与显式 `codegen_style` 冲突、汇编方言无法识别或语义不可证明时才拒绝

### 4. 再查结构化 ISA 资料

只有在 1-3 全部通过后，才允许进入具体 ISA 选择。

固定顺序：

1. 用 `scripts/query_arm_intrinsics.py` 或 `docs/arm-intrinsics-manual/` 查目标 ISA 的常用项
2. 如果是 `SME` 且疑似需要 `ZA/tile`，先读 `docs/sme-za-tile-guide.md`
3. 如果要生成任何 `ZA` / tile inline asm，必须再读 `docs/sme-za-inline-asm-guide.md`，并用 `lookup --instruction FMOPA --isa sme` 与 `search --group inline-asm --isa sme --json` 核对模板
4. 对头文件、feature macro、函数属性、`ZA` 状态争议，以 `docs/arm-official-source-map.md` 指向的 `ACLE` 为最终裁决

当前知识库覆盖：

- `NEON`
- `SVE`
- `SME`
- 与向量化直接相关的常用 `SVE2`
- `SVE` / `SVE2` broad instruction assets；curated DB 未命中时，可查询 PMULL、AES、SM4、EOR、BSL、TBL、XAR、HISTCNT 等压缩/加密相关条目作为 reference-only 资料
- 与 ZA / slice 写入直接相关的常用 `SME2`
- `NEON` / `SVE` 的 sum/dot reduction 辅助项

### 5. 最后才生成结果

通过前四步后，才能生成 `vectorization_result`。

`success=true` 的代码必须以“可编译、可链接、可运行”为目标，而不是只在文本上符合 intrinsic 形式。生成后必须至少完成：

- `query_arm_intrinsics.py validate-snippet --isa <arch> --style intrinsics|inline_asm|assembly --json`
- 用目标编译器和目标架构 flags 编译候选源码
- 链接到最小 driver 或实际 benchmark driver
- 对最终对象或可执行文件做未解析符号扫描；不得残留目标运行环境未提供的 SME ABI 符号，例如 `__arm_tpidr2_save`

如果本机或当前会话无法完成目标 ISA 的编译/链接验证，必须返回 `success=false`，或把结果标为未验证草案；不得把未验证代码包装成“优化完成”。

架构规则：

- `NEON`：固定 128-bit，主向量循环加显式尾处理
- `SVE`：长度无关循环，使用 `svcnt*()`、`svwhilelt_*`、`svld1_*`、`svst1_*`
- `SME`：默认优先 `__arm_streaming`；只有矩阵语义和 `ZA/tile` 约束都清楚时，才进入真正的 `ZA/tile`
- `SME ZA/tile`：生成 standalone C 源码时，不要默认使用 `__arm_new("za")`。该属性会让 clang 按 SME ABI 生成 `__arm_tpidr2_save` 等运行时依赖；除非目标链接环境已经被验证提供这些符号，否则必须按 `docs/sme-za-inline-asm-guide.md` 改用显式 `__arm_streaming` + inline asm 管理 `smstart za` / `zero {za}` / `fmopa` / `st1w` / `smstop za`，或返回 `success=false`。

循环形态规则：

- 逐元素 map / zip：继续按目标 ISA 生成主向量循环
- sum reduction：只允许 `acc += x[i]` 且 `acc` 只作为最终结果返回或循环后写出
- dot reduction：只允许 `acc += a[i] * b[i]`，输入必须连续、无冲突写、无副作用
- `float32` reduction 默认允许轻微舍入差异，必须在 `safety_checks` 说明非 bit-exact；若 request 或依赖说明要求严格顺序 / bit-exact，则拒绝
- `int32` reduction 仅在不依赖溢出语义时允许；无法证明时拒绝
- prefix scan / running output，例如 `out[i] = acc`，仍然必须拒绝
- GEMM / convolution / filter / matrix factorization / Stencil 风格 kernel：必须识别累加域（K、tap、radius 或 reduction length），并让 accumulator 跨该域保留在寄存器中直到最终写回
- 选择 micro-kernel shape 前必须运行 `scripts/select_register_allocation.py` 枚举候选寄存器分配；策略器会分别建模 `vector`、`predicate`、`gpr` 和 `za_tile` 预算，并优先选择可验证 throughput score 最高的可行方案
- `intrinsics` 默认最高只选择 `medium` 风险；`inline_asm` / `assembly` 可以选择 `high` 风险，但必须输出 `verification_actions` 并在验证阶段检查汇编 spill、clobber 和 ABI 边界
- `microkernel_hint.shape` 只能作为候选之一，不是最终 shape；如果策略器选出更激进的 `selected_register_allocation.shape`，代码生成必须使用策略器结果
- 对 compute kernel，不允许停在 safe but underutilized 的 4 accumulator 之类方案；候选中 accumulator 更少的 shape 必须在 `candidate_register_allocations` 中标记 `underutilization_risk=true`
- 选择 micro-kernel shape 时必须按 ISA、dtype、codegen style 和寄存器预算计算 accumulator 数量；必须记录 `register_allocation_plan`、`candidate_register_allocations`、`selected_register_allocation`、`fallback_register_allocations`、`register_budget`、`spill_risk` 与 `verification_actions`
- 如果生成方案在内层 K/tap/radius 循环中把 partial accumulator 写回内存再 reload，必须拒绝或标为未验证草案

SME ZA/tile 门控规则：

- 普通逐元素、masked 逐元素、sum/dot reduction、GEMV 默认走 NEON/SVE 或 SME streaming-compatible，不进入 ZA/tile
- 只有明确 GEMM、rank-k 或 outer-product 风格的二维块累加语义，才允许生成 ZA/tile 方案
- 生成 ZA/tile response 时，`safety_checks` 必须说明输出 tile 映射、行/列维度、K/outer-product 累加维度、ZA ownership、边界 predication 和写回时机
- 如果使用 inline asm 避免 SME ABI runtime 依赖，`safety_checks` 还必须说明：`smstart/smstop` 范围、`zero {za}` 时机、`fmopa` operand 宽度、谓词寄存器、ZA tile/slice 写回、clobber 列表，以及汇编扫描中没有 `__arm_tpidr2_save` / `__arm_tpidr2_restore` / `__arm_za_disable`
- 如果上述任一项不能说明清楚，要么退回 streaming-compatible 路径，要么 `success=false`，不得硬造 ZA/tile 代码

代码生成强制规则：

1. `vectorized_code` 必须使用与 `target_function` 完全相同的函数名
2. 必须保持原始函数的完整签名
3. 由于生成代码写入 `generate/` 子目录，本地头文件 include 需要改成 `../xxx.h`
4. 生成完整函数定义，不是片段
5. 如果不是完整函数，必须设置 `replacement_kind=function_body|loop_body`
6. 不得通过内置弱符号 stub 解决 SME ABI 链接错误；要么验证并链接真实运行时，要么生成不引用这些运行时符号的实现
7. `assembly` 结果必须通过 `artifacts` 输出 standalone `.S/.s/.asm`，不得把汇编全文塞进 `vectorized_code`
8. `.S` artifact 必须定义可链接符号，并遵守 AAPCS64 参数、返回值和 callee-saved 寄存器规则
9. 计算密集型 micro-kernel 必须先运行 `scripts/select_register_allocation.py --isa <arch> --dtype <dtype> --json`，再按 `selected_register_allocation.shape` 生成代码；需要强制高风险 intrinsics 时才显式加 `--max-spill-risk high`；`scripts/calculate_register_budget.py` 仅用于单个候选预算复核
10. 对小 K、短卷积核、固定 filter tap 或 Stencil 小半径，允许把内部 K/tap/radius 循环完全展开，但必须把 full-unroll 交给 `loop-unrolling` 规则，并记录代码体积和寄存器压力风险

## 静态规则入口

在生成代码前或审查代码时，优先用：

- `scripts/query_arm_intrinsics.py validate-snippet --isa neon|sve|sme --style intrinsics|inline_asm|assembly`

### 指令存在性验证

当不确定某条 NEON/SVE/SME 指令是否存在、语法是否正确、或需要发现同类替代指令时，查询 ARM 官方指令集：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py instruction --name <指令> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword "<功能关键词>" --family <neon|sve|sve2> --json
```

| 场景 | 查询操作 |
|------|---------|
| 不确定指令是否存在于目标 ISA | `arm_query.py instruction --name <指令名> --family <neon\|sve\|sve2> --json` |
| 需要发现某类操作的硬件指令（如绝对值、点积） | `arm_query.py instruction-search --keyword "<功能关键词>" --family <neon\|sve\|sve2> --json` |
| 需要确认指令语法/操作数/伪代码 | 先用 `arm_query.py instruction`，必要时再用 `query.py info <指令名>` 获取完整详情 |
| 不确定指令依赖哪个 ISA 扩展 | 查询结果的 `evidence.features` / `matches[].features` |
| 需要枚举某个指令家族的全部成员 | `arm_query.py instruction-search --keyword "<操作码前缀>" --family <neon\|sve\|sve2> --json` |

### Intrinsic 事前查询

在生成 NEON/SVE intrinsic 代码前，用 `acle_query.py` 确认 intrinsic 的存在性和签名：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic-search --keyword <关键词> --family <neon|sve|sve2> --json
```

| 场景 | 查询操作 |
|------|---------|
| 知道指令名，找对应的 C intrinsic | `arm_query.py intrinsic-search --keyword <指令> --family=<neon\|sve\|sve2> --json`，必要时再用 `acle_query.py insn` |
| 不确定 intrinsic 名，按关键词搜索 | `arm_query.py intrinsic-search --keyword <关键词> --family=<neon\|sve\|sve2> --json` |
| 确认 intrinsic 原型/参数/头文件/特性宏 | `arm_query.py intrinsic --name <intrinsic名> --family=<neon\|sve\|sve2> --json` |
| 需要列出某 ISA 下某类 intrinsic | `acle_query.py list --family=<neon\|sve\|sve2> --cat=<arith\|ldst\|...>` |
| 需要知道有哪些特性检测宏 | `macros` 查看全部宏列表 |
| 需要了解类型系统（类型宽度/转换） | `types` 查看 ACLE 类型定义 |

所有命令支持 `--json` 输出。生成阶段必须保留 `<pipeline_root>/docs/arm-instruction-query-contract.md` 定义的 evidence，并写入 `safety_checks`、`verification_actions` 或 artifact。数据覆盖 15090 条 intrinsic；本流程只消费 `NEON/SVE/SVE2`，不把 SME 带入本次向量化路径。

**AI 常见错误参考**：生成代码前，Read `<pipeline_root>/skills/arm-instructions-query/references/ch03-common-ai-errors.md`，预判 SVE sizeof/全局变量/结构体成员/Neon 尾处理/谓词用法/reduction 精度等典型 AI 错误，避免生成已知的反模式。

**与 `validate-snippet` 的分工**：`acle_query.py` 在生成**前**确认"这个 intrinsic 是否存在、怎么用"；`validate-snippet` 在生成**后**检查"代码结构形态是否正确"（尾处理/谓词/头文件/clobber）。

当前静态规则至少覆盖：

- `NEON` 固定宽度循环缺少显式尾处理
- `SVE` 使用固定 lane 数而不是长度无关循环
- `SVE` 载入 / 存储缺少谓词上下文
- `SVE` `svcntb/h/w/d` 与实际元素宽度或指针步长不匹配
- `SVE` load/store 用裸 `svptrue_*` 覆盖尾部，可能越界
- `SVE` gather/scatter 缺少 `semantic_contract` 对索引边界、别名和 scatter 唯一性的证明
- `SVE` 压缩/加密通算代码出现变量长度 parser、重叠 match copy、未证明变量移位或有符号溢出风险
- `NEON` / `SVE` reduction 缺少最终水平归约或精度说明
- `sv*` / `svmopa*` / `__arm_*` 缺少匹配头文件
- `SME` intrinsic 出现在非 streaming 上下文
- `ZA` 相关 intrinsic 缺少 `__arm_inout("za")`、`__arm_new("za")` 或 `__arm_out("za")`
- `svzero_za()` 缺少 fresh-ZA 或 explicit ZA output 语义
- `SME ZA` inline asm 缺少 `smstart za`、`zero {za}`、`fmopa`、`st1w`、`smstop za` 或 `za` clobber
- 生成源码里出现弱符号 SME ABI stub 或未验证的 `__arm_tpidr2_*` / `__arm_za_disable` 依赖
- 混用跨 ISA 模板
- `intrinsics` 形态中混入 inline asm
- `inline_asm` 形态缺少 `asm/__asm__`、`memory` clobber 或寄存器 clobber
- `assembly` 形态缺少 `.text/.globl`、函数 label、AArch64 指令体或 `ret`
- AArch64 汇编标签以数字开头但不是纯数字 local label，例如 `4x4_M_loop:`
- `ld1` / `st1` post-index 立即数缺少 `#`，或多寄存器列表的 post-index 立即数与实际传输字节数不匹配
- `fmla` scalar-by-element 误写成 `vM.4s[lane]` / `vM.2d[lane]` 等向量 arrangement 形式，而不是 `vM.s[lane]` / `vM.d[lane]`

## 明确拒绝的情况

出现这些情况时必须拒绝：

- 前缀和、普通递推、暴露 running accumulator 的循环携带状态
- `float32` reduction 要求 bit-exact 或严格左到右顺序
- `int32` reduction 无法证明不依赖溢出语义
- 原子、锁、I/O、全局副作用
- 不规则 gather/scatter、间接寻址、不可证明安全的别名；SVE gather/scatter 只有在 `semantic_contract` 明确证明索引只读、边界内、scatter 无重复且无别名冲突时才允许
- 显式 `codegen_style` 与源码形态冲突，例如 `.S` 输入强制要求 `intrinsics`
- 已有 intrinsics、SIMD 类型或汇编但无法证明 ABI、约束、clobber、尾处理或语义正确
- 数据类型和目标架构下的向量操作没有清晰映射
- 用户要求 `SME ZA/tile`，但输入只是普通逐元素逻辑

## 可处理的复杂模式

以下复杂模式在满足特定条件时可向量化：

### 间接寻址预处理

满足以下全部条件时，可通过预处理安全向量化：

- 索引数组 `indices[i]` 只读；若有 scatter 写，必须已知无重复（无写冲突）
- `semantic_contract.index_properties` 证明 `readonly`、`in_bounds`，scatter 场景还必须证明 `unique`
- 目标指针数组可预计算：`ptrs[i] = base + indices[i]`
- `base` 不与其他输入/输出指针别名
- 循环体只做 `*ptrs[i] += value` 或 `*ptrs[i] = value` 形式的简单更新

预处理策略：
```c
// 原始间接更新（若满足条件可预处理）
for (int i = 0; i < N; i++) {
    int dst = indices[i];
    out[dst] += value;  // scatter accumulate
}

// 预处理后（可向量化内层）
float *out_ptrs[N];
for (int i = 0; i < N; i++) {
    out_ptrs[i] = out + indices[i];  // 标量预处理
}
// 若 indices 无重复，下面可安全向量化
for (int i = 0; i < N; i++) {
    *out_ptrs[i] += value;  // 或用 SVE gather load
}
```

SVE 下可直接使用 `svld1_gather_*` / `svst1_scatter_*`，但需验证无写冲突；NEON 模拟 gather/scatter 默认不作为成功路径，除非 benchmark 和语义证明都显示收益。

### 多层嵌套循环分层向量化

满足以下条件时可分层向量化：

- 外层循环控制维度遍历，内层循环是计算密集型
- 内层循环迭代之间无跨迭代依赖
- 内层循环体可独立向量化

分层策略：
```c
// 原始多层循环
for (int r = 0; r < rows; r++) {
    for (int c = 0; c < cols; c++) {
        out[r*cols + c] = in[r*cols + c] * scale;  // 内层可向量化
    }
}

// 分层向量化后
for (int r = 0; r < rows; r++) {  // 外层保持标量
    float *row_out = out + r*cols;
    float *row_in = in + r*cols;
    // 内层向量化
    int vl = svcntw();
    for (int c = 0; c < cols; c += vl) {
        svbool_t pg = svwhilelt_b32(c, cols);
        svfloat32_t vin = svld1_f32(pg, row_in + c);
        vin = svmul_f32_x(pg, vin, scale);
        svst1_f32(pg, row_out + c, vin);
    }
}
```

### 常见算子模式识别

以下算子模式可识别并应用专用向量化策略：

- **2D Pooling (MAX/AVG)**: 窗口遍历 + 累加 + rescale；内层通道维度向量化
- **Depthwise Convolution**: 输入窗口加载 + 权重乘累加；内层通道向量化
- **GEMV**: 向量-矩阵乘；行方向向量化累加
- **SAD/SSD**: 像素差累加；可使用 `vabdl` + `vabal` 模式
- **Strided / interleaved data**: RGB/RGBA、I/Q 或 AoS 通道交错优先考虑 `vld2/vld3/vld4`、`zip/unzip/trn`，或转交 memory-access-optimization 做布局变换
- **Widen / narrow / saturating / rounding**: 图像、DSP、量化 kernel 需要专用 widen/narrow/saturating 指令，不能按普通 `add/mul` map 套模板
- **min/max/product reduction 与 argmin/argmax**: 与 sum/dot 分开处理；必须说明 NaN、有符号零、溢出和 tie-break 语义
- **int8 dotprod / i8mm / BF16 / FP16**: 只有 request 或环境明确对应 ISA 扩展时才允许，且必须记录精度和累加类型
- **CRC / crypto**: 优先 PMULL / crypto extension 或专用算法路径，不作为普通 SIMD map
- **压缩 byte/bit 子循环**: 固定块 byte map、compare/hash、mask、table lookup、独立 checksum 可以尝试；LZ match copy、变量长度 token parser 和依赖前次 match 状态的循环必须拒绝
- **数学函数 `expf/sinf/logf` 等**: 优先 compiler veclib、ArmPL/Libamath、SLEEF 或明确拒绝；不要手写错误近似
- **Portable SIMD**: 可建议 Google Highway 等 portable SIMD 作为独立模式，但不能和 ARM-only response 混写

## 输出目录约定

所有生成的产物（request JSON、response JSON、generated C 源码）必须写入源码所在目录的 `generate/` 子目录，不得原地修改原始源文件。

仓库内提交的只包括：

- `references/arm_intrinsics_db/` 下的结构化快照
- `docs/arm-intrinsics-manual/` 下的自动生成手册

仓库内不提交运行时 request / response / generated 产物。

## 脚本链路

知识库链路：

1. `refresh_arm_intrinsics_db.py`
2. `generate_arm_intrinsics_manual.py`
3. `query_arm_intrinsics.py`

向量化主链路：

1. `generate_vectorization_request.py`
2. 模型或 subagent 生成 canonical `response JSON`
3. `materialize_vectorization_result.py`
4. `benchmark_real_source.sh`
5. `benchmark_before_after.sh`

如果只是想跑单个候选 `response JSON`，使用：

- `benchmark_model_response.sh`

## Codex / subagent 边界

在 Codex 中可以：

- 主会话生成 `request JSON`
- subagent 显式使用 `$apply-vectorization` 分析源码并生成 `response JSON`
- 主会话用 `query_arm_intrinsics.py` 校验候选代码片段
- 主会话把 `response JSON` 交给仓库脚本做物化和 benchmark

仓库脚本不能：

- 直接起 subagent
- 在 shell 中假设自己能调用模型

如果模型判断 request 不合法、环境不满足、或循环不安全，应该直接返回拒绝结果，而不是把问题推给下游脚本。
