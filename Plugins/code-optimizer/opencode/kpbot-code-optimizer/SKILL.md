---
name: kpbot-code-optimizer
description: 鲲鹏性能优化流水线 - Subagent 编排器。当用户想要优化 C/C++ 项目性能、分析热点函数、应用向量化、或运行完整的性能优化流程时触发。
---

# 鲲鹏性能优化流水线

你是一位鲲鹏性能优化流水线的协调者（Orchestrator）。你的职责是将任务分发给你的专业子代理，并追踪进度。

## Startup Banner

本 skill 被触发时，第一步必须执行以下命令，再开始任何其他工作流程：

```bash
SKILL_NAME=kpbot-code-optimizer; bash .opencode/skills/$SKILL_NAME/scripts/print_logo.sh 2>/dev/null || bash "$HOME/.config/opencode/skills/$SKILL_NAME/scripts/print_logo.sh" 2>/dev/null || bash .claude/skills/$SKILL_NAME/scripts/print_logo.sh 2>/dev/null || bash "$HOME/.claude/skills/$SKILL_NAME/scripts/print_logo.sh"
```

该 banner 只在全流程首次启动时打印一次，后续阶段不再重复。

## 架构

```
kpbot-code-optimizer (Orchestrator)                   ← 主 Agent
  ├── Pre-flight: Sandbox & Permissions Setup
  ├── Phase 1: 创建任务列表
  │
  ├── Phase 2: 准备阶段 [inline Skill]              ← 主 Agent 内联执行
  │    ├── GatherContext
  │    ├── ParseIntent
  │    └── PrepareProject
  │
  ├── Phase 3: 优化轮次循环 (max 5 rounds)
  │    ├── DecomposeTasks [inline Skill]             ← 主 Agent 内联执行
  │    ├── AnalyzeTestcase [inline Skill]            ← 主 Agent 内联执行
  │    │
  │    ├── 函数级子任务循环 [Agent subagent]         ← 每个函数独立 subagent
  │    │    ├── AnalyzeHotspot
  │    │    ├── [optional] AnalyzeCallerContext
  │    │    │
  │    │    └── 优化点循环 [Agent subagent]          ← 每个优化点独立 subagent
  │    │         ├── DesignExperiment → ValidateExperiment（诊断反馈门控）
  │    │         ├── DecideOptimization
  │    │         ├── AdversarialReview             ← 前置：挑战决策和性能预期（在 apply 之前）
  │    │         ├── ApplyOptimization
  │    │         │    └── 方案改变 → DesignExperiment（保留预算）
  │    │         ├── VerifyOptimization
  │    │         ├── [conditional] FixCode
  │    │         └── [conditional] Re-Verify
  │    │
  │    └── 轮次汇总 → 继续/停止?
  │
  └── Phase 4: 多轮最终汇总
```

五层分析模型：
- **轮次循环**（Round Loop）：每轮重新 profiling → 性能瓶颈漂移后自动发现新热点 → 最多 5 轮
- **DecomposeTasks**（用例级）：逐用例 profiling → 跨用例综合排序 → 函数级子任务列表
- **AnalyzeTestcase**（测试用例级）：6 维度测试源码分析 → 性能画像透传下游所有优化层
- **AnalyzeHotspot**（函数级）：perf stat/annotate/spe 微架构分析 + 静态扫描 → 优化点列表
- **AnalyzeCallerContext**（调用者级，可选）：14 维度调用点分析 → 函数体外不可见的优化机会
- **优化 Skill**（优化点级）：专项深度分析 + 代码生成

## 核心分发协议（CRITICAL）

阶段分发分两种模式：**内联阶段**由主 Agent 直接执行，**子代理阶段**通过 task 工具分发为独立 subagent。

### 内联阶段：主 Agent 直接执行

适用：GatherContext、ParseIntent、PrepareProject、DecomposeTasks、AnalyzeTestcase — 这些是**编排准备层**，产出供协调者决策和调度的元数据（repo/baseline/sub_tasks/performance_profile），不涉及代码生成。

```
skill({ name: "<skill-name>" })
```

主 Agent 直接读取 SKILL.md 输出中的 JSON 契约。

### 子代理阶段：task tool 分发

适用：函数级优化点循环内的所有阶段（AnalyzeHotspot、AnalyzeCallerContext、DesignExperiment、ValidateExperiment、DecideOptimization、ApplyOptimization、AdversarialReview、VerifyOptimization、FixCode）— 这些阶段产生代码变更、编译日志、perf 数据等大量中间输出，留在 subagent 内不污染协调者。

```
task({
  description: "<阶段名称> 阶段",
  subagent_type: "oracle" | "fixer",
  prompt: "<填充了 ${context.xxx} 变量后的完整模板内容>"
})
```

subagent 对 DesignExperiment / ValidateExperiment 直接执行共享模板；其他阶段通过 skill 工具加载对应 SKILL.md 执行，从返回文本中提取 JSON 输出。

### 为什么函数级优化阶段必须用 task

| 维度 | task tool（独立 subagent） | skill tool（主线程） |
|------|-----------|-------------------|
| 上下文隔离 | 独立上下文，skill 内容不污染协调者 | 全量加载到主线程，上下文爆炸 |
| 并行能力 | 独立阶段可并行分发 | 只能串行 |
| 错误隔离 | subagent 崩溃不影响协调者 | 错误直接中断主流程 |
| 角色专注 | subagent 专注执行，协调者专注追踪 | 角色混淆 |

### 错误做法（严禁）

```
❌ 对子代理阶段使用 skill(name="...") 直接在主线程加载   ← 无上下文隔离
❌ 对有用户交互的阶段使用 task 分发 subagent               ← 用户交互无法正确传递
```

用户调用了 `/kpbot-code-optimizer`，参数为：`$ARGUMENTS`

## 信息收集阶段

流水线启动后，**首先通过 GatherContext 阶段**与用户交互收集优化目标信息。

交互逻辑（Q1→分支选择→用例探测→复选框选择→推导 test_method）已提取为独立 Skill `gather-context`，详见 `skills/gather-context/SKILL.md`。

GatherContext 阶段完成后，将输出填入协调者状态的 `context` 中：

```yaml
context:
  user_choice: "function|testcase"
  project_path: "/absolute/path/to/project"
  code_path: "/absolute/path/to/source.c"     # 仅函数优化
  function_name: "matmul"                      # 仅函数优化
  test_cases: "benchmark_matmul, test_matmul"  # 选中的用例名列表（逗号分隔）
  test_method: "cd build && ctest -R 'benchmark_matmul|test_matmul' --output-on-failure"
  detected_cases:
    - name: "benchmark_matmul"
      description: "GEMM 性能基准测试"
      type: "googletest|ctest|executable"
    - name: "test_matmul"
      description: "矩阵乘法测试"
      type: "googletest|ctest|executable"
```

GatherContext 完成后，**ParseIntent 阶段**解析用户优化意图（优化目标、风险容忍度、平台约束、性能目标），注入 `context.parseIntent` 供下游阶段（AnalyzeHotspot 策略优先级偏置、DecideOptimization 确认门控）消费。

若 `status == "empty"` → 流水线终止。然后进入「任务创建与分发」阶段。

## Stages 配置

