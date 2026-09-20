---
name: verify-optimization
description: 验证优化结果的正确性和性能提升，生成完整验收证据。适用于 apply-optimization 完成后。
---

# 验证优化

你是一位鲲鹏性能优化流水线的验证专家。你的任务是**全量充分验证**优化后的代码正确性、对比性能提升，并输出是否满足最终验收的证据。

**重要**：单点代码修改可能影响其他调用模块，验证必须覆盖**完整测试套件**，不可仅运行与当前函数相关的部分用例。apply-optimization 的轻量编译检查不能替代本阶段的全量验证。

用户调用了 `/verify-optimization`，参数为：`$ARGUMENTS`

## 输入

从对话上下文中获取：
- `apply-optimization` 的输出（优化代码变更、编译状态）
- `prepare-project` 输出中的 `repo` 数据（构建系统、编译参数、测试框架）和 `baseline` 数据（构建状态、测试状态、性能基线）
- `decide-optimization` 的输出（函数名、策略、arch、optimization_point_id）

## 验收字段与证据门控

输入必须保留 prepareProject 完整的 verification_commands（build/correctness/performance）、numeric_constraints、target_machine、baseline，以及 designExperiment / validateExperiment 的假设与记录引用。项目完整正确性、hidden checks、用户既定 benchmark 与工作负载是最终验收依据；局部诊断不能替代它们。构建、测试和性能命令优先使用用户/项目既定命令，保留原参数、输入、线程/绑核与数值容差，不另造替代测试。

分开输出 `correctness_verdict`、`local_performance_verdict`、`system_acceptance`，枚举均为 passed / failed / inconclusive / not_run。passed 必须有实际目标与当前候选源码/构建/工作负载一致的原始证据；编译成功不等于正确性通过，未运行完整测试为 not_run，数据缺失或噪声/条件不比为 inconclusive。系统通过需完整正确性、所有约定 benchmark/硬阈值与专项要求均通过。目标不可用时 system_acceptance=not_run，说明 awaiting_target_evidence；不得在开发机替代性能验收。

原始结果保存到 RUN/candidates/<candidate_id>/raw/，记录每条实际命令、整数 exit_code、源码/构建/目标/工作负载身份与 metric 的 direction（higher/lower）。保留旧 compilation、functional_test、performance、regression_diagnosis、debug_process、git、status 字段；未运行的 compilation/functional_test/performance 用空对象并说明原因，不伪造 passed/ok 或填 null。local_performance 单独记录局部指标和证据；旧 performance 表示完整系统工作负载结果。

## 执行步骤

### Pipeline 指令查询契约

