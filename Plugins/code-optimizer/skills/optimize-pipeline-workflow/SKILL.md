---
name: optimize-pipeline-workflow
description: 鲲鹏性能优化流水线（Workflow 版）— 与 optimize-pipeline 功能等价，但优化核心循环委托给 Workflow 工具执行，提供确定性编排、结构化输出验证和并行执行能力。当用户想要优化 C/C++ 项目性能时触发。
---

# 鲲鹏性能优化流水线（Workflow 版）

你是一位鲲鹏性能优化流水线的协调者（Orchestrator）。你的职责是将任务分发给专业子代理，并追踪进度。

**与 `optimize-pipeline` 的区别**：本版本将 Phase 3 的优化核心循环（函数级 AnalyzeHotspot → 优化点级 Decide→Challenge→Apply→Verify→Fix）委托给 Workflow 工具执行。Workflow 提供 `pipeline()`/`parallel()` 确定性编排、结构化 JSON Schema 验证和函数级并行分析。用户交互、环境预检、DecomposeTasks、报告写入仍由 Orchestrator 直接处理。

若 Workflow 不可用，自动回退到 Agent subagent 模式（与 `optimize-pipeline` 行为一致）。

## 架构

```
optimize-pipeline-workflow (Orchestrator)           ← 主 Agent
  ├── Pre-flight: Sandbox & Permissions Setup
  ├── Phase 0: Mode Selection
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
  │    ├── 3.2 [Workflow 模式, 默认]                 ← ⭐ 核心变更
  │    │   ├── Prompt 组装（模板 + SKILL.md 合并填充）
  │    │   └── Workflow({ optimization-round.js })
  │    │        ├── 函数级 pipeline: AnalyzeHotspot → [CallerContext]
  │    │        └── 优化点级 pipeline: Decide → Challenge(3×parallel) → Apply → Verify → [Fix → Re-Verify]
  │    │
  │    ├── 3.2f [Subagent 回退]                      ← Workflow 不可用时
  │    │   └── 逐函数 Agent() 分发（与 optimize-pipeline 一致）
  │    │
  │    └── 轮次汇总 → 继续/停止?
  │
  └── Phase 4: 多轮最终汇总
```

五层分析模型与 `optimize-pipeline` 一致：轮次循环 → DecomposeTasks（用例级）→ AnalyzeTestcase（测试用例级）→ AnalyzeHotspot（函数级）→ 优化 Skill（优化点级）。

## 核心分发协议（CRITICAL）

阶段分发分三种模式：**内联阶段**、**Workflow 委托**（新增）、**Subagent 回退**。

### 内联阶段：主 Agent 直接执行

适用：GatherContext、ParseIntent、PrepareProject、DecomposeTasks、AnalyzeTestcase — 编排准备层，不涉及代码生成。

```
Skill({ skill: "<skill-name>", args: "$ARGUMENTS" })
```

### Workflow 委托：优化核心循环（新增，默认）

适用：Phase 3 的函数级优化循环内的所有阶段。Orchestrator 预先组装 prompt（模板 + SKILL.md 合并），通过 Workflow 工具一次性委托整个优化循环。

```
# Step 1: 组装 prompts
for stage in [analyze-hotspot, ..., fix-code]:
  template = Read("skills/optimize-pipeline/prompts/stage.<name>.md")
  skill_content = Read("skills/<skill-name>/SKILL.md")
  full_prompt = template + skill_content
  填充静态变量，保留 {{动态占位符}}
  assembled_prompts[stage] = full_prompt

# Step 2: 委托 Workflow
Workflow({
  scriptPath: "skills/optimize-pipeline-workflow/workflows/optimization-round.js",
  args: { sub_tasks, prepareProject, prompts: assembled_prompts, ... }
})
```

### Subagent 回退：Agent tool 分发

Workflow 不可用时使用，与原 `optimize-pipeline` 行为完全一致：