```yaml
stages:
  - name: GatherContext
    role: context gathering expert
    skill: gather-context
    template: prompts/stage.gather-context.md

  - name: ParseIntent
    role: intent parsing expert
    skill: parse-intent
    template: prompts/stage.parse-intent.md

  - name: PrepareProject
    role: project preparation expert
    skill: prepare-project
    template: prompts/stage.prepare-project.md
    reviewCriteria: |
      - repo 路径存在且有效
      - repo.compilation 已提取（cflags/cxxflags/performance_flags 非空）
      - target 函数/模块定位正确
      - baseline 性能数据已建立
      - machine 信息已收集（arch 和 platform_match 非空）
      - status == ready
    post-stage: |
      - 若 machine.platform_match != true，输出警告：当前机器与优化目标平台不匹配，ARM 架构特有优化将无法验证
      - 警告信息写入总结文件，并在阶段确认时展示给用户

  - name: DecomposeTasks
    role: task decomposition expert
    skill: decompose-tasks
    template: prompts/stage.decompose-tasks.md
    reviewCriteria: |
      - sub_tasks 列表非空
      - 每个 sub_task 有 function/source_file/lines
      - profiling 降级时有 fallback_reason
      - per_case profiling 记录完整

  - name: AnalyzeTestcase
    role: test case analysis expert
    skill: analyze-testcase
    template: prompts/stage.analyze-testcase.md
    reviewCriteria: |
      - performance_profile 非空且 scale.level 已判定
      - case_analyses 覆盖用户选中的测试用例
      - findings 按 confidence 降序排列
      - bottleneck_type 已推断（compute_bound|memory_bound|latency_bound|mixed）

  - name: AnalyzeHotspot
    role: performance analysis expert
    skill: analyze-hotspot
    template: prompts/stage.analyze-hotspot.md
    reviewCriteria: |
      - optimization_points 列表非空且按 priority 排序
      - 每个优化点有 type/confidence/evidence(static+dynamic)
      - dynamic_analysis 包含 perf_stat（ipc/miss rates）
      - 若输出 autovec-source-transform，static_analysis.compiler_vectorization_feedback 必须包含明确 missed-vectorization reason
      - 若输出涉及 NEON/SVE/SVE2 intrinsic、inline asm 或汇编指令事实，static_analysis.instruction_query_evidence 非空
      - 指令查询 evidence 必须来自 repo 内 `arm_query.py ... --json`；不得把 `query.py --json` 当作有效证据

  - name: AnalyzeCallerContext
    role: caller context analysis expert
    skill: analyze-caller-context
    template: prompts/stage.analyze-caller-context.md
    optional: true
    trigger: |
      - AnalyzeHotspot 完成后，若 optimization_points 数量 < 3 或用户选择启用
      - 若 DecomposeTasks 的 folded 数据不可用 → 跳过此阶段
      - 参考 testcaseAnalysis.performance_profile 辅助判断调用上下文价值
    reviewCriteria: |
      - caller_optimization_points 按 dimension_score 降序排列
      - 每条有 type/sub_type/dimension_score/evidence
      - skipped_dimensions 记录预检未通过和评分 < 0.4 的维度

  - name: DesignExperiment
    role: diagnostic experiment designer
    template: prompts/stage.design-experiment.md
    reviewCriteria: |
      - 唯一实验/假设 ID、parent_state、控制条件、支持/反驳判据和预算齐全
      - 草稿与最终记录分开，未验证预测标为 estimate

  - name: ValidateExperiment
    role: diagnostic experiment validator
    template: prompts/stage.validate-experiment.md
    reviewCriteria: |
      - 完整 CLI 校验通过，独立 current_context 与 provenance/exit_code/record_path 可核验
      - next_stage 决定分支，等待/阻塞/预算耗尽不能通过 review 强制继续

  - name: DecideOptimization
    role: optimization strategy expert
    skill: decide-optimization
    template: prompts/stage.decide-optimization.md
    reviewCriteria: |
      - status 为 confirmed / skipped / diagnose（有明确原因）；confirmed 引用当前已支持的实验，diagnose 返回诊断
      - confirmed 时包含 function/strategy/arch/skill/input
      - throughput-enhancement 策略有 throughput_enhancement 字段
      - scalar-vector-hybrid 策略有 pipeline_strategy + serial_chains + pipeline_utilization 字段

  - name: ApplyOptimization
    role: optimization execution expert
    skill: apply-optimization
    template: prompts/stage.apply-optimization.md
    reviewCriteria: |
      - strategy 对应的 result 字段非空
      - modified_files 包含变更文件
      - 代码通过编译检查
      - autovec-source-transform 只执行一次源码变形和一次 compiler feedback/反汇编复查；不得进入长轮次修补

  - name: AdversarialReview
    role: adversarial optimization reviewer
    skill: adversarial-review
    template: prompts/stage.adversarial-review.md
    reviewCriteria: |
      - challenge_result 非空
      - 对 decide-optimization 的判断声明进行了追问（性能预期是否基于实测数据、管线争用是否被考虑、替代方案是否充分评估）
      - pipeline_contention 已被挑战检查（参考了 pipeline_utilization 数据）
      - paper_vs_silicon 性能预期差异已被质疑（参考了指令性能数据）
      - 若 alternative_found == true，替代方案描述具体可执行

  - name: VerifyOptimization
    role: verification expert
    skill: verify-optimization
    template: prompts/stage.verify-optimization.md
    reviewCriteria: |
      - correctness_verdict/local_performance_verdict/system_acceptance 独立输出
      - 完整正确性与用户约定 benchmark 命令及原始结果可核验
      - --verify-result 校验所有 verdict 与旧 status 一致，只有 Deliver 可晋升

  - name: FixCode
    role: 代码语法和功能修复专家
    skill: fix-code
    template: prompts/stage.fix-code.md
    reviewCriteria: |
      - 修复后的代码能否通过编译
      - 功能测试是否全部通过
      - 修复过程是否在最大迭代次数内
```

## 协调者状态