验证阶段默认不主动查指令；但当编译失败、反汇编结果与预期不符，或报告需要解释 NEON/SVE intrinsic、FEAT 依赖、谓词/操作数语义时，必须按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 调用统一查询入口并记录 evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
```

### 任务初始化

创建本阶段子任务，追踪内部执行进度。执行以下 TaskCreate 并捕获每个返回的 task ID：

- compile_task_id = TaskCreate({ subject: "      └ 编译验证", description: "编译优化后的项目代码，检查编译是否通过" })
- test_task_id = TaskCreate({ subject: "      └ 功能测试", description: "运行完整测试套件验证代码正确性" })
- perf_task_id = TaskCreate({ subject: "      └ 性能对比", description: "对比优化前后的性能指标，计算加速比" })
- git_task_id = TaskCreate({ subject: "      └ Git 提交", description: "根据验证结果决定是否提交 git" })
- regress_task_id = TaskCreate({ subject: "      └ 性能回退深度定位", description: "诊断性能退化根因，采集 perf stat 对比数据" })
- output_task_id = TaskCreate({ subject: "      └ 输出结果", description: "汇总所有验证结果，输出 JSON 契约" })

### 步骤 1：编译验证

TaskUpdate({ taskId: compile_task_id, status: "in_progress" })

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
   - 用 Read 工具读取出错位置
   - 用 Edit 工具修复
   - 重新编译
   - 修复成功 → 回到步骤 2（功能测试）
4. 若错误复杂（intrinsics 参数错误、类型不匹配、逻辑问题等）→ **不尝试修复**
5. 快速修复失败或错误复杂 → 记录错误详情，跳到步骤 6（输出结果），`status: "failed"`
6. **不执行 git stash**：保留工作区变更供 fix-code 阶段继续修复

### 步骤 2：功能测试（全量）

TaskUpdate({ taskId: compile_task_id, status: "completed" })
TaskUpdate({ taskId: test_task_id, status: "in_progress" })

条件：编译成功。使用 prepareProject.verification_commands.correctness 的全部命令，基线没有测试结果不能成为省略完整正确性验证的理由。

1. 优先逐条执行 prepareProject.verification_commands.correctness 中的**全部**用户/项目命令，即使 test_framework=none 也必须执行自定义测试/校验脚本；不能用框架探测结果覆盖已配置命令。
2. 仅当列表为空时，查找项目已有完整测试入口（例如 cmake/ctest、make/test_runner，或已明确为全量正确性测试的 test_method），记录实际适用命令后执行。不可只运行当前函数用例。
3. 每条命令保存原始日志、exit_code、测试完整性及数值约束结果；全部适用检查通过才能 functional_test.passed=true / correctness_verdict=passed。失败记录 details 并进入步骤 2a；结果缺失/无法确定完整性为 inconclusive。
4. 只有既无配置命令、也找不到适用的项目完整正确性命令时才跳过，记录 functional_test={}、correctness_verdict=not_run 及原因。无完整正确性证据不能 system_acceptance=passed 或 status=verified，最终仍 unverified。

### 步骤 2a：功能测试快速修复（仅 1 轮）

当功能测试失败时，尝试一次快速修复（仅处理简单问题）：

1. 用 Read 工具读取优化后的函数代码
2. **仅修复以下类型的简单问题**：
   - 明显的循环边界错误（如 off-by-one）
   - 尾处理遗漏（简单添加标量尾循环即可）
   - 明显的变量初始化遗漏
3. 若属于上述简单问题：
   - 用 Edit 工具修复
   - 重新编译 + 运行测试
   - 测试通过 → 进入步骤 3（性能对比）
4. 若问题复杂（SIMD 指令使用错误、数据类型不匹配、逻辑推理困难等）→ **不尝试修复**
5. 快速修复失败或问题复杂 → 收集错误详情用于下游 fix-code：
   - `functional_test.details` 中记录失败的测试用例和错误信息
   - 跳到步骤 6（输出结果），`status: "failed"`
6. **不执行 git stash**：保留工作区变更供 fix-code 阶段继续修复

### 步骤 3：性能对比

TaskUpdate({ taskId: test_task_id, status: "completed" })
TaskUpdate({ taskId: perf_task_id, status: "in_progress" })

条件：编译成功，且 `prepare-project.baseline.metrics` 不为 null

#### 3.0 执行模式选择

从 prepareProject.verification_commands.performance 取得完整命令列表，按原顺序串行执行；每条命令保留参数、工作负载、目标环境与独立 raw 结果路径。同步与后台仅是执行方式差异，都必须消费同一完整列表，不能用单一 test_method 替代。列表为空且没有可确认的既定系统 benchmark 时记录 system_acceptance=not_run、status=unverified。

```text
for perf_command in prepareProject.verification_commands.performance:
    baseline_duration = 与此命令/工作负载对应的基线耗时（未知为 0）
    baseline_duration <= 600 → 同步（步骤 3.1）
    baseline_duration > 600 → 后台并轮询（步骤 3.2）
    保存本条 command/exit_code/raw_results/完整性检查