```
Agent({
  description: "<阶段名称> 阶段",
  subagent_type: "general-purpose",
  prompt: "<填充了 ${context.xxx} 变量后的完整模板内容>"
})
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
  test_method: null
  detected_cases: null     # [{name, description, type}]
  gatherContext: null
  # 准备阶段填入（只跑一次）
  parseIntent: null
  prepareProject: null
  decomposeTasks: null     # 每轮重新执行
  testcaseAnalysis: null
  # 轮次循环
  current_round: 0
  max_rounds: 5
  round_results: []
  auto_continue: false
  pipeline_mode: "collaboration"  # "auto" | "collaboration"
  # ═══ Workflow 相关状态（新增） ═══
  use_workflow: true              # 是否使用 Workflow 模式（默认 true）
  prompts_assembled: false        # 本轮 prompts 是否已组装
  assembled_prompts: {}           # { analyzeHotspot, decideOptimization, ... }
  workflow_failed: false          # 本轮 Workflow 是否失败（触发回退）
  # 函数级子任务循环（每轮重置）
  current_sub_task: null
  sub_task_index: 0
  sub_task_results: []      # [{id, function, status, speedup, fix_info, description, optimization_point_results}]
  # 函数级各阶段输出（仅 Subagent 回退模式使用）
  analyzeHotspot: null
  analyzeCallerContext: null
  user_supplemented_points: []
  # 优化点级子循环（仅 Subagent 回退模式使用）
  optimization_point_index: 0
  optimization_point_results: []
  current_optimization_point: null
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
    DecomposeTasks: 1
    AnalyzeTestcase: 1
    AnalyzeHotspot: 1
    AnalyzeCallerContext: 1
    DecideOptimization: 1
    ApplyOptimization: 1
    AdversarialReview: 1
    VerifyOptimization: 1
    FixCode: 1
```

## Stages 配置

与 `optimize-pipeline` 完全相同的 stages 配置，详见原文件 `skills/optimize-pipeline/SKILL.md` 的 Stages 配置章节。本文件不重复定义，所有 reviewCriteria 和 post-stage 逻辑保持一致。

## 分发逻辑

当收到用户请求时，按以下流程执行。Phase 2（准备阶段）和 Phase 4（最终汇总）与 `optimize-pipeline` 完全一致，详见原文件。Phase 3 的核心变更如下。

### 前置步骤 0：模式选择

与 `optimize-pipeline` 完全一致：通过 `AskUserQuestion` 选择 `collaboration` 或 `auto` 模式，存入 `context.pipeline_mode`。

### 前置步骤：环境预检（Sandbox & Permissions）

与 `optimize-pipeline` 完全一致：检测 sandbox 状态，引导用户选择配置方案。

### Phase 0：信息收集与运行初始化

与 `optimize-pipeline` 完全一致：生成 `run_id`，准备进入 Phase 2。

### Phase 1：创建任务列表

与 `optimize-pipeline` 完全一致：通过 TaskCreate 创建阶段任务。

### Phase 2：准备阶段（只跑一次）

与 `optimize-pipeline` 完全一致：GatherContext → ParseIntent → PrepareProject，全部由主 Agent 内联执行。

### Phase 3：优化轮次循环

PrepareProject 完成后，进入迭代式优化轮次。

```
round = 1
while round <= max_rounds:
```

#### 3.0 轮次初始化

```
  a. 设置 context.current_round = round
  b. 若 context.pipeline_mode == "auto" 且 round == 1 → context.auto_continue = true
  c. 重置轮次级状态：
     - context.decomposeTasks = null
     - context.sub_task_results = []
     - context.sub_task_index = 0
     - context.workflow_failed = false
     - context.prompts_assembled = false
     - context.assembled_prompts = {}
     - context.stage_versions.DecomposeTasks = 1
  d. 若 round == 1：
     - TaskUpdate({ taskId: <优化轮次循环 taskId>, status: "in_progress" })
  e. 创建本轮任务组：
     round_task_id = TaskCreate({
       subject: "第 ${round} 轮优化",
       description: "重新 profiling 发现热点 → 分解子任务 → 应用优化（Workflow 模式）",
       activeForm: "第 ${round} 轮优化"
     })
```

#### 3.1 执行 DecomposeTasks 和 AnalyzeTestcase

与 `optimize-pipeline` 完全一致，主 Agent 内联执行：

```
  a. DecomposeTasks: Skill({ skill: "decompose-tasks", args: "$ARGUMENTS" })
     → context.decomposeTasks
     → status == "empty" → 退出轮次循环
  b. AnalyzeTestcase: Skill({ skill: "analyze-testcase", args: "$ARGUMENTS" })
     → context.testcaseAnalysis
     → 提取 performance_profile
```

#### 3.2 [Workflow 模式] 优化执行（默认路径） ⭐

本节是 Workflow 集成的核心。Orchestrator 预先组装所有阶段的 prompt，然后委托 Workflow 一次性执行整个优化循环。

##### 3.2.0 Prompt 路径传递（简化版）

协调者**不再手动读取 prompt 文件**。只需确定 prompt 文件目录路径，传给 Workflow。Workflow 内的 agent 在执行时自行 Read 需要的 prompt 文件。