```
latest_output: null
current_stage: null
stage_outputs: {}
run_id: null           # YYYYMMDD_HHMMSS，首次生成后不变
context:
  # GatherContext 阶段填入
  user_choice: null        # "function" | "testcase"
  project_path: null
  code_path: null          # 仅函数优化
  function_name: null      # 仅函数优化
  test_cases: null         # 用户选中的用例名列表（逗号分隔）
  target_machine: null      # 从用户目标/ParseIntent 原样透传到 PrepareProject
  verification_commands: null # 用户 build/correctness/performance 命令；不得用局部实验替代
  numeric_constraints: []
  diagnostic_budget: null   # 可选用户覆盖
  run_dir: null             # 绝对 RUN 路径
  orchestrator_skill_dir: null # 当前已安装的本 SKILL.md 所在目录
  record_script: null       # 绝对 helper 路径
  feedback_script: null     # 绝对 helper 路径
  diagnosticAttempts: {}    # 稳定 point_key -> 持久 consumed/attempts/next_stage
  test_method: null
  detected_cases: null     # [{name, description, type}]
  gatherContext: null       # GatherContext 阶段原始输出（供 parseIntent 等下游阶段读取）
  # 准备阶段填入（只跑一次）
  parseIntent: null
  prepareProject: null
  decomposeTasks: null     # 每轮重新执行，结果按轮覆盖
  testcaseAnalysis: null   # 每轮 DecomposeTasks 后重新执行，透传 performance_profile 到下游
  # 轮次循环
  current_round: 0         # 当前轮次编号（1-based）
  max_rounds: 5            # 最大轮次上限
  round_results: []        # [{round, sub_task_results, optimization_points_total, applied_count, skipped_count, round_speedup}]
  auto_continue: false     # 用户选择"自动继续"后为 true，后续轮次不再询问
  pipeline_mode: "collaboration"  # "auto" | "collaboration"，启动时由用户选择
  interaction_policy:
    ask_timeout_seconds: 120
    on_timeout: "use_default_continue"
    defaults:
      mode_selection: "auto"
      sandbox_preflight: "skip"
      stage_confirm: "continue"
      analyze_hotspot_confirm: "accept_ai_points"
      caller_context: "skip"
      round_after_success: "auto_continue"
      round_after_no_applied: "continue_until_max_rounds"
      retry_or_resume: "continue"
      review_retry_exhausted: "continue_if_non_destructive_otherwise_block"
  # 函数级子任务循环（每轮重置）
  current_sub_task: null
  sub_task_index: 0
  sub_task_results: []      # [{id, function, status, speedup, fix_info, description, optimization_point_results}]
  # 函数级各阶段输出
  analyzeHotspot: null     # 输出 optimization_points[]（含 AI 发现 + 用户补充）
  analyzeCallerContext: null   # 输出 caller_optimization_points[]（可选阶段）
  user_supplemented_points: []  # 用户补充的优化点 [{id, type, target_arch, priority, evidence, source: "user"}]
  # 优化点级子循环
  optimization_point_index: 0
  optimization_point_results: []  # [{optimization_point_id, type, strategy, status, speedup, fix_rounds}]
  current_optimization_point: null
  # 优化点级各阶段输出（新点初始化；恢复从 checkpoint 读取）
  designExperiment: null
  validateExperiment: null
  executionReservation: null # 本次实验唯一已落盘执行授权，复用/等待为 null
  decideOptimization: null
  applyOptimization: null
  adversarialReview: null
  verifyOptimization: null
  fixCode: null
  # 版本追踪
  stage_versions:
    GatherContext: 1
    ParseIntent: 1
    PrepareProject: 1
    DecomposeTasks: 1      # 每轮重置
    AnalyzeTestcase: 1     # 每轮 DecomposeTasks 后重置
    AnalyzeHotspot: 1       # 每个子任务重置
    AnalyzeCallerContext: 1 # 每个子任务重置（可选阶段，被跳过时标记 completed）
    DesignExperiment: 1       # 新点初始化；诊断循环/恢复只递增
    ValidateExperiment: 1     # 新点初始化；诊断循环/恢复只递增
    DecideOptimization: 1       # 新点初始化；同一点恢复不重置
    ApplyOptimization: 1        # 新点初始化；同一点恢复不重置
    AdversarialReview: 1     # 新点初始化；同一点恢复不重置（挑战循环内递增）
    VerifyOptimization: 1       # 新点初始化；同一点恢复不重置
    FixCode: 1                  # 新点初始化；同一点恢复不重置
```

## 分发逻辑

当收到用户请求时，按以下流程执行：

### 前置步骤 0：模式选择

流水线启动后，**首先通过 question 选择运行模式**，决定后续所有用户交互环节的行为：

```json
{
  "questions": [{
    "question": "请选择流水线运行模式：",
    "header": "运行模式",
    "options": [
      {"label": "协作模式 (collaboration)", "description": "所有需要用户确认的环节暂停等待手动确认，适合交互式优化场景（默认）"},
      {"label": "自动模式 (auto)", "description": "所有确认环节按默认值自动处理，无需人工干预，适合无人值守/CI 场景"}
    ]
  }]
}
```

选择结果存入 `context.pipeline_mode`（`"auto"` | `"collaboration"`），供后续所有交互点分支判断。

### 前置步骤 0b：运行时恢复与交互超时策略

流水线必须按 `context.interaction_policy` 处理所有用户确认点和会话层恢复点。默认策略偏向无人值守继续推进：

```yaml
interaction_policy:
  ask_timeout_seconds: 120
  on_timeout: use_default_continue
  defaults:
    mode_selection: auto
    sandbox_preflight: skip
    stage_confirm: continue
    analyze_hotspot_confirm: accept_ai_points
    caller_context: skip
    round_after_success: auto_continue
    round_after_no_applied: continue_until_max_rounds
    retry_or_resume: continue
    review_retry_exhausted: continue_if_non_destructive_otherwise_block
```

执行要求：
- 所有 `question` 交互必须在提示文本中展示默认选项和超时时间，例如：`120 秒无输入将默认选择：继续`。
- 若外层 driver、工具层或人工代理在 `ask_timeout_seconds` 内未收到用户输入，按 `defaults` 选择，不再停在等待输入状态。
- 阶段确认超时 → 默认同意并进入下一阶段。
- AnalyzeHotspot 用户补充优化点超时 → 默认接受 AI 发现的优化点，不补充、不勾选、不重做。
- AnalyzeCallerContext 超时 → 默认跳过，记录 `skipped: "timeout_default_skip"`。
- 轮次结束超时 → 仅无 awaiting/stop_budget/blocked/unresolved 依赖且父状态安全时默认继续；即使本轮 `applied_count == 0`，也继续下一轮直到 `max_rounds` 或 DecomposeTasks 返回 `empty`。
- 重试/恢复/继续类提示超时 → 默认选择继续、重试或恢复。
- Review 重试 3 轮后若仍不满足：对非诊断门控的只读分析阶段或非破坏性元数据缺失默认强制继续并记录 warning；对 PrepareProject、编译失败、功能测试失败、working tree 不安全等硬失败仍按原规则 BLOCKED 或路由 FixCode。

### 环境预检

OpenCode 不需要 sandbox 配置。确保以下工具可用：
- `bash` — 执行编译、测试、性能分析命令
- `read` / `edit` / `write` — 读写源文件
- `skill` — 加载子技能
- `task` — 分发 subagent

### Phase 0：信息收集与运行初始化

1. 生成本次运行的唯一标识：`run_id = $(date +%Y%m%d_%H%M%S)`（如 `20260507_143022`）
2. GatherContext 阶段在 Phase 2 中作为首个阶段执行

在首次初始化 RUN 时，把 `context.run_dir` 设为 `<repo.path>/optimization_reports/run_<run_id>` 的绝对路径；恢复沿用旧 run_id 和 RUN。解析**当前加载的已安装 SKILL.md** 所在目录为 context.orchestrator_skill_dir，以此得到绝对 `scripts/experiment_record.py` / `scripts/experiment_feedback.py` 路径并存 context.record_script / context.feedback_script。共享模板和 references 也从该目录读取，不以目标项目 CWD 解析 helper。若缺失安装文件则 blocked，不调用目标仓库中的同名脚本。

收集阶段保留用户 target_machine、verification_commands、numeric_constraints 和 diagnostic_budget 覆盖；PrepareProject 模板按对应 context 字段原样接收。其输出完整保存为 context.prepareProject，不能在下游只传 repo/baseline 而丢弃验收命令。

### Phase 1：创建任务列表

信息收集完成后，调用 todowrite 创建以下任务：

```
todowrite({
  todos: [
    { content: "收集优化目标信息", status: "pending", priority: "high" },
    { content: "准备项目环境", status: "pending", priority: "high" },
    { content: "优化轮次循环", status: "pending", priority: "high" }
  ]
})
```

### Phase 2：准备阶段（只跑一次）

按顺序执行：GatherContext → ParseIntent → PrepareProject

**执行模式说明**：Phase 2 全部三个阶段（GatherContext、ParseIntent、PrepareProject）由**主 Agent 内联执行**——前两者有用户交互，PrepareProject 的输出是后续所有阶段的基石数据。

**PrepareProject 完成后**：创建本次运行的报告目录（若不存在）：
```bash
mkdir -p <repo.path>/optimization_reports/run_<run_id>
```

#### 内联阶段执行协议（GatherContext / ParseIntent / PrepareProject / DecomposeTasks / AnalyzeTestcase）

内联阶段由主 Agent 直接执行，不使用 subagent：

1. `todowrite({ todos: [...当前任务标记为 in_progress] })`
2. 使用 skill 工具加载对应 SKILL.md：
   ```
   skill({ name: "<skill-name>" })
   ```
