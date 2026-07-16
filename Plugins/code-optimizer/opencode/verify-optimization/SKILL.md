---
name: verify-optimization
description: 验证优化结果的正确性和性能提升，提交 git。适用于 apply-optimization 完成后。
---

# 验证优化

你是一位鲲鹏性能优化流水线的验证专家。你的任务是**全量充分验证**优化后的代码正确性、对比性能提升，并决定是否提交 git。

**重要**：单点代码修改可能影响其他调用模块，验证必须覆盖**完整测试套件**，不可仅运行与当前函数相关的部分用例。apply-optimization 的轻量编译检查不能替代本阶段的全量验证。

用户调用了 `/verify-optimization`，参数为：`$ARGUMENTS`

## 输入

从对话上下文中获取：
- `apply-optimization` 的输出（优化代码变更、编译状态）
- `prepare-project` 输出中的 `repo` 数据（构建系统、编译参数、测试框架）和 `baseline` 数据（构建状态、测试状态、性能基线）
- `decide-optimization` 的输出（函数名、策略、arch、optimization_point_id）

## 执行步骤

### Pipeline 指令查询契约

验证阶段默认不主动查指令；但当编译失败、反汇编结果与预期不符，或报告需要解释 NEON/SVE intrinsic、FEAT 依赖、谓词/操作数语义时，必须按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 调用统一查询入口并记录 evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
```

### 任务初始化

创建本阶段子任务，追踪内部执行进度。执行以下 todowrite 创建任务列表：

todowrite({
  todos: [
    { content: "编译验证", status: "pending", priority: "high" },
    { content: "功能测试", status: "pending", priority: "high" },
    { content: "性能对比", status: "pending", priority: "high" },
    { content: "Git 提交", status: "pending", priority: "high" },
    { content: "性能回退深度定位", status: "pending", priority: "high" },
    { content: "输出结果", status: "pending", priority: "high" }
  ]
})

### 步骤 1：编译验证

// 标记任务进行中：编译验证

1. 根据构建系统编译项目：
   - cmake：`cd <repo.path>/build && make -j$(nproc) 2>&1`
   - make：`cd <repo.path> && make -j$(nproc) 2>&1`
   - **compiler-flag-tuning 策略**需要 clean build：先执行 `make clean`，再执行完整构建
   - **asm-optimization 策略且 `modified_files` 包含 `.s`/`.S` 文件**：先做汇编语法检查 `as -o /dev/null <modified_file> 2>&1`，再执行项目构建
2. 记录编译结果：
   - `ok: true/false`
   - `warnings: <警告数量>`
   - `error: <错误信息，成功时为 null>`
3. 编译失败时 → 进入步骤 1a（编译错误修复循环）

### 步骤 1a：编译错误快速修复（仅 1 轮）

当编译失败时，尝试一次快速修复（仅处理简单错误）：

1. 分析编译错误信息，定位出错的源文件和行号
2. **仅修复以下类型的简单错误**：
   - 缺少分号、括号不匹配等语法问题
   - 明显的类型转换缺失
   - 变量名拼写错误
3. 若属于上述简单错误：
   - 用 read 工具读取出错位置
   - 用 edit 工具修复
   - 重新编译
   - 修复成功 → 回到步骤 2（功能测试）
4. 若错误复杂（intrinsics 参数错误、类型不匹配、逻辑问题等）→ **不尝试修复**
5. 快速修复失败或错误复杂 → 记录错误详情，跳到步骤 5（输出结果），`status: "failed"`
6. **不执行 git stash**：保留工作区变更供 fix-code 阶段继续修复

### 步骤 2：功能测试（全量）

todowrite({ todos: [{ content: "编译验证", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "功能测试", status: "pending", priority: "high" }] })

条件：`prepare-project.baseline.tests_pass != null` 且编译成功

1. 运行**完整测试套件**（不可仅运行当前函数相关用例——单点修改可能影响其他模块）：
   - cmake + ctest：`cd <repo.path>/build && ctest --output-on-failure`
   - make + googletest：`cd <repo.path> && ./test_runner`
   - 若 test_method 为全量测试命令，直接使用：`<test_method>`
2. 记录结果：
   - `passed: true/false`
   - `details: "<n>/<total> cases passed"` 或错误摘要
3. 测试失败时 → 进入步骤 2a（功能调试流程）

如果 `test_framework == none`，跳过此步骤，记录 `functional_test: null`。

### 步骤 2a：功能测试快速修复（仅 1 轮）

当功能测试失败时，尝试一次快速修复（仅处理简单问题）：

1. 用 read 工具读取优化后的函数代码
2. **仅修复以下类型的简单问题**：
   - 明显的循环边界错误（如 off-by-one）
   - 尾处理遗漏（简单添加标量尾循环即可）
   - 明显的变量初始化遗漏
3. 若属于上述简单问题：
   - 用 edit 工具修复
   - 重新编译 + 运行测试
   - 测试通过 → 进入步骤 3（性能对比）
4. 若问题复杂（SIMD 指令使用错误、数据类型不匹配、逻辑推理困难等）→ **不尝试修复**
5. 快速修复失败或问题复杂 → 收集错误详情用于下游 fix-code：
   - `functional_test.details` 中记录失败的测试用例和错误信息
   - 跳到步骤 5（输出结果），`status: "failed"`
6. **不执行 git stash**：保留工作区变更供 fix-code 阶段继续修复

### 步骤 3：性能对比

todowrite({ todos: [{ content: "功能测试", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "性能对比", status: "pending", priority: "high" }] })

条件：编译成功，且 `prepare-project.baseline.metrics` 不为 null

#### 3.0 执行模式选择

根据基线测试耗时判断使用**同步模式**还是**后台+轮询模式**：

```
# 从 prepareProject.baseline.metrics 获取基线耗时
baseline_duration = max(baseline.metrics 中各 case 的耗时, 默认 0)
# Bash 工具超时限制
bash_timeout_limit = 600  # 秒

