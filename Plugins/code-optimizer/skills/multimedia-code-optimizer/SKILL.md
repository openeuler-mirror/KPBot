---
name: multimedia-code-optimizer
description: Linux AArch64 媒体与计算密集型代码优化的统一入口，每次调用都先让用户重新选择 origin／summary／optimize 三种模式（不复用上次选择，也不因请求里提到某个模式而跳过确认）：origin 用领域优化知识与微架构性能建模改进实现并做独立验证；summary 从项目或前后两个仓库逐个函数提炼可复用的 C→NEON／C→SVE／NEON→SVE 向量化方法并增量融合进 summary/ 方法库；optimize 按热点逐函数做 SVE2p1、256 位向量化改写，并用位精确正确性测试和 A/B 实测证明收益。适用于指定函数、循环或局部代码片段优化，基于 Top-down／perf 报告优化热点代码，总结或沉淀 SIMD/NEON/SVE 向量化经验，用 SIMD/NEON/SVE 优化 ARM 项目，或对热点函数做向量化并验证收益。仅面向 Linux AArch64，x86 SSE/AVX 与 GPU 场景不适用。
---

# 多媒体代码优化

本技能是 Linux AArch64 媒体与计算密集型代码优化的统一入口。每次调用后的第一件事都是让用户在 **origin**、**summary**、**optimize** 三个模式中重新选择一个，再进入该模式的完整流程。一次调用只执行一个模式，模式之间不自动切换；用户要换模式就重新走模式选择。

| 模式       | 一句话                                                          | 详细指令                       |
| ---------- | --------------------------------------------------------------- | ------------------------------ |
| `origin`   | 原有流程：领域知识 + 微架构建模，候选迭代、独立正确性验证与写回 | `references/modes/origin.md`   |
| `summary`  | 只读扫描，把向量化改写提炼成可复用方法，增量融合进方法库        | `references/modes/summary.md`  |
| `optimize` | 逐函数 SIMD 改写，位精确正确性门 + A/B 性能门，实测证明收益     | `references/modes/optimize.md` |

## 输入

### 第 0 步：选择模式（硬门控，每次调用都重问）

**每次执行本技能都要重新完成这一步。** 上一次调用选过什么模式、会话历史里出现过什么、`other/state.json` 或最终报告里记过什么，都不能作为跳过提问的理由。未确定本次模式前，不读源码、不扫描仓库、不启动 Workflow、不追问优化细节。

1. 每次调用都调用 `ask_user_question` 问这一个问题。即使用户请求里已经出现 `origin`、`summary` 或 `optimize` 字样，也要把三个选项重新摆出来让用户确认；可以把用户提到的那个选项写进问题里（例如“你在请求里提到 optimize，请确认本次模式”），但不能替用户点选。
2. 三个选项的说明必须让用户能判断差异：
   - `origin`：已有 Top-down／perf 数据或明确优化目标，想要微架构建模、多候选迭代与独立验证写回。
   - `summary`：想总结项目里已有的 SIMD 改写，或对比改前改后两个仓库，沉淀成可复用方法库；不改被扫描代码。
   - `optimize`：想直接改 ARM 项目代码，逐函数做 SVE/NEON 向量化，并要求位精确正确性和实测加速。
3. 不要用默认值替用户决定，不要把上一次的选择标成推荐，也不要因为某个模式更省事而引导用户。
4. 模式只在本次调用内有效：不要把选择写成跨调用状态，也不要从其他调用的 `state.json`、报告或历史结论里推断本次模式。
5. 唯一不重问的边界：同一次调用内部因上下文压缩被接续，且本次调用已经拿到过用户答案；这时沿用本次答案继续，不算新的调用。

### 第 1 步：按模式收集输入

模式确定后再收集该模式需要的输入：**本次调用内**已提供的字段不重复问，缺失的集中问一次；上一次调用留下的输入不算已提供。字段清单以对应模式文件的“输入”节为准，速查：

| 模式       | 必需                                        | 可选                                                              | 不要猜的                               |
| ---------- | ------------------------------------------- | ----------------------------------------------------------------- | -------------------------------------- |
| `origin`   | `repo_path`、`operator`                     | `profile_paths`、`build_scope`、`max_candidates`、`writeback`     | 用户未给目标时不得按库类型或经验猜热点 |
| `summary`  | 子模式 `project` 或 `different`，及对应路径 | `scope`、`merge`                                                  | 子模式和路径不得用默认值替用户决定     |
| `optimize` | `project_path`，以及第一/二/三问的答案      | `hotspot_file`、`compile_cmd`、`sample_data`、`budget`、`targets` | 验证方式与优化节奏不得替用户决定       |

## 执行步骤

### 1. 读模式文件（不可跳过）

选定模式后的第一条动作，是**完整读取**对应的模式文件：

- `origin` → `references/modes/origin.md`
- `summary` → `references/modes/summary.md`
- `optimize` → `references/modes/optimize.md`

模式文件是该模式唯一权威的步骤、门控和输出契约来源。未读完不得执行命令、扫描代码或改写代码；文件读不到就停下告诉用户，不要凭记忆继续。

### 2. 按模式文件执行