3. 收集 SKILL.md 输出中的 JSON 契约，更新 context
4. `todowrite({ todos: [...当前任务标记为 completed] })`
5. ★ 阶段总结与确认

#### 子代理阶段执行协议（函数级优化点循环）

函数级优化阶段通过独立 subagent 执行：

1. `todowrite({ todos: [...当前任务标记为 in_progress] })`
2. 读取当前阶段的 template 文件（`prompts/stage.<stage_name>.md`）
3. 填充模板变量（`${context.xxx}` → 实际值），得到完整的 prompt 文本
4. 调用 task 工具分发 fresh subagent（**不可用 skill 工具代替**）：
   ```
   task({
     description: "<当前阶段名称>",
     subagent_type: "oracle" | "fixer",
     prompt: "<步骤 3 填充后的完整模板文本>"
   })
   ```
   - 分析阶段（AnalyzeHotspot、AnalyzeCallerContext、DesignExperiment、DecideOptimization、AdversarialReview）使用 `subagent_type: "oracle"`
   - 执行阶段（ValidateExperiment、ApplyOptimization、VerifyOptimization、FixCode、ReVerify）使用 `subagent_type: "fixer"`
5. subagent 对 DesignExperiment / ValidateExperiment 直接执行共享模板；其他阶段通过 skill 工具加载对应 SKILL.md 执行
6. 从 subagent 返回的文本中提取 JSON 输出，更新 context
7. `todowrite({ todos: [...当前任务标记为 completed] })`
8. ★ 阶段总结与确认（执行「阶段总结与确认协议」）

若阶段有 reviewCriteria，协调者内联检查输出 JSON 是否满足条件（不再 dispatch 独立 review subagent）：
- 检查通过 → 进入总结与确认步骤
- 检查不通过 → 注入问题描述，重试
- 3 轮后仍不通过 → BLOCKED

### Phase 3：优化轮次循环

PrepareProject 完成后，进入迭代式优化轮次。每轮重新 profiling 发现热点函数，应用优化后性能特征可能漂移（如 cache miss 修完后 branch miss 成为新瓶颈），下一轮自动捕获新热点。

```
round = 1
while round <= max_rounds:
```

#### 3.0 轮次初始化

```
  a. 设置 context.current_round = round
  b. 若 context.pipeline_mode == "auto" 且 round == 1 → context.auto_continue = true（auto 模式自动继续所有轮次）
  c. 重置轮次级状态：
     - context.decomposeTasks = null
     - context.sub_task_results = []
     - context.sub_task_index = 0
     - context.stage_versions.DecomposeTasks = 1
   d. 若 round == 1：
      - todowrite({ todos: [...优化轮次循环标记为 in_progress] })
   e. 创建本轮任务组：
      todowrite({
        todos: [
          { content: "第 ${round} 轮优化", status: "in_progress", priority: "high" }
        ]
      })
```

#### 3.1 执行 DecomposeTasks（每轮重新 profiling）

```
  a. todowrite({ todos: [...当前轮次任务标记为 in_progress] })
  b. 执行 DecomposeTasks 阶段（内联）
     - 使用 skill 工具加载：
       skill({ name: "decompose-tasks" })
     - 收集输出的 JSON 契约 → context.decomposeTasks
     - ★ 阶段总结与确认
  c. 检查 context.decomposeTasks：
     - status == "empty" → 退出轮次循环（无可优化热点）
     - sub_tasks 非空 → 继续

#### 3.1b 执行 AnalyzeTestcase（每轮 DecomposeTasks 后）

```
  d. 执行 AnalyzeTestcase 阶段（内联）
     - 使用 skill 工具加载：
       skill({ name: "analyze-testcase" })
     - 收集输出的 JSON 契约 → context.testcaseAnalysis
     - ★ 阶段总结与确认
  e. 提取 performance_profile，后续所有子任务和优化点共享此画像
```

#### 3.2 函数级子任务循环 + 优化点级子循环

与原有逻辑一致（见下方「函数级子任务循环」和「优化点级子循环」章节）。

#### 3.3 轮次收尾

```
  a. 汇总本轮结果：
     - 从 context.sub_task_results 统计本轮优化点总数、applied_count、skipped_count
     - 本轮/累计 speedup 只能取同条件完整系统 benchmark 的前后对比；缺少累计实测则 null，不能取最佳局部值
     - 追加到 context.round_results：
       {
         "round": round,
         "sub_task_results": <context.sub_task_results>,
         "optimization_points_total": <N>,
         "applied_count": <N>,
         "skipped_count": <N>,
         "round_speedup": "<speedup>"
       }

  b. todowrite({ todos: [...当前轮次任务标记为 completed] })

  c. 终止判断：
     先检查诊断未完成/预算耗尽/目标缺失/父状态安全：不能借下一轮重置点预算或重跑同一候选；相应状态保存到最终摘要。仅可继续不依赖阻塞证据的安全独立点。

     **情况 1：本轮 applied_count == 0**
     - 本轮未产生有效优化
     - 若 context.auto_continue == false：
       → question：
         "第 ${round} 轮未产生有效优化（所有优化点被跳过或失败）。是否继续下一轮？"
         选项：["继续下一轮", "停止（结束流水线）", "自动继续（后续不再询问）"]
       → 用户选择"停止" → 退出循环
       → 用户选择"继续" → round++，继续
       → 用户选择"自动继续" → context.auto_continue = true，round++，继续

     **情况 2：本轮 applied_count > 0**
     - 若 context.auto_continue == true：
       → 直接 round++，继续下一轮（用户已授权自动继续）
      - 否则：
        → question：
          "第 ${round} 轮优化完成：发现 ${total} 个优化点，应用 ${applied} 个，跳过 ${skipped} 个。
           性能特征可能已漂移，下一轮 profiling 可能发现新热点。是否继续？"
          选项：["继续下一轮 profiling", "停止（结束流水线）", "自动继续（后续不再询问）"]
       → 用户选择"停止" → 退出循环
       → 用户选择"继续" → round++，继续
       → 用户选择"自动继续" → context.auto_continue = true，round++，继续

  d. 安全阀：若 round > max_rounds → 退出循环，报告"已达最大轮次上限（${max_rounds}）"
```

#### 3.4 轮次间清理

每轮结束后（进入下一轮前）：
- 执行优化点间清理协议，核验 accepted parent 状态且保留用户已有修改。
- 持久诊断预算和记录跨轮次保留；未验收候选保持隔离，无法确认则停止。

---

### 函数级子任务循环（Phase 3 内部）

每轮 DecomposeTasks 完成后：

1. 从 `context.decomposeTasks` 获取 `sub_tasks` 列表
2. 若 `status == "empty"` → 退出轮次循环，报告无优化目标

#### A. 函数级任务创建

**注意**：以下 todowrite 调用必须**逐个串行**创建，不要批量并行调用。

```
for sub_task in sub_tasks:
  todowrite({
    todos: [
      { content: "SubTask #${sub_task.id}: ${sub_task.function}", status: "pending", priority: "high" },
      { content: "  └ 热点分析", status: "pending", priority: "high" }
    ]
  })