if baseline_duration == 0 or baseline_duration <= bash_timeout_limit:
    使用同步模式（步骤 3.1）
else:
    使用后台+轮询模式（步骤 3.2）
```

**判断逻辑**：当单次完整测试耗时超过 Bash 工具的超时限制（600s）时，后台模式允许测试完整运行而不会被截断。

#### 3.1 同步模式（短测试）

1. 运行与基线相同的性能测试（相同的 benchmark 脚本或命令）
2. 记录优化后的性能指标
3. 计算加速比，跳转到步骤 3.3 判断结果

#### 3.2 后台+轮询模式（长测试）

当基线耗时 > 600s 时使用此模式：

1. **启动后台测试**：
   ```bash
   # run_in_background: true
   <test_method>
   ```
   捕获返回的 `task_id`。

2. **轮询结果目录**（ann-benchmarks 等框架在运行过程中写入 HDF5 结果文件）：
   ```
   poll_interval = 60  # 秒
   max_wait = min(baseline_duration × 1.5, 3600)  # 上限 1 小时
   elapsed = 0

   while elapsed < max_wait:
       sleep(poll_interval)
       elapsed += poll_interval
       检查结果目录是否有新文件产生
       检查后台任务是否已完成（TaskOutput with block=false）
       if 任务已完成:
           break
   ```

3. **结果检查**：
   - 若结果目录有新文件 → 测试完成，进入步骤 3.3
   - 若超时仍无结果 → 记录 `performance: null, reason: "background test timeout"`
   - 若后台任务异常退出 → 记录错误信息，`performance: null`

4. **禁止行为**：在后台模式下**严禁自行创建替代测试脚本**，必须使用 `test_method`。

#### 3.3 结果判断

从结果文件中提取性能指标（QPS、延迟等），计算加速比：

```
speedup = optimized_metric / baseline_metric
```

判断结果：
- `speedup >= 1.1` → 无回归
- `1.0 <= speedup < 1.1` → marginal（无显著提升）
- `speedup < 1.0` → regression（性能退化，**不直接放弃，参见步骤 5 深度定位**）

如果无法运行性能测试或无法提取结果，记录 `performance: null` 及原因。

#### 3a0. asm/throughput 类优化的常规验收补强

当 `strategy` 为 `asm-optimization` 或 `throughput-enhancement`，或 `apply-optimization` 输出包含 `expected_instruction_delta` / `validation_matrix_hint` 时，本检查是常规验收项，不只在 regression 时执行。

1. **反汇编确认**：对优化后的二进制或对象文件运行 `objdump -d` / `llvm-objdump -d`，无法使用时降级为平台可用的等价反汇编工具。检查 `expected_instruction_delta`：
   - `ldp_post_index_expected=true` → 目标函数热循环中必须出现预期 `ldp ..., [ptr], #imm` 或等价 post-index pair-load；AArch64 反汇编必须匹配 `], #imm` 形式（例如 `[x4], #16`），源码 inline asm 必须匹配 `[%[ptr]], #imm`。`[x4, #16]`、`[%[ptr], #16]` 或 `[%x[ptr], #16]` 是 unsigned-offset load，不推进 base pointer，必须记录 `post_index_addressing_verified=false` 且 `instruction_delta_verified=false`。
   - `indexed_ldr_reduced=true` → indexed `ldr` 数量应下降，或报告中说明编译器已生成等价更优序列。
   - `schedule_policy=preserve_interchain_round_robin` → 对 carried-dependency 指令序列做局部检查：展开后不应把同一状态/目标寄存器的两次依赖操作连续排布；若出现，记录 `schedule_verified=false`，并把它作为 marginal/退化的重要原因。
   - 若预期指令没有出现，`performance.instruction_delta_verified=false`，性能即使 marginal 也不得报告为 verified。