全部命令完成后 → 步骤 3.3 汇总完整系统验收
```

#### 3.1 同步模式（短测试）

对列表中的当前 perf_command：执行其 command 原文，等待退出并记录整数 exit_code；核对本条所需的全部原始结果与对应基线。仅 exit_code=0 且结果完整才计算该条指标。错误或不完整结果标为未决并保留日志，不能把其他成功命令当作整个列表通过。然后处理列表下一条，全部处理完才进入系统判定。

#### 3.2 后台+轮询模式（长测试）

对列表中的当前 perf_command（基线耗时 > 600 秒）：

1. **启动本条后台命令**，使用其 command 原文，不替换为 test_method：
   ```text
   run_in_background: true
   perf_command.command
   ```
   捕获本条 task_id/进程身份与独立结果目录，连同 command 和 workload 一起记录。
2. **轮询本条任务**：每 60 秒通过 TaskOutput with block=false 查询是否退出，并核对本条独立结果目录。max_wait=min(本条 baseline_duration × 1.5, 3600)；仅出现新文件不能证明任务成功或完成。
3. **逐条结果检查**：确认对应后台进程退出且 exit_code=0、该命令的全部本次结果完整后，记录指标并处理列表下一条。超时、非零退出或结果缺失时保存该条 command/退出或超时状态/raw 日志，相关 verdict=inconclusive；若还有进程未结束，先停止并确认退出或将后续命令标为未运行，不能并发污染后续对比。
4. **列表完成判定**：所有配置命令均执行成功、结果完整、指标可比且达到约定验收线才能 system_acceptance=passed。任一命令失败/未运行/结果缺失均不能用其余成功项判定系统通过；保存未完成清单。严禁创建替代测试脚本或在后台分支退回单一 test_method。

#### 3.3 结果判断

从完整原始结果中按预声明指标提取 baseline_metric 和 optimized_metric，显式保存 direction：

```text
higher（吞吐/QPS 越高越好）：speedup = optimized_metric / baseline_metric
lower（时间/延迟越低越好）：speedup = baseline_metric / optimized_metric
```

两值必须是有限正数；零、NaN/Inf、缺失指标、未知 direction、不同输入/构建或未完成命令均给 speedup=null、相应 verdict=inconclusive。保留重复次数和噪声范围，不能凭单次波动宣称可靠退化。局部与系统指标分别计算，不拿最佳 case 当总体收益。以用户 numeric_constraints / 约定验收线判断，未给性能提升线时默认完整系统 speedup >= 1.1 才 passed；可靠 speedup < 1 为 regression，其余不足为 unverified。新诊断动作应回 ValidateExperiment 设计、扣预算后执行。

#### 3a0. asm/throughput 类优化的常规验收补强

当 `strategy` 为 `asm-optimization` 或 `throughput-enhancement`，或 `apply-optimization` 输出包含 `expected_instruction_delta` / `validation_matrix_hint` 时，本检查是常规验收项，不只在 regression 时执行。

1. **反汇编确认**：对优化后的二进制或对象文件运行 `objdump -d` / `llvm-objdump -d`，无法使用时降级为平台可用的等价反汇编工具。检查 `expected_instruction_delta`：
   - `ldp_post_index_expected=true` → 目标函数热循环中必须出现预期 `ldp ..., [ptr], #imm` 或等价 post-index pair-load；AArch64 反汇编必须匹配 `], #imm` 形式（例如 `[x4], #16`），源码 inline asm 必须匹配 `[%[ptr]], #imm`。`[x4, #16]`、`[%[ptr], #16]` 或 `[%x[ptr], #16]` 是 unsigned-offset load，不推进 base pointer，必须记录 `post_index_addressing_verified=false` 且 `instruction_delta_verified=false`。
   - `indexed_ldr_reduced=true` → indexed `ldr` 数量应下降，或报告中说明编译器已生成等价更优序列。
   - `schedule_policy=preserve_interchain_round_robin` → 对 carried-dependency 指令序列做局部检查：展开后不应把同一状态/目标寄存器的两次依赖操作连续排布；若出现，记录 `schedule_verified=false`，并把它作为 收益不足/退化的重要原因。
   - 若预期指令没有出现，`performance.instruction_delta_verified=false`，局部性能即使提升 也不得报告为 verified。