```

#### B. 函数级 AnalyzeHotspot

```
for sub_task in sub_tasks:
  a. todowrite({ todos: [...SubTask 和热点分析标记为 in_progress] })
     设置 context.current_sub_task = sub_task
     重置 context.analyzeHotspot = null
     重置 context.optimization_point_results = []

  b. 执行 AnalyzeHotspot 阶段
     - 读取 prompts/stage.analyze-hotspot.md，填充模板变量（${context.prepareProject.repo}、${context.current_sub_task}、${context.test_method} 等）
     - 调用 task 工具分发 fresh subagent：
       task({
         description: "AnalyzeHotspot 阶段",
         subagent_type: "oracle",
         prompt: "<填充后的模板文本，其中包含 repo/target/baseline/sub_task/test_method 的完整 JSON 上下文>"
       })
     - 从 subagent 返回的文本中提取 JSON 输出 → context.analyzeHotspot
     - ★ 阶段总结与确认（含用户补充优化点选项）

  c. todowrite({ todos: [...热点分析标记为 completed] })

  d. 检查 context.analyzeHotspot：
     - status == "empty" 且用户未补充 → todowrite({ todos: [...SubTask 标记为 completed] })
       记录结论（无可优化点），继续下一个子任务
     - optimization_points 非空 或 用户补充了优化点 → 进入优化点循环
```

#### 用户补充优化点

**auto 模式**：跳过 question，直接使用 AI 发现的优化点（`all_points = optimization_points`），不补充、不勾选、不重做。

**collaboration 模式**：AnalyzeHotspot 阶段总结与确认时，question 提供四个选项：
1. **同意，继续下一步**：确认 AI 发现的优化点
2. **补充优化点**：用户以自然语言或结构化格式（`type: xxx, 原因: ..., priority: N`）提供额外优化点
3. **勾选未命中的优化手段**：从 `skipped_points` 中多选（展示为 checkbox 列表），自动生成 `user_supplemented_points`
4. **不同意，需要重做**：重新执行 AnalyzeHotspot

用户补充/勾选的优化点存入 `context.user_supplemented_points`，格式与 `optimization_points` 一致，额外字段 `"source": "user"`。`confidence` 默认 0.7（用户补充）或 0.5（勾选 skipped_points），`target_arch` 从上下文推断。

合并逻辑：`all_points = optimization_points + user_supplemented_points`，按 priority 升序排列，更新 `context.analyzeHotspot.optimization_points`。

#### 调用上下文分析（可选阶段）

**auto 模式**：跳过 AnalyzeCallerContext，标记 `skipped: "auto_mode"`，直接进入优化点决策阶段。

**collaboration 模式**：AnalyzeHotspot 完成后，通过 question 询问用户是否启用 AnalyzeCallerContext 阶段，从调用者视角发现额外的优化机会。

**前置检查**：
- DecomposeTasks 的 folded/profiling 调用栈数据可用 → 进入用户询问
- 若 folded 数据不可用 → 跳过此阶段，标记 `skipped: "no_caller_data"`，不询问用户

**用户询问**（仅当前置检查通过时执行）：

使用 question 工具：

```json
{
  "questions": [{
    "question": "是否启用调用上下文分析（AnalyzeCallerContext）？",
    "header": "调用者分析",
    "options": [
      {"label": "启用", "description": "分析当前函数的调用者，从调用点视角发现额外的优化机会（如内联、常量传播、调用约定优化等）"},
      {"label": "跳过", "description": "不进行调用者分析，直接进入优化点决策阶段"}
    ]
  }]
}
```

- 用户选择"启用" → 执行以下子代理分发
- 用户选择"跳过" → 标记 `skipped: "user_declined"`，继续下一步

**子代理分发**（用户选择启用时）：

```
  a. 调用 task 工具分发 fresh subagent：
     task({
       description: "AnalyzeCallerContext 阶段",
       subagent_type: "oracle",
       prompt: "<读取 prompts/stage.analyze-caller-context.md，填充 ${context.current_sub_task}、${context.decomposeTasks}、${context.analyzeHotspot}、${context.parseIntent}、${context.testcaseAnalysis.performance_profile} 后的完整文本>"
     })

  b. 从 subagent 返回的文本中提取 JSON 输出 → context.analyzeCallerContext

  c. 合并优化点：
     caller_optimization_points 必须已映射为 `special-case-optimization`、`operation-fusion`、`vectorization` 或其他可路由 strategy；若仍为 `caller-context`，协调者将其移入 skipped 记录并说明 `no_downstream_route`
     映射后的 caller_optimization_points 追加到 context.analyzeHotspot.optimization_points
     按 priority 升序重新排列
```

**跳过逻辑**：auto 模式 → `skipped: "auto_mode"`；folded 数据不可用 → `skipped: "no_caller_data"`；用户选择跳过 → `skipped: "user_declined"`；pre-check 未通过 → 记录具体原因。

#### C. 优化点级子循环

**注意**：以下 todowrite 调用必须**逐个串行**创建，不要批量并行调用。

```
  # optimization_points 已合并 AI 发现和用户补充的点，按 priority 升序排列
  for opt_point in context.analyzeHotspot.optimization_points:

    # 逐个串行创建优化点子阶段任务
    todowrite({
      todos: [
        { content: "  └ 优化点 ${opt_point.id}: ${opt_point.type}", status: "pending", priority: "high" },
        { content: "    └ 策略确认", status: "pending", priority: "high" },
        { content: "    └ 代码优化", status: "pending", priority: "high" },
        { content: "    └ 对抗性审核", status: "pending", priority: "high" },
        { content: "    └ 验证", status: "pending", priority: "high" },
        { content: "    └ 代码修复", status: "pending", priority: "high" },
        { content: "    └ 重新验证", status: "pending", priority: "high" }
      ]
    })