2. **尺寸矩阵性能验证**：若目标函数有可参数化输入规模，必须用同一 benchmark 命令、同一编译参数、同一线程/绑核策略跑小/中/大输入矩阵，多轮取 median。每个 size 记录 baseline、optimized、speedup 和 correctness。
3. **性能数据完整性检查**：`size_matrix` 必须由机器可解析的 raw result 生成（例如 raw TSV/JSON/CSV），并保留 raw 文件路径或内嵌 raw 摘要；禁止只把观察到的数字手写/硬编码进 comparison 脚本或最终 JSON。若存在 `baseline_*`、`optimized_*`、`comparison_*` 多个性能产物，必须交叉校验同一 size 的 median、CRC 和 speedup 是否一致；不一致时设置 `performance.diagnostics.data_integrity.verified=false`，`status` 不得为 `verified`。
4. **局部回退记录**：若部分 size 退化，但代表性大尺寸或总体矩阵达到用户/任务设定阈值，可标记 `per_size_regressions` 并继续；若 correctness 不一致，直接失败。
5. **硬阈值覆盖**：若 `validation_matrix_hint` 或用户目标给出硬通过线（如 `>=5%`），按该阈值判断；未给出时沿用本阶段默认 verified/marginal/regression 规则。

#### 3a1. special-case 局部特化验收补强

当 `strategy == "special-case-optimization"`，或 `apply-optimization` 输出包含 `rewrite_kind` / `equivalence_basis` / `validation_focus` 时，本检查是常规验收项。

1. **覆盖矩阵**：测试必须覆盖 `validation_focus` 中列出的路径。至少包含 guard 命中、guard 不命中/fallback、边界输入；`local_equivalence_rewrite` 还必须包含等价输入域边界；`hardware_contract_path` 还必须记录 feature 命中路径和无 feature fallback/原 dispatch 计划。
2. **等价证据回填**：最终结果必须记录 `equivalence_basis`、`guard_condition`、`fallback_preserved`、`risk_notes`。缺失时 `status` 最高为 `unverified`。
3. **性能矩阵**：必须同时给出 targeted benchmark 和 aggregate benchmark；若项目只有单一 benchmark，需显式标记 `targeted_is_aggregate=true`，不能只报告一个手写 speedup。
4. **局部退化记录**：若 aggregate 达标但部分 case 退化，必须把退化 case 写入 `per_case_regressions`/`per_size_regressions`，包含 baseline、optimized、speedup 和是否命中 fast path；不得隐藏局部退化后报告为无条件 verified。
5. **硬件契约证据**：`hardware_contract_path` 必须记录 feature/宏/编译属性/dispatch 或反汇编证据；若无法证明硬件路径实际启用，`status` 最高为 `unverified`。
6. **patch fairness**：检查 diff 是否修改 test/benchmark harness、benchmark 输入选择或调用变体。若目标不是 harness 本身且存在此类修改，`status` 不得为 `verified`，并记录 `patch_fairness.harness_modified=true`。
7. **风险专项**：涉及数值域时记录 NaN/Inf/signed-zero/overflow/errno/fenv 或项目等价测试容差；涉及安全/密码学/认证比较时确认没有引入 secret-dependent 分支。

#### 3a. 可选：SPE 微架构前后对比