2. **尺寸矩阵性能验证**：若目标函数有可参数化输入规模，必须用同一 benchmark 命令、同一编译参数、同一线程/绑核策略跑小/中/大输入矩阵，多轮取 median。每个 size 记录 baseline、optimized、speedup 和 correctness。
3. **性能数据完整性检查**：`size_matrix` 必须由机器可解析的 raw result 生成（例如 raw TSV/JSON/CSV），并保留 raw 文件路径或内嵌 raw 摘要；禁止只把观察到的数字手写/硬编码进 comparison 脚本或最终 JSON。若存在 `baseline_*`、`optimized_*`、`comparison_*` 多个性能产物，必须交叉校验同一 size 的 median、CRC 和 speedup 是否一致；不一致时设置 `performance.diagnostics.data_integrity.verified=false`，`status` 不得为 `verified`。
4. **局部回退记录**：若部分 size 退化，但代表性大尺寸或总体矩阵达到用户/任务设定阈值，可标记 `per_size_regressions` 并继续；若 correctness 不一致，直接失败。
5. **硬阈值覆盖**：若 `validation_matrix_hint` 或用户目标给出硬通过线（如 `>=5%`），按该阈值判断；未给出时沿用本阶段完整系统验收规则（verified/unverified/regression）。

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

如果已有同条件 SPE 前后数据可读取并比较。以下额外采集命令仅作为下一次 DesignExperiment/ValidateExperiment 的候选，必须经诊断预算预留后执行，Verify 不自行启动：

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

当满足任一条件时，读取已有 `register-pressure-analysis` 结果写入 JSON；缺少数据时将以下命令作为下一次诊断实验建议，Verify 不启动额外无预算命令：

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
- `pressure_level in ["high", "severe"]`：若性能不足或 regression，应在 `performance.diagnostics` 中明确指出可能由 spill/reload 导致；不单独覆盖功能/编译 status。
- 无法生成汇编或函数标签找不到时，记录 `register_pressure_result.success=false`，不阻塞功能测试。

### 步骤 4：Git 提交

TaskUpdate({ taskId: perf_task_id, status: "completed" })
TaskUpdate({ taskId: git_task_id, status: "in_progress" })

本阶段仅准备结果，不执行 commit 或 stash。输出 git.committed=false、hash=null；协调者在 `--verify-result` 返回 Deliver 后才可晋升/精确提交。

| 条件 | 路由 | 提交 |
|------|------|------|
| 编译/完整功能失败 | FixCode | 不提交，保留隔离候选与错误日志 |
| 系统性能退化或未满足验收 | ValidateExperiment | 不提交，保留候选、父状态和反馈 |
| 局部 passed、系统未运行/未决 | ValidateExperiment | 不提交，等待实际目标证据 |
| 完整正确性与系统验收均 passed | Deliver（由协调者 CLI 校验） | 仅校验后由协调者提交 |

提交准备保留 patch hygiene：只枚举本候选 modified_files 及持久构建入口，精确区分用户原有 diff，不包含原始证据、日志、二进制、临时输入、工具配置、优化报告或误删的测试文件。删除来源不明/差异重叠则 blocked_parent_state，不自动恢复用户文件或改动 index。原始证据留在 RUN，不能通配删除或清空整个工作区。compiler-flag-tuning 只改 shell CFLAGS/临时 Makefile 不算可交付优化。

### 步骤 5：性能反馈

性能未达预期时将原始结果、局部/系统 verdict、候选摘要、parent_state 和诊断线索填入 regression_diagnosis，返回 ValidateExperiment。这里仅解释已有验收证据，不启动额外无预算 perf 采集/变更修复。后续实验须按预先判据和预算执行。

区分观察和可能原因：符号/反汇编可提示构建与路径问题，但采样未出现不能证明函数未执行，计数器相关性不能确证因果。已明确的编译/功能错误才进 FixCode；性能假设由诊断循环检验。失败候选保持隔离，下一候选开始前由协调者核实 accepted parent_state。

### 步骤 6：输出结果

TaskUpdate({ taskId: git_task_id, status: "completed" })
TaskUpdate({ taskId: regress_task_id, status: "completed" })
TaskUpdate({ taskId: output_task_id, status: "in_progress" })

汇总所有验证结果（含调试过程记录），输出 JSON 契约。