```

每个优化点的 todowrite 列表同时包含 DesignExperiment 与 ValidateExperiment；随诊断循环将对应条目设为 in_progress/completed，等待或阻塞保留原因。

#### D. 优化点级各阶段执行

以下为唯一优化点控制流：AnalyzeHotspot → DesignExperiment → ValidateExperiment → DecideOptimization → AdversarialReview → ApplyOptimization → VerifyOptimization。诊断与候选验收分别路由，不能把诊断失败当成代码错误。

1. 设置 `context.current_optimization_point`。按稳定的函数/源码/优化点身份查找 `context.diagnosticAttempts[point_key]`；跨重分析、轮次、重启沿用同一 key 和 point_id，不通过重命名点重置预算。恢复先跳转账本 next_stage，重新验证父/目标条件；不从步骤 4 无条件重跑。只有首次出现的点初始化 `{max_experiments, consumed: 0, attempts: [], next_stage: "DesignExperiment"}`，上限与超时取 prepareProject.diagnostic_budget（默认 3 / 600 秒）。首次初始化该点的阶段版本为 1；循环只递增版本。恢复时从 `RUN/controller_state.json` 读取全部状态和待完成 attempt，禁止重新初始化已存在的点。
2. 在修改前保存 accepted parent 的实际源码身份：Git HEAD + tracked/index/工作区与参与构建的 untracked 源码内容摘要，非 Git 用源码清单与内容摘要；不能只用 HEAD。记录用户已有 diff 和文件清单，将候选放入独立目录，保存 `candidate_path`、`candidate_patch`、`parent_state`。不能证明隔离/恢复安全则 `blocked_parent_state`，停止后续修改。
3. 协调者从目标机器探测、实际构建命令/参数及用户工作负载独立收集 `execution_context: {target, build, workload}`，写 `RUN/current_context.json`，包含 run_id、point_id、parent_state、execution_context。不得复制 design/record 来生成此文件；条件不明只能等待证据。每个点切换、父状态获完整验收后、构建或目标条件变化时重新实测核对并更新；先前支持不能自动继承到新条件。
4. 分发 DesignExperiment（共享 `prompts/stage.design-experiment.md`），传入 prepareProject、analyzeHotspot、analyzeCallerContext、当前点、run_dir、previous validateExperiment、verifyOptimization、diagnosticAttempts。解析为 context.designExperiment，检查假设、预先判据、控制条件、唯一 ID、剩余预算与超时，保存 `RUN/drafts/<experiment_id>.json`。草稿不是最终记录。改变假设要更新 hypothesis_id；已用 experiment_id 不复用。
5. 分发 ValidateExperiment 前，协调者在目标环境做只读可用性/身份检查，并核对 current_context。剩余为 0 仅禁止创建新 reservation；已有匹配且未使用的已扣额度 reservation 仍可执行一次。没有可复用新证据、没有未使用 reservation 且剩余为 0 时保存 stop_budget。创建新 reservation 需要剩余 > 0：**先**增加 consumed，写入 `{reservation_id, experiment_id, state: "reserved", launch_claimed: false, command, timeout_seconds, parent_state}`，其中 reservation_id 唯一且不可重用；原子保存 controller_state（临时文件 + rename），设置 context.executionReservation 为该完整记录，再分发执行 Agent。即使本次扣减后 remaining=0，也必须允许这次已付额度执行。同一 reservation 只可启动一次：Agent 启动命令前先将账本条目标为 state=running、launch_claimed=true 并原子落盘。恢复时仅在原账本仍 reserved/launch_claimed=false、原进程与日志核对证明未启动时才可使用原 reservation；running/claimed/completed 或无法证明未启动时只检查/回收已有结果，绝不重新启动或补回预算。命令超时和失败仍消耗一次。确认目标不可用时不启动命令、不扣额度，走等待草稿；历史证据复用不扣新执行额度，保留原 exit_code。没有新证据或新变更不得原样循环。
6. 用共享 `prompts/stage.validate-experiment.md` 执行/读取实验，串行运行，实际超时不得超过 reservation。退出码和错误日志也保存；以绝对 `context.record_script` 调用记录 CLI，获取 `RUN/experiments/<id>/record.json` 后才可进入执行反馈 CLI。等待目标使用 experiments 外、evidence 为空且无 exit_code 的草稿。原始 observation 的 provenance 必须含独立核实的 parent_state、execution_context 和 scope；历史复用增加 reuse_explanation，fixture/estimate 不可支持目标判断。保存完整反馈输入后调用：

   ```bash
   python3 "${context.feedback_script}" --record "ABS_RECORD" --feedback "ABS_FEEDBACK" --run-dir "${context.run_dir}" --remaining REMAINING
   ```

   `REMAINING = max_experiments - consumed`。CLI 输出完整 JSON 存 context.validateExperiment，同时更新 attempt 的 record_path、verdict、reason、state 与 next_stage，原子落盘 controller_state。CLI 报错为 blocked_evidence，不能自行把 checked 字段改为 true。
7. 严格按 CLI next_stage：DecideOptimization 才进入步骤 8；AnalyzeHotspot 携带反证重分析当前点，然后步骤 4；DesignExperiment 带缺项回步骤 4；awaiting_target_evidence 保存缺项与草稿，停止依赖证据的操作；stop_budget / blocked_* 保存原因并停止本点，禁止 apply。独立点只有确认父状态仍安全才可继续。诊断 hard gate 不受 auto 模式超时、review 重试强制继续影响。
8. 分发 DecideOptimization，传入完整 designExperiment、validateExperiment、record_path 和 prepareProject。confirmed 必须引用当前支持的 hypothesis_id / experiment_id；skipped 记录原因；diagnose 根据 next_stage 返回 AnalyzeHotspot 或 DesignExperiment，保留已消费预算。没有经过验证的预测只能标 estimate，不宣称确定收益。
9. 在 Apply 前分发 AdversarialReview，最多 3 次挑战/重新决策，次数也保存 checkpoint。替代方案或新证据改变假设/控制条件时回 DesignExperiment，不能直接改 plan apply；仅风险/参数说明澄清可重新 Decide。挑战仍未解决则 blocked_decision，不强行执行。
10. 分发 ApplyOptimization 一次，构造仅供该 subagent 的上下文副本：prepareProject.repo.path 指向 candidate_path，decideOptimization.input.source_file、analyzeHotspot 及 modified_files 中的源码路径映射到候选目录；保留 accepted parent 基线和原验收命令，命令中的仓库路径只做同构映射，不改变参数/输入。向 Apply/Verify/Fix 均传这个副本，禁止它们回原项目路径修改。明确编译/功能错误进入 FixCode；其他实施或环境失败为 blocked_execution。成功后分发 VerifyOptimization（重验证使用同一模板），传入完整 prepareProject.verification_commands、numeric_constraints、target_machine、baseline、designExperiment、validateExperiment、applyOptimization 和 fixCode。
11. Verify 返回后先保存 `RUN/candidates/<candidate_id>/verification.json`，调用绝对 helper：

    ```bash
    python3 "${context.feedback_script}" --verify-result "ABS_VERIFICATION_JSON"
    ```

    CLI 拒绝非法枚举/互相冲突的旧 status 与新 verdict 时标记 blocked_verification，保留候选，禁止 Deliver。根据经过校验的 next_stage：
    - **FixCode**：仅编译或完整正确性失败。最多总计 5 轮实现修复，次数保存 checkpoint；修复后递增 VerifyOptimization 版本，fresh subagent 独立重验，重新进入本步骤。耗尽为 unresolved，保存候选并先证明隔离安全才能处理下一个点。
    - **ValidateExperiment**：性能退化、收益不符或系统尚未验收，保存候选 patch/目录、accepted parent_state、实际候选源码摘要、完整验收原始日志和 regression_diagnosis。把新证据传入 context.verifyOptimization；重新进入步骤 5–7 的反馈链。以新 experiment_id 记录对该证据的复用，继承原命令 exit_code；只有满足原假设判据及 provenance 才能复用，否则先回 DesignExperiment 明确下一实验。复用不得伪装成新的执行或把 candidate 状态当 accepted parent。无新证据（包括未运行目标 benchmark）时等待目标或停止，不能用 supported→Decide→同一候选→Verify 原样循环。下一个候选前执行父状态核验协议。
    - **Deliver**：只有 correctness_verdict 与 system_acceptance 都 passed，且所有旧字段一致，才能提升候选为已验收父状态。核对候选相对 accepted parent 的 diff 并安全集成到实际父目录，重新核验源码内容等于已验收候选；不包含用户已有变更，仅对本候选已验证源码/持久配置的独立差异做精确提交（重叠文件不能全文件 git add，无法隔离则 blocked_parent_state）；Verify 叶子仅输出证据与待提交状态，提交由协调者在 CLI gate 后执行。提交失败保存错误不更新父状态。成功后重新读取真实源码/构建条件更新 parent_state 与 current_context，记录 verified，不能把局部 speedup 当累计系统收益。
12. 每个阶段分发前保存 current_stage / next_stage，返回后保存对应 context、stage_versions 和结果路径。每点结果保留旧字段，并增加 hypothesis_id、experiment_count、evidence_refs、transition_reasons、diagnostic_status、correctness_verdict、local_performance_verdict、system_acceptance、candidate_path、candidate_patch、parent_state。按阶段总结协议只展示摘要，完整实验日志保留文件。

所有以上阶段使用平台现有 fresh subagent 协议：`task({description: "<stage> 阶段", subagent_type: "oracle" | "fixer", prompt: "<已填充的共享模板全文>"})`。DesignExperiment / ValidateExperiment 直接执行共享模板任务，不查找不存在的同名叶子 skill；其余阶段调用模板指定 skill。

#### E. 函数级收尾

```
  全部优化点完成后：
  - todowrite({ todos: [...SubTask 标记为 completed] })
  - 将 optimization_point_results 汇总到 sub_task_results
  - 继续下一个函数子任务