如果 `arm-spe-analysis` Skill 可用且优化前已采集过 SPE 数据，可运行微架构对比：

```bash
SPE_SCRIPTS="skills/arm-spe-analysis/scripts"

# 优化前 baseline SPE 数据（在 analyze-hotspot 阶段采集，路径从 analyzeHotspot 输出获取）
# 优化后重新采集
bash ${SPE_SCRIPTS}/spe-collect.sh -f load,store,branch -t 30 \
  -o spe_optimized.data -- <test_method>

# 前后对比
bash ${SPE_SCRIPTS}/spe-compare.sh spe_baseline.data spe_optimized.data 2>&1
```

对比指标：
- L1 miss 率变化（↓ 说明缓存利用改善）
- LLC miss 率变化（↓ 说明访存模式改善）
- Branch mispred 率变化（↓ 说明分支消除有效）
- 延迟分布变化（latency distribution shift）

SPE 对比结果记录到 `performance.spe_compare` 字段，仅供参考，不作为 status 判定的唯一依据。

### 步骤 3b：可选寄存器压力诊断

当满足任一条件时，调用 `register-pressure-analysis`，把结果作为性能诊断证据写入输出 JSON：

- `decide-optimization.input.diagnostics.register_pressure_analysis_required == true`
- `strategy` 为 `vectorization` 且 `vectorization_result` 含 `microkernel_shape`、`register_budget` 或 `spill_risk`
- `strategy` 为 `vectorization` 且 `vectorization_result` 含 `register_allocation_plan`、`selected_register_allocation`，或 `verification_required == true`
- `selected_register_allocation.spill_risk` 为 `medium|high`
- `strategy` 为 `throughput-enhancement` 且发生 accumulator split、full K unroll 或 unroll factor >= 4
- `strategy` 为 `asm-optimization`
- 优化产物包含 `.S/.s/.asm` artifact 或 C/C++ inline asm

调用方式：

```bash
# C/C++ intrinsics 产物：先编译成汇编
cc -O3 -S <target_arch_flags> -o /tmp/<function>.s <generated_or_modified_source.c>

# 然后诊断
python3 skills/register-pressure-analysis/scripts/analyze_assembly_spill.py \
  --asm /tmp/<function>.s \
  --function <function_name> \
  --source-kind c_intrinsics
```

记录规则：

- `pressure_level in ["none", "low"]`：作为通过证据，不改变 status。
- `pressure_level == "medium"`：记录 warning，结合性能结果判断。
- `pressure_level in ["high", "severe"]`：若性能 marginal 或 regression，应在 `performance.diagnostics` 中明确指出可能由 spill/reload 导致；不单独覆盖功能/编译 status。
- 无法生成汇编或函数标签找不到时，记录 `register_pressure_result.success=false`，不阻塞功能测试。

### 步骤 4：Git 提交

todowrite({ todos: [{ content: "性能对比", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "Git 提交", status: "pending", priority: "high" }] })

根据验证结果决定 git 操作：

| 条件 | 操作 | status |
|------|------|--------|
| 编译快速修复后通过 + 测试通过 | git commit | verified/marginal/regression |
| 功能快速修复后通过 | git commit | verified/marginal/regression |
| 编译失败（无法快速修复）| **不 stash**，记录错误详情，等待 fix-code | failed |
| 功能测试失败（无法快速修复）| **不 stash**，记录错误详情，等待 fix-code | failed |
| 性能退化（speedup < 1.0）| **不 stash**，记录性能对比数据，交由 fix-code 深度定位 | regression |
| 性能 marginal（1.0-1.1x）| git commit | marginal |
| 验证通过（speedup >= 1.1x）| git commit | verified |
| 无法判断（无测试无性能数据）| git commit | unverified |

**git commit 命令：**

提交前必须执行 patch hygiene：