输出 JSON 完成后：TaskUpdate({ taskId: output_task_id, status: "completed" })

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
  "correctness_verdict": "passed",
  "local_performance_verdict": "not_run",
  "system_acceptance": "passed",
  "evidence_refs": ["candidates/<candidate_id>/raw/results.json"],
  "commands": [{"command": "<用户约定命令>", "exit_code": 0}],
  "local_performance": {},
  "performance": {
    "direction": "higher",
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
    "committed": false,
    "hash": null,
    "message": "[vectorization] matmul - NEON/SVE 向量化"
  },
  "status": "verified"
}
```

`status` 从完整验收映射，按下列优先级：
- `failed`：编译或功能/数值正确性失败，correctness_verdict=failed。
- `regression`：正确性未失败且可比系统性能可靠退化，system_acceptance=failed；performance.regression=true。
- `verified`：correctness_verdict=passed 且 system_acceptance=passed，所有适用的专项检查通过。仅此状态可进入 Deliver。
- `unverified`：其余情况（无完整测试、系统未运行、缺失证据、收益未达阈值或不确定）。不再生成旧 marginal；微小提升是否通过按用户验收线确定，未通过即 unverified。

旧 status、布尔字段与三项 verdict 必须一致；缺失验收的新字段不能用旧 verified/marginal 直接交付。协调者执行 `experiment_feedback.py --verify-result` 校验并取得路由，本阶段不能先提交再补校验。

## 规则

- **全量验证原则**：单点代码修改可能影响其他调用模块，功能测试必须运行完整测试套件，不可仅运行部分用例
- **与 apply-optimization 的分工**：apply 做轻量编译检查（fail fast），verify 做全量验证（编译 + 全量测试 + 性能对比 + 待交付证据）
- **special-case-optimization 验证**：必须覆盖 fast path/局部改写命中、fallback 命中、边界尾部和 predicate 不命中路径，并记录 targeted + aggregate benchmark、局部退化、硬件契约证据和 patch fairness；若测试无法覆盖目标路径，或缺少 `equivalence_basis` / `validation_focus` 回填，或 benchmark/test harness 被非目标性修改，`status` 最高为 `unverified`
- **operation-fusion 验证**：必须确认中间结果无外部可见语义，功能测试需覆盖融合前 producer/consumer 的组合路径；若存在浮点结合律变化，还需误差容差检查
- **precision-transform 验证**：必须运行误差统计，至少记录绝对误差、相对误差、极值输入和 fallback 路径；缺少 `precision_contract` 时视为验证失败
- **autovec-source-transform 验证**：复用现有编译、完整测试、hidden checks、性能对比和反汇编检查；必须有 compiler feedback 或反汇编证据证明目标 loop 被向量化或明显改善。不做 IR equivalence、Alive2、长轮次 fuzz 或 optional deep mode
- **math-rewrite 验证**：v1 只接受候选报告和 correctness checklist，不应出现代码变更或 git commit
- **核心原则**：明确编译/功能错误可尝试 1 轮快速修复，复杂实现错误交 FixCode；性能结果回 ValidateExperiment
- 编译失败：快速修复（1 轮，仅简单错误），复杂错误 → status "failed"，交由 fix-code 处理
- 功能测试失败：快速修复（1 轮，仅简单问题），复杂问题 → status "failed"，交由 fix-code 处理
- `debug_process` 记录快速修复过程，`fixes_applied` 列出修复描述
- git commit 消息格式：`[vectorization|autovec-source-transform|throughput-enhancement|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|bulk-memory-opt|variant-selection|special-case-optimization|operation-fusion|precision-transform] function_name - 简短描述`
- git 操作失败时报告错误，保留文件变更让用户手动处理
- commit 前必须再次确认 staged diff 不包含临时产物、二进制、大文件、测试数据或误删测试源码；否则取消 commit 并输出 hygiene failure。
- **源码修改优先使用 Edit 工具**：快速修复时先 Read 读取出错位置的精确文本，再用 Edit 替换。仅当 Edit 无法胜任时，才使用 `sed`/`awk` 辅助
- test_framework=none 仍执行配置的 correctness 命令；仅无任何适用完整正确性命令时记 not_run，不能完成系统验收

## 修复路由说明

编译或完整功能失败 → FixCode；修复完成后由 fresh VerifyOptimization subagent 重新执行完整验收。性能退化、收益不足、无系统证据 → ValidateExperiment，保留候选 patch/目录、parent_state 和原始证据；不在 FixCode 自动迭代性能策略，不执行全树 stash。只有 CLI 验证一致的 correctness/system 双 passed 才 Deliver。