```
a. 确定 prompt 根路径：
   prompt_root = "<pipeline_root>/skills/optimize-pipeline-workflow/prompts"

   <pipeline_root> 解析：从 skills/optimize-pipeline-workflow/SKILL.md 的真实路径向上两级。
   当 skill 位于 ~/.claude/skills/... 软链接下时使用 resolved path。

b. 无需组装 assembled_prompts 对象。
   无需手动 Read 任何 prompt 文件。
   标记 context.prompts_assembled = true 即可。
```

##### 3.2.1 调用 Workflow

```
a. 【关键检查】在构造 args 之前，确认以下值非空：
   - context.decomposeTasks.sub_tasks 必须是数组且非空
   - context.prepareProject 必须存在
   - prompt_root 必须是有效的目录路径字符串
   
   若 context.decomposeTasks.sub_tasks 为空数组或 undefined，
   说明 DecomposeTasks 未产生有效子任务，应停止本轮而非调用 Workflow。

b. 构造 Workflow args（⚠️ 所有 8 个字段必须全部包含）：
   workflow_args = {
     run_id: context.run_id,
     round: context.current_round,
     sub_tasks: context.decomposeTasks.sub_tasks,     // ← 必填！数组
     prompt_root: "<上一步确定的 prompt 根路径>",      // ← 必填！字符串路径
     prepareProject: {                                  // ← 必填！对象
       repo: context.prepareProject.repo,
       baseline: context.prepareProject.baseline,
       binary_path: context.prepareProject.binary_path,
       machine: context.prepareProject.machine,
       architecture_file: context.prepareProject.architecture_file,
       microarch_file: context.prepareProject.microarch_file,
       instruction_perf_file: context.prepareProject.instruction_perf_file
     },
     intent: context.parseIntent,
     performanceProfile: context.testcaseAnalysis.performance_profile,
     testMethod: context.test_method
   }

c. 调用 Workflow 工具（⚠️ CRITICAL: args 必须是 JSON 对象，不是字符串）：

   ⛔ 错误写法（会导致 Workflow 把 args 当字符串解析）：
   workflow_result = Workflow({
     scriptPath: "...",
     args: "{\"sub_tasks\": [...], ...}"        ← 这是 JSON 字符串！不能这样！
   })
   workflow_result = Workflow({
     scriptPath: "...",
     args: """
     sub_tasks: [...]
     prompt_root: ...
     """                                       ← 这是 YAML 文本！不能这样！
   })

   ✅ 正确写法（args 是内联 JSON 对象，不要引号包裹）：
   workflow_result = Workflow({
     scriptPath: "<pipeline_root>/skills/optimize-pipeline-workflow/workflows/optimization-round.js",
     args: workflow_args     ← workflow_args 已经是 JSON 对象，直接传入即可
   })

   <pipeline_root> 解析：从 skills/optimize-pipeline-workflow/SKILL.md 的真实路径向上两级。
   当 skill 位于 ~/.claude/skills/... 软链接下时使用 resolved path。

d. 若 Workflow 调用成功：
   - context.sub_task_results = workflow_result.sub_task_results
   - 记录统计：optimization_points_total / applied_count / skipped_count / failed_count
   - 跳转到 3.3 轮次收尾

e. 若 Workflow 调用失败（抛出异常、返回错误、或超时 >30min）：
   - log "Workflow 执行失败：<错误信息>。回退到 Subagent 模式。"
   - context.workflow_failed = true
   - 跳转到 3.2f [Subagent 回退模式]
```

##### 3.2.2 处理 Workflow 结果

```
a. 验证结果结构：
   - workflow_result.sub_task_results 非空
   - 每个 sub_task_result 含 id/function/status/optimization_point_results

b. 同步到协调者状态：
   - context.sub_task_results = workflow_result.sub_task_results

c. 清理临时文件：
   cd <repo.path> && rm -f perf.data perf.data.old spe_*.data core.* *.o *.tmp 2>/dev/null

d. 进入 3.3 轮次收尾
```

#### 3.2f [Subagent 回退模式] 函数级子任务循环

当 Workflow 不可用时（`context.workflow_failed == true` 或 Workflow 工具不存在），使用与 `optimize-pipeline` 完全一致的 Agent-based 分发逻辑。

回退触发条件：
- Workflow 工具不可用（旧版 Claude Code）
- Workflow 脚本语法错误
- Workflow 执行超时（>30min）
- 用户通过阶段确认明确要求 Subagent 模式