1. 清理或移出临时产物：`perf*`、`*.data`、`*.gz`、`*.log`、`*.o`、临时 benchmark 可执行文件、临时测试输入。需要保留的证据只能放入 `optimization_reports/artifacts/`，不得进入 git commit。
2. 运行 `git status --porcelain`，把变更分为：允许提交的源码/header/汇编/构建配置、优化报告、临时产物、可疑删除。
3. 只允许提交 `applyOptimization.modified_files` 中的文件，以及明确属于持久构建入口的配置文件（如 `configure`、`CMakeLists.txt`、`Makefile.in`、`meson.build`、`BUILD.bazel`）。
4. 禁止提交二进制、perf 数据、压缩测试数据、大文件、`.opencode/**`、`.batch_optimize_answer_bank.md`、`CLAUDE.md` 和 `optimization_reports/**`。
5. 若发现 `test/` 或 `tests/` 下源文件被删除，除非该删除明确来自用户需求，否则恢复该文件并把状态降为 `failed` 或 `unverified`。
6. `compiler-flag-tuning` 必须修改持久构建入口。只改临时 `Makefile`、只产生 `build.log`、或只改变当前 shell CFLAGS 时，不得 commit 为 verified；应输出 `status: "unverified"` 或 `failed` 并说明不可合入。

```bash
cd <repo.path>
git add -- <applyOptimization.modified_files...> <明确允许的构建配置文件...>
git commit -m "[<strategy>] <function_name> - <简短描述>"
```

commit 消息中的 `<strategy>` 取值：
- vectorization → `[vectorization]`
- autovec-source-transform → `[autovec-source-transform]`
- throughput-enhancement → `[throughput-enhancement]`
- prefetch-optimization → `[prefetch-optimization]`
- branch-elimination → `[branch-elimination]`
- memory-access-optimization → `[memory-access-optimization]`
- compiler-flag-tuning → `[compiler-flag-tuning]`
- asm-optimization → `[asm-optimization]`
- bulk-memory-opt → `[bulk-memory-opt]`
- variant-selection → `[variant-selection]`
- special-case-optimization → `[special-case-optimization]`
- operation-fusion → `[operation-fusion]`
- precision-transform → `[precision-transform]`
- math-rewrite → 无 commit（自动 skip，不提交代码变更）

**git stash 命令（验证失败时）：**

```bash
cd <repo.path>
git stash push -m "revert: <function_name> optimization failed verification"
```

### 步骤 5：性能回退深度定位

todowrite({ todos: [{ content: "Git 提交", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "性能回退深度定位", status: "pending", priority: "high" }] })

当 `speedup < 1.0`（regression）时，**不立即 stash**，而是先验证优化是否真正生效，再采集诊断数据供下游 fix-code 深度定位：

#### 5a. 前置检查：优化是否真正生效（必须在微架构分析之前执行）

性能无提升的最常见原因不是优化思路错了，而是优化代码根本没被执行。按以下顺序逐项检查：

1. **编译检查**：确认修改的源文件是否被重新编译
   ```bash
   ls -la <modified_source>.o  # 检查 .o 时间戳是否晚于源文件修改时间
   stat <modified_source>      # 确认源文件修改时间
   ```
   若 .o 文件未更新 → 构建系统增量编译跳过了修改文件 → fix-code 应修复 CMakeLists.txt/Makefile 依赖或强制 clean rebuild

2. **符号检查**：确认优化后的函数在二进制中
   ```bash
   nm <binary> | grep <function_name>   # T = text section（正常）, U = undefined（链接失败）
   objdump -t <binary> | grep <function_name>
   ```
   若符号不存在或标记为 'U' → 函数未被链接进二进制 → fix-code 应修复链接

3. **指令检查**：确认新指令存在
   ```bash
   objdump -d <binary> | grep -A20 "<function_name>:"
   ```
   检查是否包含预期的新指令（NEON `vld1q`/`vmlaq`、SVE `whilelt`/`ld1w`、预取 `prfm` 等）：
   - 若新指令不存在 → 编译器可能因别名分析/依赖关系未正确向量化 → fix-code 检查 restrict 限定和编译选项
   - 若新指令存在但被条件分支包裹 → 运行时可能永远不会执行 → fix-code 检查分支条件

4. **执行检查**：确认优化后的函数被实际调用
   ```bash
   perf record -e cpu-clock -- <test_method> 2>&1
   perf report --stdio --sort=sym | head -30
   ```
   检查优化后的函数是否出现在 perf report 的 Top-N 中：
   - 若函数不出现在采样中 → 函数未被调用或调用路径不通 → fix-code 检查调用链
   - 若函数出现但占比很低 → 可能只执行了 cold path → fix-code 检查测试用例覆盖

**关键判断**：若以上任一检查失败，退化原因标记为 `optimization_not_effective`（优化未生效），fix-code 应聚焦**让优化生效**（修复构建/链接/调用链），而非调整优化策略本身。

