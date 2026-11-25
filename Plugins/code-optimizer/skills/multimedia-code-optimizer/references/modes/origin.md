# origin 模式：原有优化流程

本文件是 origin 模式的完整指令，行为与合并前的 multimedia-code-optimizer 一致。

你负责协调 Linux AArch64 媒体计算密集型代码优化：确定优化目标、管理任务、调用与恢复 Workflow、处理用户决策并汇报结果。源码分析、实现、验证及写回清理由阶段子 Agent 执行，主 Agent 不直接修改或重新实现候选代码。各阶段的操作与交接见[阶段职责](../stages.md)。

入口的[模式选择](../../SKILL.md)已经完成；确认用户选择的是 origin 后再执行本文件。文中的路径都相对技能根目录解析。

## 输入

**每次调用都重新收集本节的输入，不沿用上一次调用留下的任务 JSON。** 收集 Git 仓库根路径、优化目标和已有 Top-down／perf 数据。优化目标可以是算子、函数、循环或局部代码片段，用函数名或源码文件与代码范围定位；缺失信息集中询问，已提供的不重复问。性能数据路径记录为 `profile_paths`，不主动采集 perf 或准备素材。

用户指定目标时直接采用，已有性能数据辅助分析，没有也可继续。用户未指定目标时，只依据其提供的 Top-down／perf 数据定位具体热点代码，记录报告位置、输入场景和选择依据到 `instructions`；不按库类型、源码印象或经验猜选。没有数据或数据不能定位具体代码时，请用户补充优化目标或报告，确定对象后再启动 Workflow。默认逐个优化目标。

任务 JSON 包含：`repo_path`、`operator`、`skill_dir`，以及仓库外相互独立的 `preparation_dir`、`run_dir`、`validation_dir`。路径均使用绝对路径；`operator` 记录优化目标及其源码位置，不限于函数名。`instructions` 保存用户要求和目标选择依据，其他选项按需填写：

- 准备：`build_scope` 表达构建范围，默认项目媒体库构建；`check_revision` 默认 1。具体构建目标与开关由 BuildRequirements 确定。
- 优化：已有准备文件填 `preparation_path`；`max_candidates` 默认 4，限制新增留档候选数，不含基线，失败候选也计入；已有性能数据填 `profile_paths`。
- 验证与写回：已有优化文件填 `optimization_path`；`writeback` 默认 `true`，用户明确要求不写回时设为 `false`。片段验证在运行 skill 的本机执行，可按用户要求暂缓；未独立验证通过不写回。

## 执行步骤

### 1. 选择流程与检查环境

| 已有输入 | 启动流程 | 阶段 |
|---|---|---|
| 库路径与已确定的优化目标 | `workflows/preparation.js` | BuildRequirements → Environment |
| 适用的 `ready` 准备结果 | `workflows/optimization-loop.js` | SourceAnalysis（创建 worktree）→ BaselineBuild → 基线 MCA → Knowledge → Implement |
| 已完成的优化结果 | `workflows/validation.js` | ValidationSetup → Correctness → Writeback |
| 同一任务中断 | 原控制目录的 `resume` | 从指定阶段接续 |

同一项目、机器、工具链和构建配置下可复用准备结果；构建范围、工具链或机器改变时更新准备。新一轮优化使用新的 `run_dir` 和控制目录，只复用准备，不继承旧优化历史。必需 Git、编译工具链、`llvm-objdump`、`llvm-mca` 及可用的目标调度模型，由 Environment 检查；缺项提示用户准备，不自动安装。

### 2. 启动 Workflow

调用 `python3 -I <skill_dir>/scripts/workflow_state.py start --state-dir <新控制目录> --script <对应流程脚本> --args <任务文件>`，新控制目录由 helper 创建，无需预先 mkdir。脚本读取结果文件、填入 `preparation`／`optimization` 对象，并冻结任务、流程与阶段规范，恢复沿用同一契约。旧源码快照任务保留原产物，新建 worktree 任务，不跨契约恢复。无需查找原控制目录或抄写结果对象。

把输出的 `scriptPath` 直接交给 Workflow；用 `record --state-dir ... --run-id ...` 记录返回的 run ID，可用 `--journal` 指定该运行日志。启动并记录后结束当前回合，由 Workflow 完成事件接续。

### 3. 衔接与恢复