回退执行逻辑详见 `skills/optimize-pipeline/SKILL.md` 的「函数级子任务循环」和「优化点级子循环」章节。核心流程：

```
for sub_task in sub_tasks:
  Agent({ prompt: "<analyzeHotspot 模板填充>", ... })
  for opt_point in optimization_points:
    Agent({ prompt: "<decideOptimization 模板填充>", ... })
    if confirmed:
      # AdversarialReview → Apply → Verify → [FixCode → Re-Verify]
      Agent({ ... })
```

**注意**：Subagent 回退模式下，AnalyzeCallerContext 和用户补充优化点的交互逻辑与 `optimize-pipeline` 完全一致，但不享受 Workflow 的并行化和结构化验证。

#### 3.3 轮次收尾

与 `optimize-pipeline` 完全一致：

```
  a. 汇总本轮结果
  b. TaskUpdate({ taskId: <round_task_id>, status: "completed" })
  c. 终止判断（applied_count == 0 或用户选择停止）
  d. 安全阀（round > max_rounds）
```

#### 3.4 轮次间清理

与 `optimize-pipeline` 完全一致：确保 working tree 干净。

### Phase 4：多轮最终汇总

与 `optimize-pipeline` 完全一致。从 `context.round_results` 提取数据，按 `skills/optimize-pipeline/prompts/partials/final-summary.md` 格式汇总。

## 模板变量填充规则

Workflow 模式下，变量分两层填充：

| 填充时机 | 填充者 | 变量类型 | 示例 |
|---------|--------|---------|------|
| Prompt 组装时 | Orchestrator | 静态上下文 | `${context.prepareProject}`, `${context.parseIntent}` |
| Workflow 运行时 | Workflow JS | 动态上下文 | `{{SUB_TASK}}`, `{{OPT_POINT}}`, `{{ANALYZE_HOTSPOT_RESULT}}` |

完整变量映射表详见 `skills/optimize-pipeline/SKILL.md` 的「模板变量填充规则」章节，两者一致。

## 阶段总结与确认协议

与 `optimize-pipeline` 完全一致：
- Workflow 模式：Workflow 返回后 Orchestrator 生成总结，写入报告文件，根据 `pipeline_mode` 决定是否询问用户确认
- Subagent 回退模式：每阶段完成后确认

## Review Loop 实现

Workflow 模式下，review 在 Workflow 内部通过 JSON Schema 验证实现。`agent({ schema: SCHEMA })` 确保返回合法 JSON，不满足 schema 时自动重试（最多 3 次）。

Subagent 回退模式下，与 `optimize-pipeline` 的内联 review 完全一致。

## 复用清单

本 Skill 复用以下现有资源，不重复定义：

| 资源 | 路径 | 用途 |
|------|------|------|
| Workflow Prompt 文件 | `skills/optimize-pipeline-workflow/prompts/*.md` | 预清洗的完整 agent prompt（模板+SKILL.md 合并，去除 TaskCreate/Skill tool/AskUserQuestion） |
| 最终汇总格式 | `skills/optimize-pipeline/prompts/partials/final-summary.md` | Phase 4 输出格式（复用原文件） |
| 子 Skill 脚本 | `skills/arm-instructions-query/scripts/` 等 | Workflow agent 通过 Bash 调用（复用原文件） |
| 微架构文档 | `skills/kunpeng_microarch/` | 性能分析参考（复用原文件） |
| Workflow 脚本 | `skills/optimize-pipeline-workflow/workflows/optimization-round.js` | 优化核心循环编排 |

## 与 optimize-pipeline 的差异总结

| 维度 | optimize-pipeline | optimize-pipeline-workflow |
|------|------------------|---------------------------|
| Phase 3 优化执行 | 逐函数 Agent() 分发 subagent | Workflow 委托（pipeline + parallel） |
| 函数级并行 | 串行 for 循环 | pipeline() 多函数并行分析 |
| AdversarialReview | 串行 3 轮 challenge_round | parallel() 3 路并行 skeptics |
| 结构化输出 | 手动解析 JSON | agent({ schema }) 自动验证 |
| 控制流 | LLM 理解自然语言控制流 | 确定性 JS 控制流 |
| 回退机制 | N/A | Workflow 失败 → Subagent 回退 |
| Skill 内容引用 | subagent 自行 Skill() 加载 | Orchestrator 预组装到 prompt |
| 用户交互 | 5 个交互点 | 3 个（Workflow 内无交互） |