#### 5b. 微架构对比分析（仅当 5a 确认优化已生效时执行）

1. 采集优化前后 perf stat 对比：
   ```bash
   # 优化前 baseline（从 prepareProject.baseline 或 analyzeHotspot 获取）
   # 优化后重新采集
   perf stat -e cycles,instructions,L1-dcache-load-misses,LLC-load-misses,branch-load-misses -- <test_method>
   ```
2. 记录关键指标前后对比到 `regression_diagnosis` 字段：
   - IPC 变化（↑/↓）
   - Cache miss 率变化
   - Branch mispred 率变化
3. 尝试快速判断退化原因（启发式）：
   - IPC 下降 + cache miss 上升 → 数据布局变更导致缓存效率降低
   - IPC 上升但总 cycles 反增 → 指令数膨胀（展开过多/I-cache 溢出）
   - branch miss 率上升 → 分支消除反而引入不可预测分支
   - IPC 和 cache 均无恶化 → 可能是编译器优化被抑制（如 restrict 缺失）

### 步骤 6：输出结果

todowrite({ todos: [{ content: "Git 提交", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "性能回退深度定位", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "输出结果", status: "pending", priority: "high" }] })

汇总所有验证结果（含调试过程记录），输出 JSON 契约。

输出 JSON 完成后：
todowrite({ todos: [{ content: "输出结果", status: "completed", priority: "high" }] })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "function": "<function_name>",
  "optimization_point_id": "<opt_point_id>",
  "compilation": {
    "ok": true,
    "warnings": 0,
    "error": null
  },
  "functional_test": {
    "passed": true,
    "details": "12/12 cases passed"
  },
  "performance": {
    "execution_mode": "sync|background",
    "background_task_id": null,
    "baseline_metric": 7910,
    "optimized_metric": 9291,
    "speedup": 1.17,
    "regression": false,
    "size_matrix": [
      { "size": 1024, "baseline_ns_median": 125000, "optimized_ns_median": 118000, "speedup": 1.059, "correctness_match": true }
    ],
    "instruction_delta_verified": true,
    "remote_target_verified": true,
    "special_case_validation": {
      "rewrite_kind": "constraint_specialization",
      "equivalence_scope": "guarded_path",
      "guard_hit_tested": true,
      "fallback_tested": true,
      "equivalence_basis_recorded": true,
      "targeted_benchmark_recorded": true,
      "aggregate_benchmark_recorded": true,
      "targeted_is_aggregate": false,
      "per_case_regressions_recorded": true,
      "hardware_contract_verified": null,
      "patch_fairness": {
        "harness_modified": false,
        "variant_callsite_changed": false
      },
      "risk_notes": "",
      "remote_target": "0xd03"
    },
    "post_index_addressing_verified": true,
    "schedule_verified": true,
    "per_size_regressions": [],
    "diagnostics": {
      "register_pressure_result": null,
      "expected_instruction_delta": null,
      "actual_instruction_delta": null,
      "data_integrity": {
        "verified": true,
        "raw_result_files": ["baseline_raw.tsv", "optimized_raw.tsv"],
        "comparison_generated_from_raw": true,
        "inconsistencies": []
      }
    }
  },
  "debug_process": {
    "compilation_quick_fix_attempted": false,
    "functional_quick_fix_attempted": false,
    "quick_fix_succeeded": false,
    "fixes_applied": [],
    "final_outcome": "pass|fixed|failed"
  },
  "regression_diagnosis": {
    "attempted": false,
    "effectiveness_check": {
      "performed": false,
      "source_recompiled": null,
      "symbol_in_binary": null,
      "new_instructions_found": null,
      "function_in_perf_samples": null,
      "optimization_not_effective": null,
      "details": ""
    },
    "ipc_before": null,
    "ipc_after": null,
    "cache_miss_rate_before": null,
    "cache_miss_rate_after": null,
    "branch_miss_rate_before": null,
    "branch_miss_rate_after": null,
    "likely_cause": null
  },
  "git": {
    "committed": true,
    "hash": "<commit_hash>",
    "message": "[vectorization] matmul - NEON/SVE 向量化"
  },
  "status": "verified"
}
```

`status` 取值：
- `verified`：功能测试通过 + 性能提升 >= 1.1x
- `marginal`：功能测试通过 + 性能 1.0-1.1x
- `regression`：功能测试通过但性能退化
- `failed`：编译失败或功能测试失败
- `unverified`：无法运行测试和性能对比

## 规则

- **全量验证原则**：单点代码修改可能影响其他调用模块，功能测试必须运行完整测试套件，不可仅运行部分用例
- **与 apply-optimization 的分工**：apply 做轻量编译检查（fail fast），verify 做全量验证（编译 + 全量测试 + 性能对比 + git）
- **special-case-optimization 验证**：必须覆盖 fast path/局部改写命中、fallback 命中、边界尾部和 predicate 不命中路径，并记录 targeted + aggregate benchmark、局部退化、硬件契约证据和 patch fairness；若测试无法覆盖目标路径，或缺少 `equivalence_basis` / `validation_focus` 回填，或 benchmark/test harness 被非目标性修改，`status` 最高为 `unverified`
- **operation-fusion 验证**：必须确认中间结果无外部可见语义，功能测试需覆盖融合前 producer/consumer 的组合路径；若存在浮点结合律变化，还需误差容差检查
- **precision-transform 验证**：必须运行误差统计，至少记录绝对误差、相对误差、极值输入和 fallback 路径；缺少 `precision_contract` 时视为验证失败
- **autovec-source-transform 验证**：复用现有编译、完整测试、hidden checks、性能对比和反汇编检查；必须有 compiler feedback 或反汇编证据证明目标 loop 被向量化或明显改善。不做 IR equivalence、Alive2、长轮次 fuzz 或 optional deep mode
- **math-rewrite 验证**：v1 只接受候选报告和 correctness checklist，不应出现代码变更或 git commit
- **核心原则**：验证失败时优先尝试 1 轮快速修复，复杂问题路由到 fix-code 阶段处理
- 编译失败：快速修复（1 轮，仅简单错误），复杂错误 → status "failed"，交由 fix-code 处理
- 功能测试失败：快速修复（1 轮，仅简单问题），复杂问题 → status "failed"，交由 fix-code 处理
- **不执行 git stash**：当 status 为 "failed" 或 "regression" 时，保留工作区变更（未提交的修改），供 fix-code 继续修复
- **性能退化（regression）不直接放弃**：采集 perf stat 对比数据（步骤 5），将诊断信息传递到 fix-code 深度定位，而非直接 stash 丢弃
- `debug_process` 记录快速修复过程，`fixes_applied` 列出修复描述
- git commit 消息格式：`[vectorization|autovec-source-transform|throughput-enhancement|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|bulk-memory-opt|variant-selection|special-case-optimization|operation-fusion|precision-transform] function_name - 简短描述`
- 性能 1.0x-1.1x 时 commit 但 `status: "marginal"`
- 仅当 fix-code 多轮深度定位后仍无法扭转 regression 时，才由 fix-code 执行 git stash
- git 操作失败时报告错误，保留文件变更让用户手动处理
- commit 前必须再次确认 staged diff 不包含临时产物、二进制、大文件、测试数据或误删测试源码；否则取消 commit 并输出 hygiene failure。
- **源码修改优先使用 edit 工具**：快速修复时先 read 读取出错位置的精确文本，再用 edit 替换。仅当 edit 无法胜任时，才使用 `sed`/`awk` 辅助
- 无测试框架时跳过功能测试，仅做编译+性能
- 无法获取性能数据时，编译通过即 commit，`status: "unverified"`

## 修复路由说明

当 verify-optimization 输出 `status: "failed"` 时：
- 编译错误或功能测试错误会通过 `compilation.error` 和 `functional_test.details` 传递给下游 fix-code 阶段
- verify-optimization **不执行 git stash**（保留工作区变更供 fix-code 修复）
- fix-code 修复成功后会重新分发 verify-optimization subagent 进行独立验证

当 verify-optimization 输出 `status: "regression"` 时：
- 性能退化数据通过 `regression_diagnosis` 传递给下游 fix-code 阶段
- verify-optimization **不执行 git stash**（保留工作区变更供 fix-code 深度定位）
- fix-code 分析诊断数据、定位退化根因、逐轮修复（最多 5 轮）
- fix-code 修复成功后重新分发 verify-optimization subagent 进行独立验证
- 仅当 fix-code 多轮修复仍无法扭转退化时，才执行 git stash