收到完成事件后，调用 `collect --state-dir ... --run-id ... --output <任务输出文件>`。脚本读取运行时 JSON 的完整 `result`，原样保存为准备目录的 `preparation-<check_revision>.json`、优化目录的 `optimization.json` 或验证目录的 `validation.json`。任务输出用于结果收集，子阶段 journal 用于恢复。

准备 `ready` 后进入优化；优化正常结束且有 `selected` 才进入验证。后续流程调用 `start --state-dir <下一控制目录> --script <下一流程脚本> --from-state <上一控制目录>`，脚本继承任务并填入结果对象和路径；需要调整目录或用户选择时附加 `--args <任务调整文件>`。

阻塞或中断先处理当前阶段。旧运行结束后，调用 `resume --state-dir ... --stage <stopped_stage>`，从 journal 提取前序结果，生成新的启动文件；将其交给 Workflow 并记录新 run ID。Implement 的 label 为 `media:implement`，恢复时读取 `run_dir/workspace.json`、`implementation.json`、`attempts.md` 与候选补丁，已有候选继续计入预算。Writeback 的 label 为 `media:writeback`，恢复时复用已完成的验证结果并读取 `validation_dir/writeback.json`，已写回的不重复应用，只接续未完成的核对或清理。必要说明用 `--note`，准备环境更新用 `--check-revision <新修订号>`。缺工具提示用户准备，不安装或升级环境。

### 4. 候选迭代与选优

每个任务仅在迭代目录内创建一个 `run_dir/worktree-<8位随机字符>`，由 SourceAnalysis 初始化并纳入原仓库已有未提交修改。实际路径保存到 `workspace.json`，后续阶段、恢复和清理均使用该路径，不按名称猜测或重新创建。版本以 Git tree 与补丁记录，不创建提交；候选不再保存整份源码目录。基线保存在 `run_dir/baseline.patch`，候选保存在 `run_dir/candidates/<id>.patch`，构建产物和证据位于 worktree 外。各候选补丁均相对任务基线，`parent_id` 只记录探索来源。

基线由独立 Agent 执行 MCA 分析；Knowledge 的完整 `isa_comparison` 和含 `rationale` 的 `methods` 交给一个 Implement Agent。Implement 在同一个 worktree、同一上下文内自主“修改 → 编译 → MCA → 调整”，不设外层 Implement 循环或候选 MCA Agent。正式评价前冻结候选补丁与 tree，评价完成立即留档、计入预算并更新 `implementation.json`，再继续探索；局部编译与指令探查不单独计数，也不限制 MCA 调用次数。

脚本核对候选身份、补丁路径、模型和工作量口径，从基线与有效候选中选择最低 MCA 成本，平局保留较早版本，全部不可用则 `selected=null`。优化输出 `schema_version=5`；`current` 仅表示最后留档候选，`selected` 独立保存。未完成的实现记录为 `running`，结束为 `converged`、`budget_exhausted` 或 `blocked`；无新方向即可收敛，不必凑满预算。

### 5. 正确性验证与写回

验证保持两个子 Agent：ValidationSetup 在同一 worktree 中依次还原并构建基线和选中版，在验证目录准备 testbench、链接独立入口并执行最小冒烟测试；Correctness 只运行这些已构建入口，执行完整正确性验证，不修改代码或 testbench、不重复构建。测试覆盖修改及其影响，不默认扩展为媒体端到端测试；整数精确一致，浮点沿用项目规则。不做性能计时，不自动改选或追加修复轮次。

Workflow 仅在选中非基线候选、独立验证 `passed` 且 `writeback=true` 时启动 Writeback 子 Agent。它应用已验证的候选补丁、核对写回结果，再删除本任务创建的临时 worktree；不重新实现代码，不 commit、不 push。写回冲突或失败保留 worktree。用户要求不写回、验证未通过或选中基线时跳过 Writeback，保留 worktree 并说明状态。

## 输出

主 Agent 按保存的结果简短汇报：`optimization.json` 提供选中补丁与 MCA 对比；`validation.json`（`schema_version=3`）提供独立验证覆盖、用例统计，以及 `writeback` 中的修改文件、写回和清理结果。Implement 自测单独标识，不混入独立验证计数。MCA 表示调度成本，不作为真机加速结论。补丁、构建产物和证据不随 worktree 清理删除，不另写逐阶段报告。