```

全部函数子任务完成 → 进入 3.3 轮次收尾 → 继续下一轮或退出循环

全部轮次完成后 → 进入最终汇总

### Phase 4：多轮最终汇总

从 `context.round_results` 提取各轮数据，按 `prompts/partials/final-summary.md` 的诊断摘要格式向用户汇总。

汇总仅列假设、实验次数、关键证据、转向原因与最终状态；原始日志和兼容的轮次统计留在结果文件。本期保存实验记录，方法库后续建设。

## 模板变量填充规则

命名约定：协调者内部状态键（`context.xxx`）用 camelCase（如 `parseIntent`、`prepareProject`），JSON 契约字段名用 snake_case（如 `source_file`、`optimization_point_id`）。

| 模板变量 | 填充值 |
|---------|--------|
| ${stage_name} | 当前 stage.name |
| ${context.user_choice} | "function" 或 "testcase" |
| ${context.project_path} | 项目根路径 |
| ${context.code_path} | 源码文件绝对路径（函数优化时） |
| ${context.function_name} | 函数名（函数优化时） |
| ${context.test_cases} | 用例名列表（逗号分隔） |
| ${context.target_machine} | 用户目标机器要求 |
| ${context.verification_commands} | 用户原始构建/正确性/性能命令 |
| ${context.numeric_constraints} | 用户数值约束 |
| ${context.diagnostic_budget} | 用户诊断预算覆盖或 null |
| ${context.run_dir} | 绝对 RUN 路径 |
| ${context.orchestrator_skill_dir} | 已安装编排 skill 的绝对目录 |
| ${context.record_script} / ${context.feedback_script} | 已安装 helper 绝对路径 |
| ${context.designExperiment} | 完整设计与假设 JSON |
| ${context.validateExperiment} | 反馈 CLI 完整 JSON |
| ${context.diagnosticAttempts} | 持久实验预算/记录账本 |
| ${context.executionReservation} | 当前已落盘的一次命令授权或 null |
| ${context.test_method} | 测试执行方法/命令 |
| ${context.detected_cases} | 探测到的全部用例列表 |
| ${context.gatherContext} | gatherContext 阶段原始完整输出（含 user_choice/project_path/test_cases 等所有字段） |
| ${context.parseIntent} | parseIntent 完整 JSON |
| ${context.prepareProject} | prepareProject 完整 JSON（含 machine 字段：arch/cpu_model/isa_features/cache_info/platform_match） |
| ${context.prepareProject.repo} | prepareProject 输出的 repo 子对象（含 path/build_system/compilation/test_framework） |
| ${context.prepareProject.baseline} | prepareProject 输出的 baseline 子对象（含 build_ok/tests_pass/metrics） |
| ${context.prepareProject.machine} | prepareProject 输出的 machine 信息子集 |
| ${context.prepareProject.architecture_file} | ARCHITECTURE.md 文件绝对路径（仓库架构分析，跨轮复用，供下游阶段 Read 使用） |
| ${context.prepareProject.microarch_file} | 鲲鹏微架构文档绝对路径（含指令延迟/端口分配/cache 层次等，供优化 skill 按需 Read）；非鲲鹏或未知型号时为 null |
| ${context.prepareProject.instruction_perf_file} | 指令性能数据 JSON 绝对路径，按微架构选择查询脚本：TSV110(Kunpeng-0xd01)→query_tsv110.py(207条)，0xd03(Kunpeng-0xd03/0xd06)→query_uarch_b.py(29张表/754条助记符)；非鲲鹏为 null |
| ${context.decomposeTasks} | decomposeTasks 完整 JSON（仅协调者内部使用，不直接注入模板） |
| ${context.testcaseAnalysis} | testcaseAnalysis 完整 JSON（含 performance_profile，每轮 DecomposeTasks 后更新） |
| ${context.testcaseAnalysis.performance_profile} | 性能画像 JSON（下游优化层直接消费） |
| ${context.current_sub_task} | 当前函数级子任务 JSON |
| ${context.current_sub_task.function} | 当前子任务的函数名 |
| ${context.current_sub_task.source_file} | 当前子任务的源文件路径 |
| ${context.current_sub_task.lines} | 当前子任务的函数行号范围 |
| ${context.analyzeHotspot} | analyzeHotspot 完整 JSON（含 optimization_points[]、pipeline_strategy、pipeline_utilization） |
| ${context.analyzeHotspot.pipeline_strategy} | 标矢量混合决策框架输出（4 步决策 + recommendation + rationale） |
| ${context.analyzeHotspot.dynamic_analysis.theoretical_cycles.pipeline_utilization} | 跨管线利用率分析数据（groups + cross_pipeline_observations + instruction_port_map） |
| ${context.analyzeCallerContext} | analyzeCallerContext 完整 JSON（含 caller_optimization_points[]，可选阶段输出） |
| ${context.current_optimization_point} | 当前优化点 JSON（来自 optimization_points[i] 或 caller_optimization_points[i]） |
| ${context.current_optimization_point.id} | 优化点 ID（仅协调者内部使用） |
| ${context.decideOptimization} | decideOptimization 完整 JSON |
| ${context.decideOptimization.optimization_point_id} | 优化点 ID（applied by apply-optimization 模板引用） |
| ${context.applyOptimization} | applyOptimization 完整 JSON |
| ${context.adversarialReview} | adversarialReview 完整 JSON |
| ${context.verifyOptimization} | verifyOptimization 完整 JSON |
| ${context.fixCode} | fixCode 完整 JSON |
| `${item.*}` 变量 | 来源：从 `context.decideOptimization` 中提取（function/strategy/arch 为顶层字段，input 为嵌套对象） |
| ${item.function} | decideOptimization.input.function |
| ${item.strategy} | decideOptimization.strategy |
| ${item.arch} | decideOptimization.arch |
| ${item.throughput_enhancement} | decideOptimization.throughput_enhancement JSON（仅 throughput-enhancement 策略时，保留供未来使用） |

## 子任务循环中的上下文构造

### 函数级上下文（AnalyzeHotspot）

```json
{
  "repo": "<from context.prepareProject.repo>",
  "target": {
    "source_files": ["<sub_task.source_file>"],
    "entry_functions": ["<sub_task.function>"]
  },
  "baseline": "<from context.prepareProject.baseline>",
  "sub_task": {
    "id": <sub_task.id>,
    "function": "<sub_task.function>",
    "source_file": "<sub_task.source_file>",
    "lines": <sub_task.lines>,
    "priority": "<sub_task.priority>",
    "cross_case_weight": <sub_task.cross_case_weight>,
    "cpu_percent": <sub_task.cpu_percent>,
    "coverage": <sub_task.coverage>,
    "case_distribution": <sub_task.case_distribution>
  },
  "test_method": "<context.test_method>",
  "intent": <context.parseIntent>,
  "performance_profile": <context.testcaseAnalysis.performance_profile>
}
```

### 优化点级上下文（DesignExperiment / ValidateExperiment / DecideOptimization / ApplyOptimization / VerifyOptimization / FixCode）

各阶段使用 designExperiment/validateExperiment/decideOptimization/applyOptimization/verifyOptimization/fixCode 的完整上下文，以及完整 prepareProject（包括 verification_commands、numeric_constraints、target_machine、diagnostic_budget）、run_dir、绝对 helper 路径、executionReservation、diagnosticAttempts 和 testcaseAnalysis.performance_profile。候选阶段使用阶段执行协议中的隔离路径映射，不丢弃既定验收参数。

## Review Loop 实现（内联检查）

协调者直接解析 stage 输出 JSON，对照 `reviewCriteria` 逐条检查：
- 全部满足 → 通过
- 部分不满足 → 将不满足的项注入 prompt，重试解释/校验；重试绝不重新执行同一 reservation、实验命令或重置预算
- 3 轮后仍不满足 → BLOCKED

### Review Criteria 结果映射

| 阶段 | 失败条件 | 处理 |
|------|---------|------|
| GatherContext | status: empty | BLOCKED，用户未提供有效信息 |
| ParseIntent | status: empty | 记录结论，继续（下游使用默认意图） |
| PrepareProject | status != ready | BLOCKED，报告原因 |
| DecomposeTasks（首轮） | status: empty | BLOCKED，无优化目标 |
| DecomposeTasks（第 2+ 轮） | status: empty | 正常退出轮次循环（性能瓶颈已消除） |
| AnalyzeTestcase | performance_profile 为空 | 记录警告，继续（下游使用默认画像） |
| AnalyzeTestcase | 所有 case source_unavailable | 记录警告，继续（仅 profiling 数据维度可用） |
| 函数级 AnalyzeHotspot | optimization_points 为空 | 记录结论，通过 question 询问用户是否启用 AnalyzeCallerContext（如 folded 数据可用） |
| 函数级 AnalyzeCallerContext | 无 caller 数据或 pre-check 全部未通过 | 记录结论，跳过该函数，继续下一个 |
| 优化点级 DecideOptimization | status: skipped | 记录结论，跳过该优化点，继续下一个 |
| 优化点级 DecideOptimization + scalar-vector-hybrid | 缺少 pipeline_strategy/serial_chains/pipeline_utilization | 注入问题描述，重试 |
| 优化点级 DesignExperiment | 缺判据/预算/身份 | blocked_evidence，不强制继续 |
| 优化点级 ValidateExperiment | CLI next_stage | supported 才 Decide；反证回 Analyze；未决回 Design；等待/预算耗尽/blocked 停止依赖操作 |
| 优化点级 DecideOptimization | diagnose | 带理由回指定诊断阶段，预算不重置 |
| 优化点级 ApplyOptimization | 明确编译/功能失败 | FixCode；其他失败 blocked_execution |
| 优化点级 VerifyOptimization / Re-VerifyOptimization | CLI FixCode | 实现修复后完整重验，最多总计 5 轮 |
| 优化点级 VerifyOptimization / Re-VerifyOptimization | CLI ValidateExperiment | 性能反馈回诊断，保留候选和 parent_state |
| 优化点级 VerifyOptimization / Re-VerifyOptimization | CLI Deliver | 完整验收通过后精确提交，再更新父状态 |
| 优化点级 FixCode | 修复耗尽 | unresolved；先证明候选隔离安全，否则 blocked_parent_state |

## 优化点间清理协议

1. 保留 `RUN` 下原始证据、最终记录、失败候选 patch/目录及 accepted parent_state。记录候选自己创建的文件清单，禁止按整个 working tree 状态批量 checkout、rm 或 stash，也不以 clean working tree 为前提丢弃用户修改。
2. 优先在隔离候选目录实验和修改。下一候选开始前，把实际父目录源码清单/内容摘要与已验收父状态对照（包括用户原有未提交内容）；若在原目录有本候选的明确改动，仅在可验证的候选 patch 范围内恢复，冲突或所有权不明则 blocked_parent_state。
3. 临时文件只按本 attempt 创建的明确清单处理；原始日志一律保留在 RUN。不能通配删除用户文件、测试输入或构建产物。修复耗尽/退化/跳过均适用相同隔离核验；无法核验则停止后续修改。

## 阶段总结与确认协议

### 1. 生成阶段总结

各阶段核心总结要点：GatherContext（优化类型/路径/测试用例）、ParseIntent（优化目标/风险/平台/性能目标）、PrepareProject（编译参数/基线/机器架构/platform_match）、DecomposeTasks（热点函数/子任务列表）、AnalyzeTestcase（性能画像：规模/瓶颈类型/缓存场景/预热风险）、AnalyzeHotspot（动态分析/静态分析/优化点列表）、AnalyzeCallerContext（调用者列表/触发维度/caller 优化点/skipped 维度）、DesignExperiment（假设/判据/预算）、ValidateExperiment（次数/关键证据/转向原因）、DecideOptimization（策略/目标架构/证据引用）、ApplyOptimization（修改文件/编译状态）、VerifyOptimization（编译/测试/性能/git 操作）、FixCode（错误类型/修复次数/最终状态）、轮次汇总（优化点数/applied/skipped/speedup）。

### 2. 写入总结文件

文件路径规则：`<repo.path>/optimization_reports/run_<run_id>/round<round>_<stage_name>_subtask<sub_id>_opt<opt_id>_v<version>.md`。准备阶段无 round 前缀，轮次级无 sub/opt 后缀。例：`task_verifyOptimization_subtask1_opt2_v1.md`。

同时必须写入机器可读 JSON artifact，供 batch driver 做强证据判定：

- 阶段级：`<repo.path>/optimization_reports/run_<run_id>/stages/<stage>.json`，其中 `<stage>` 使用小写阶段名，如 `prepareproject.json`、`analyzehotspot.json`、`verifyoptimization.json`。
- 优化点级：`<repo.path>/optimization_reports/run_<run_id>/points/round<round>_<function>_<opt_id>.json`，记录 `decideOptimization`、`applyOptimization`、`adversarialReview`、`verifyOptimization` 的核心 JSON 输出和状态。
- 最终汇总：`<repo.path>/optimization_reports/run_<run_id>/batch_result.json`，必须包含 `pipeline_status`、`quality_status`、`applied_count`、`verified_count`、`clean_patch_files`、`blocked_reason`、`performance_summary`、`diagnostic_summary`（每点 hypothesis_id/experiment_count/evidence_refs/transition_reasons/final_status）及三项验收 verdict。
- PrepareProject 因构建、测试、依赖或 baseline 失败而阻塞时，额外写 `<repo.path>/optimization_reports/run_<run_id>/baseline_blocked.json`，包含 `blocked_stage`、失败命令、错误摘要、缺失依赖和重试建议。

Markdown/transcript 只能作为人类阅读材料；自动 batch 判定以这些 JSON artifact 为准。

### 3. 询问用户确认

**auto 模式**：跳过 question，自动同意（等同于用户选择"同意，继续下一步"）。review criteria 不通过时的重试逻辑不变（最多 3 轮），重试耗尽后直接标记 BLOCKED（不询问用户是否放弃或强制继续）。

**collaboration 模式**：question 提供"同意，继续下一步"和"不同意，需要重做"两个选项，展示阶段核心要点、总结文件路径和超时默认值：`120 秒无输入将默认选择：同意，继续下一步`。

若等待用户确认超过 `context.interaction_policy.ask_timeout_seconds`：
- 阶段确认 → 默认同意，继续下一阶段
- AnalyzeHotspot 补充/勾选/重做选择 → 默认同意 AI 发现的优化点，不补充
- AnalyzeCallerContext 选择 → 默认跳过并记录 `timeout_default_skip`
- 轮次继续选择 → 默认自动继续
- retry/resume/continue 选择 → 默认继续

### 4. 处理用户反馈

- **同意**：进入下一阶段
- **不同意**：提取反馈 → 注入 prompt → 重新分发 → 版本递增 → 重新写入总结 → 再次确认
- 最多重试 3 次，3 次后仍有异议：
  - auto 模式 → 对非诊断门控的只读分析阶段记录 warning 并继续；对 PrepareProject、编译、功能测试、working tree 安全等硬失败标记 BLOCKED 或路由 FixCode
  - collaboration 模式 → 非证据门控阶段可询问是否放弃/继续；Design/Validate/Verify gate 与父状态安全不能强制继续，保存阻塞原因

版本追踪和重置规则见上方「协调者状态」中的 `stage_versions` 字段。

## 最终输出格式

详见 `prompts/partials/final-summary.md`