严格按模式文件里的阶段顺序执行，不跳步、不跨模式借用步骤。三个模式共享同一份技能资源：方法库 `summary/`、`scripts/`、`assets/`、`workflows/`。`summary/` 同时是 summary 模式的写入目标和 optimize 模式的必读知识源，summary 模式整理出的方法就是 optimize 模式的起点。

### 3. 收尾

按模式文件规定的输出契约返回结果并结束本轮调用。用户要换模式时，重新从第 0 步开始选择，不复用上一个模式的中间状态。

## 输出

| 模式       | 输出                                                                       | 写入位置                                              |
| ---------- | -------------------------------------------------------------------------- | ----------------------------------------------------- |
| `origin`   | 准备／优化／验证结果 JSON，主 Agent 按保存的结果简短汇报                   | 任务指定的准备、运行、验证目录与仓库工作区            |
| `summary`  | `simd_summary_result` JSON（含 `coverage`、`methods`、`summary_paths`）    | 只写 `summary/`；扫描中间文件放 `/tmp`                |
| `optimize` | `simd_optimize` JSON（含 `verify_method`、逐函数 `status`、`next_action`） | 只写 `simd_optimization_result/` 与通过门控的目标函数 |

两个结构化模式的顶层形状如下（一次调用只输出其中一个；这里只列关键字段，完整字段与取值见对应模式文件的“输出”节）：

summary：

```json
{
  "simd_summary_result": {
    "success": true,
    "coverage": "complete",
    "error_message": ""
  }
}
```

optimize：

```json
{
  "simd_optimize": {
    "success": true,
    "next_action": "await_user",
    "blocked_reason": ""
  }
}
```

`origin` 不新增顶层结构，沿用准备／优化／验证三个结果文件。`success=false` 时必须在 `error_message` 或 `blocked_reason` 写清原因；summary 的 `coverage` 取 `complete`／`partial`，optimize 的 `next_action` 取 `next_function`／`await_user`／`done`／`blocked`。未通过门控的产物不得写成通过。

## 规则

- **先选模式再动手。** 跳过模式选择或替用户选模式，会让三套流程的门控互相污染：summary 会试图改代码，optimize 会跳过验证，origin 会绕过 Workflow 的写回核对。
- **一次调用一个模式。** 不复用其他模式的中间产物、状态或结论；模式切换必须由用户发起。
- **只面向 Linux AArch64。** origin 与 optimize 都只在 AArch64 上做优化；x86 SSE/AVX、GPU 和其它架构明确说明不适用。summary 模式只做代码阅读，不受架构限制，但只归纳 C／NEON／SVE 三类改写。
- **写入边界固定。** summary 只写 `summary/`，不碰被扫描仓库；optimize 只写 `simd_optimization_result/` 和验证通过的目标函数；origin 只通过 Workflow 写回，不手工改仓库。
- **`summary/manual/` 是用户的。** 只在缺失时创建，已有内容不读、不改、不合并、不删除。
- **方法库不记来源、不记性能。** 仓库名、路径、行号、commit、作者、URL 和性能数字都不进 `summary/`；性能结论留在 optimize 模式的测试与报告里。
- **证据必须真实。** 没跑过的测试、没测过的数字、没读过的函数体都不能写成结论；未验证就标未验证。
- **重问边界按调用划分。** 新的调用必须重新询问入口模式和该模式的全部条件（origin 的输入、summary 的子模式与路径、optimize 的项目／热点／验证／节奏），不复用上一次调用的答案；只有同一次调用内部被中断或压缩时，才读本次已答记录、只补未完成的部分。
- **不越权。** 不自动 commit / push，不改平台权限配置，不删除用户文件与历史方法。

## 参考资料

- `references/modes/origin.md`：origin 模式的完整流程、任务 JSON、Workflow 启动与恢复、候选迭代与写回。
- `references/modes/summary.md`：summary 模式的扫描、逐函数提炼、bundle 合并与质量门。
- `references/modes/optimize.md`：optimize 模式的优化原则、四问、逐函数闭环、三个门控与输出契约。
- `references/stages.md`：origin 模式各阶段子 Agent 的职责与交接。
- `references/arm-simd-optimization/`：ARM 指令、寄存器、谓词、VL 与编译分派知识，origin 模式的 Knowledge 阶段使用。
- `references/media-optimization-knowledge/`：媒体算法与通用计算优化知识，origin 模式的 Knowledge 阶段使用。
- `summary/README.md`：向量化方法库导航，summary 模式维护、optimize 模式读取。
- `scripts/`：`workflow_state.py` 管 origin 的流程状态；`init_summary.py`、`merge_summary.py`、`scan_simd.py`、`validate_summary.py` 属 summary；`check_env.py`、`init_workspace.py`、`scan_candidates.py`、`compare_outputs.py`、`run_ab_bench.py`、`render_report.py` 属 optimize。
- `assets/bench/` 与 `assets/templates/`：optimize 模式的裸机基准夹具与记录模板。
- `cases/`：`cases/summary-mode-integrity/`、`cases/optimize-mode-integrity/` 两个 Tier-A 用例；`evals/evals.json` 记录路由样例。
