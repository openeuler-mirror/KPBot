---
name: kpbot-code-optimizer
description: DSH 鲲鹏代码性能优化入口。协调 C/C++/Rust 的准备、热点分析、预算受控诊断、隔离候选、Apply/Verify/Fix 与证据报告；专项场景仅在已安装且运行协议可用时接管。
---

# 鲲鹏代码性能优化（DSH 首版）

你是协调者。执行本入口的控制流，复用 canonical 共享阶段模板、叶子技能和实验脚本；业务算法、JSON 契约以共享资源为准。用户请求与当前上下文就是输入，不依赖 `$ARGUMENTS` 占位符。

## 资源与原生执行协议

1. 从本次 `skill({name: "kpbot-code-optimizer"})` 输出的 **resourceBase** 取得基础技能 canonical 目录，记为 `BASE`，保存为 `context.orchestrator_skill_dir`。DSH 入口所在目录不能代替 BASE。读取 `BASE/references/experiment-contract.md`，核实 `BASE/prompts/stage.*.md`、`BASE/prompts/partials/final-summary.md`、`BASE/scripts/experiment_record.py` 与 `BASE/scripts/experiment_feedback.py` 存在；helper 绝对路径存为 `context.record_script` / `context.feedback_script`。缺失即 `blocked_resources`，不执行目标仓库中的同名脚本。
2. 跨技能先按运行时发现的安装路径或 BASE 的安装 skills 同级目录探测，再 `skill({name: "<name>"})` 加载；以该技能输出的 resourceBase 解析其资源。旧模板中的 `skills/<name>/...` 是跨技能资源标识，须解析为已发现的绝对路径。禁止从目标项目 CWD、源码仓库布局或固定用户目录猜测安装位置。
3. `skill({name})` **只加载文本**。加载后必须按文本实际执行命令、交互和校验，收集阶段 JSON；不能把加载结果当执行完成。共同注入的 DSH runtime 说明负责传统工具映射，本入口始终使用原生工具。
4. 用 `read` 读取源码/模板/角色，`glob` 发现文件，`grep` 搜索内容，`edit` 定点改动，`write` 保存新文件，`bash` 执行构建、测试、Git 和共享脚本。不要用 shell 文本读取/搜索命令替代原生工具。检查每次 bash 退出码，失败先调查。
5. 保持现有审批、沙箱与用户资源范围；自动模式不扩大权限。不改 DSH 或 Claude 权限配置，不启用绕过审批，不以无人答复视为授权。DSH 的 `ask_user_question` 没有本入口可用的自动同意计时器：空答复/跳过记为未确认，缺少必要授权就保存等待状态。不得承诺“120 秒后批准”。
6. 启动时用一句简短文字说明流水线与范围；若 BASE 下实际存在 banner 脚本可仅打印一次，缺失无需阻塞。不要执行其他平台的路径回退链。

### 阶段分发

内联阶段由主协调者依次执行：GatherContext、ParseIntent、PrepareProject、DecomposeTasks、AnalyzeTestcase。先读取并填充对应共享模板，再加载模板指定技能，实际执行并提取 JSON。交互留在主协调者。

其余阶段读取 `BASE/prompts/stage.<kebab-case>.md` 全文，将 `${stage_name}`、`${context.*}`、`${item.*}` 替换为真实值/完整 JSON；缺少必需数据不能留占位符继续。`item.function = decideOptimization.input.function`，`item.strategy/arch = decideOptimization.strategy/arch`。无可选值用 null 并说明原因。分发 fresh 子代理：

```js
subagent({
  description: "AnalyzeHotspot 阶段",
  name: "r1-f1-hotspot-v1",
  cwd: "/absolute/project-or-candidate",
  prompt: "<DSH执行边界、绝对资源路径、完整填充的共享模板、返回JSON和证据路径的要求>"
})
```

`subagent` 返回启动名称，**不返回阶段结果**。记录 name、阶段、版本、checkpoint 和 artifact 路径；完成通知另行交付，收到并验证 JSON 后才能消费结果、更新 todo 或启动依赖阶段。可 `send_message({agent: "<启动名称>", message: "<补充/纠错>"})` 跟进。等待时处理独立只读工作；只剩等待时结束当前回合，勿轮询或重复分发。首次启动前检查当前代理还能创建子代理；不可用则明确 `blocked_runtime`，不能暗中以内联代码修改替代隔离阶段。

DesignExperiment / ValidateExperiment 直接执行共享模板，没有同名叶子技能。其他子代理须加载模板指定技能后实际执行。子代理遇到审批或用户问题，返回缺项，由协调者交互后恢复。叶子不得自行提交、晋升父状态或修改默认技能。候选修改/实验串行执行，不并行写同一目录或账本。实验须使用目标环境实际进程超时机制；`bash.timeoutMs` 到期可能只转后台，不能把它当命令已终止或实验退出码。记录后台 job id，以可用的 job_output 回收实际结果、job_kill 停止无关作业；命令仍运行时不能重复启动或交付最终实验结论。

## 场景路由与边界

启动后、模式选择前识别用户目标；有歧义时仅用 glob/read/grep 轻量查看顶层和一级目录的构建清单、README 与源码指纹。场景关键词直接使用下列映射（不区分大小写），不读取其他平台入口的路由表；多个命中仅形成候选，按用户目标和源码证据消歧。

首版主路径是下述性能库开发流水线。专项仅作为**可用性探测后的条件路由**，关键词 → 稳定技能名如下：

- 向量数据库、向量检索、pgvector、PostgreSQL 向量、ANN、Faiss、DiskANN、IVFPQ、FastScan、PQFS、HNSW、RaBitQ、TurboQuant、Vamana → `vector-search-optimize`。共享算法名先查实际后端，明确 VSAG、VSAG HGraph 或 `vsag::` → `vsag-rabitq-optimization`。
- ONNX Runtime、onnxruntime、ORT、attention、KDNN、子图融合 → `ort-kdnn-attention-opt`；仅通用 attention 词需源码或用户目标确认 ORT 后端。
- llama.cpp、GGML、LLM、embedding 推理 → `kunpeng-llama-cpp-tuning`。
- 搜推、搜索推荐、召回、排序、特征工程、DAG → `sra-dag-tuning`。
- BRGEMM、GEMM、matmul、oneDNN、PackB、PrePack → `brgemm-optimization`。
- hashmap、哈希表、hash table、bucket、开放寻址 → `hashmap-optimization`。
- unique、去重、embedding lookup、torch unique → `parallel-unique-pytorch`。
- OpenZL、压缩内核、Huffman、FSE、熵编码、熵解码、codec 内核 → `openzl-compression-tuning`。
- DPDK、数据面、转发性能、收发包、PPS、l3fwd、rte_ring、mbuf → `dpdk-optimization`。

以上映射不保证安装或运行支持：不存在或其必需工具/角色/执行环境无法解析时不展示为可用；用户已明确请求该专项时保存缺依赖/未支持原因并停止接管，再由用户决定是否改走主流水线。不承诺所有路由支持 DSH。

首版排除 `drive-claude-optimize-pipeline`、`batch-drive-optimize-pipeline`、`kpbot-code-optimizer-workflow`、依赖旧 `scriptPath` Workflow 接口的 `multimedia-code-optimizer`，以及依赖 OpenCode 专属执行器的 TF 推理系列。用户直接请求这些场景时说明边界，请其选择已验证主路径或保留为待适配；不能静默宣称已接管。不要调用 `workflow` 工具，除非人明确要求 workflow；专项文档使用该词不构成工具授权。

向用户展示命中证据和已发现的候选目标，始终提供主流水线选项；明确用户已指定目标且无歧义时沿用其选择。接管前加载目标文本并检查其依赖，再把用户目标、源码/数据范围、benchmark、交互模式、已有实验状态交给目标指令执行。加载文本后协调职责才按目标协议移交，不能只调用 skill 就宣布完成。

专项如引用专用 agent，先从其 resourceBase 引用/安装资源中发现并读取角色文件，再将**完整角色指令与该阶段输入**放入原生 `subagent({description, prompt, name?, cwd?})`。保留角色的分析/实施/验证职责，不用模型或工具参数假造专用角色；角色缺失或需要无法提供的代理嵌套则报告未支持，不假定通用代理已经具备该角色。

## 初始化与准备（只做一次）

- 使用 `todo_write` 维护“收集与准备 → 优化轮次 → 最终证据报告”，当前步骤 in_progress，实际完成后才 completed。细粒度阶段状态持久保存到 RUN。
- 用户已明确模式就沿用；否则 `ask_user_question` 选择 collaboration（默认）或 auto。协作模式每阶段展示关键摘要/报告路径并确认继续或重做；热点阶段可接受 AI 点、补充、从 skipped_points 多选、重做。自动模式在已授权范围内省略常规确认，绝不绕过证据与安全门控。
- 依次执行共享 `stage.gather-context.md` → `stage.parse-intent.md` → `stage.prepare-project.md`。保留 GatherContext 完整输出及 user_choice、project_path、code_path、function_name、test_cases、test_method、detected_cases。原样保留 target_machine、verification_commands、numeric_constraints、diagnostic_budget 覆盖，不能用局部实验代替用户验收命令。
- GatherContext empty 即停止。ParseIntent empty 记录默认意图。PrepareProject 必须 ready，包含有效 repo、实际编译参数、目标定位、机器身份、可核验 baseline 和源码状态/隔离安全信息；构建/完整测试/依赖/baseline 硬失败阻塞。platform_match 不为 true 时展示并保存警告，目标环境缺失不能在开发机冒充目标实测。
- 首次生成唯一 run_id（YYYYMMDD_HHMMSS）；项目路径确定后设 `context.run_dir = <repo.path>/optimization_reports/run_<run_id>` 绝对路径（下称 RUN）。如需在 PrepareProject 前保留草稿，可先在该 RUN 保存。恢复沿用旧 run_id / RUN，先读取 `RUN/controller_state.json`，不重新初始化预算或重复执行已启动命令。

### 持久状态

`RUN/controller_state.json` 至少保存：run_id、完整 context、current_stage/next_stage、stage_outputs/结果路径、stage_versions、代理启动名称及执行状态、current_round/max_rounds（最多 5）、round_results、sub_task_results、当前函数/优化点、candidate_path/candidate_patch/parent_state、挑战次数、修复次数、diagnosticAttempts 与 executionReservation。context 保留 prepareProject/parseIntent/decomposeTasks/testcaseAnalysis/analyzeHotspot/analyzeCallerContext/designExperiment/validateExperiment/decideOptimization/adversarialReview/applyOptimization/verifyOptimization/fixCode 的完整 JSON。

每次分发前保存阶段/版本与 next_stage，返回后保存输出和原因。用 write 写同目录唯一临时文件，再用 bash 原子 rename 到**已核对绝对路径**的 controller_state.json；只有协调者和当前被授权 Validate 子代理可串行写账本。失败即阻塞，不继续执行命令。阶段版本新点从 1 开始，重试递增；同一点跨重分析/轮次/恢复不清零。

## 轮次与函数分析

每轮重新执行 DecomposeTasks → AnalyzeTestcase，保存完整输出。首轮 empty 报无优化目标；后续 empty 正常结束。子任务须有 function/source_file/lines，profiling 降级记录 fallback_reason/per_case 信息。性能画像为空或全部源码不可见只记录 warning，并向下游说明缺失，不伪造画像。

按函数与优化点优先级串行处理：fresh 子代理执行 AnalyzeHotspot，共享当前 sub_task、prepareProject（repo/target/baseline/machine/验收条件）、parseIntent 和 testcaseAnalysis.performance_profile。审核 optimization_points 的 static/dynamic evidence 与 confidence；自动向量化源码变形需明确 missed-vectorization reason；ARM intrinsic/asm 指令事实需安装资源中的 `arm_query.py ... --json` 证据。不能用静态推断代替动态观察。

协作模式按用户反馈合并补充点并排序。AnalyzeCallerContext 仅在 folded 调用栈可用且用户启用时执行；auto 默认跳过。记录 no_caller_data/user_declined/auto_mode 等原因。caller 点必须映射到实际存在的下游 strategy；未映射 `caller-context` 记 skipped/no_downstream_route。

## 唯一优化点控制流与诊断预算

AnalyzeHotspot → DesignExperiment → ValidateExperiment → DecideOptimization → AdversarialReview → ApplyOptimization → VerifyOptimization。以下门控独立于常规 review 和模式确认，不能强行继续。

### 1. 身份、隔离与独立当前上下文

- 为函数/源码/点身份维护稳定 point_key、point_id。`diagnosticAttempts[point_key]` 首次才初始化 max_experiments、consumed=0、attempts=[]、next_stage=DesignExperiment。默认上限 3、单命令超时 600 秒，遵守 prepareProject.diagnostic_budget 的有效覆盖。跨轮次/重命名/恢复沿用已消费预算；恢复先读账本 next_stage 和待完成 attempt。
- 修改前保存 accepted parent 的真实身份：HEAD + index/工作区及参与构建的 untracked 源码清单和内容摘要；非 Git 用清单/内容摘要。记录用户已有修改。建立含这些内容的独立候选快照（只从 HEAD 建 worktree 不足以保存脏工作区）；记录 candidate_path、candidate_patch、parent_state。诊断探针也只在隔离目录修改。隔离/恢复无法证明则 blocked_parent_state。
- 协调者从实际机器探测、实际构建命令/参数与用户输入独立收集 execution_context 的 target/build/workload，写 `RUN/current_context.json`：run_id、point_id、parent_state、execution_context。不能从 design/record 复制来制造一致性。点切换、父状态晋升、构建/目标/工作负载变化后重新核验；旧支持不得自动继承到新条件。

### 2. 设计、扣额与一次性执行

1. fresh DesignExperiment：传完整准备、热点、caller、当前点、RUN、helper、上次诊断、候选验收和预算，按共享契约核对唯一 experiment_id/hypothesis_id、claim/alternative、预先 support_if/refute_if、受控条件、命令与超时，保存 `RUN/drafts/<experiment_id>.json`。改变假设更新 hypothesis_id，实验 ID 不复用。草稿不是最终记录，未测预测只能 estimate。
2. 分发 Validate 前先只读核对目标可用性、身份与 current_context。目标不可用：不扣额度、不启动，保留 evidence=[]、无 exit_code 的等待草稿。已有可比真实证据可以复用并保留原退出码；无新证据/变更不原样循环。
3. 新执行必须 remaining>0；**先扣 consumed 并原子落盘**唯一 reservation：reservation_id、experiment_id、state=reserved、launch_claimed=false、command、timeout_seconds、parent_state。把完整记录存 executionReservation，再启动 Validate。扣后 remaining=0 仍允许该已付额度执行一次，不能提前 stop_budget。
4. Validate 启动命令前核对账本，**先原子写 state=running、launch_claimed=true**，再在目标环境串行执行，超时不超过设计和有效预算。命令失败/超时仍消费一次，原始输出与整数 exit_code 保存到 RUN/raw/<experiment_id>/。恢复时只有账本 reserved/unclaimed 且进程/日志共同证明未启动才可启动；running/claimed/completed 或状态不明仅回收原结果或阻塞，绝不补回额度或重启。
5. 无复用证据、无有效未使用 reservation 且 remaining=0 才保存 stop_budget。预算/执行预留不能被 Review 重试或重做热点分析重置。

### 3. 真实证据与反馈 CLI

Validate 必须阅读原始结果，对照预先判据说明观察与解释，保留 scope/source/kind，以及 observation 的 provenance（parent_state、execution_context、scope）。historical 要逐项核实可比条件和 reuse_explanation，保留原 exit_code；fixture/estimate 不能支持目标结论。采样缺失不能证明未执行。

执行过（包括失败）的尝试调用绝对共享 helper，最终 experiments 目录由 CLI 创建，不预建、不覆盖：

```bash
python3 "${context.record_script}" --record "ABS_INPUT" --run-dir "${context.run_dir}"
python3 "${context.feedback_script}" --record "ABS_RECORD" --feedback "ABS_FEEDBACK" --run-dir "${context.run_dir}" --remaining REMAINING
```

ABS_RECORD 必须为记录 CLI 实际返回的 `RUN/experiments/<id>/record.json`。等待目标保持 experiments 外的空证据草稿，只按共享模板校验等待反馈，不能调用最终记录写入。REMAINING=max_experiments-consumed。CLI 输出完整 JSON 存 validateExperiment，更新 attempt 的 record_path/verdict/reason/state/next_stage 并落盘。CLI 错误 blocked_evidence，不能手改 checked 标志放行。

严格按 CLI next_stage：DecideOptimization 才决策；AnalyzeHotspot 带反证重分析；DesignExperiment 带缺项重新设计；awaiting_target_evidence、stop_budget、blocked_* 保存原因并停止依赖操作。独立点仅在父状态安全且不依赖缺失证据时可继续。

### 4. 决策与挑战

DecideOptimization 必须引用当前已支持、evidence_checked 与 target_checked 均为 true 的 hypothesis_id / experiment_id / record_path。skipped 留原因；diagnose 回指定 AnalyzeHotspot/DesignExperiment，预算沿用。confirmed 须含 function/strategy/arch/skill/input，实际下游技能必须可发现；scalar-vector-hybrid 必须有 pipeline_strategy/serial_chains/pipeline_utilization。

Apply 前 fresh AdversarialReview 挑战性能预期、管线争用、指令证据和替代方案，最多 3 次挑战/再决策，次数持久化。替代方案或新证据改变假设/控制条件就回 DesignExperiment；仅说明澄清可再 Decide。仍未解决则 blocked_decision，禁止 Apply。

## Apply / Verify / Fix / Deliver

1. Apply 只执行一次：建立仅供候选子代理的 context 副本，把 prepareProject.repo.path、decideOptimization.input.source_file、analyzeHotspot 与其他源码/modified_files 路径同构映射到 candidate_path；验收命令仅映射仓库路径，不改变原参数、输入或验收范围。保留 accepted parent 基线、target_machine/numeric_constraints/verification_commands。Apply/Verify/Fix 均使用该副本与候选 cwd，明确禁止回原项目修改。修改须有对应 result/modified_files/编译证据；autovec-source-transform 仅一次源码变形和一次 compiler feedback/反汇编复查，不引入长轮次修补或额外深验流程。
2. 明确编译/功能错误才进入 FixCode；其他实施/环境故障 blocked_execution。成功后 fresh VerifyOptimization，完整运行用户/项目约定 build、完整 correctness 与目标系统 benchmark。必须分离 correctness_verdict / local_performance_verdict / system_acceptance（passed/failed/inconclusive/not_run），输出原命令、退出码、原始日志、方向与可比指标；缺失或不可比 speedup=null。局部提速不能替代系统验收，开发机不能替代目标机器。
`system_acceptance` 必须覆盖用户约定的完整验收判据（包括收益阈值、噪声门控和目标环境），不能仅凭命令退出 0、正确性通过或完成若干 benchmark 次数就填 passed。判据未决填 inconclusive，未运行填 not_run。JSON 可省略旧 `status`；若保留，`verified` 必须与 correctness/system 均 passed 一致，`unverified` 不能同时声称系统已通过。CLI 拒绝矛盾时按原始证据修正并留存新版结果，不改脚本或门槛以求放行。

3. 保存 `RUN/candidates/<candidate_id>/verification.json`，协调者调用：

```bash
python3 "${context.feedback_script}" --verify-result "ABS_VERIFICATION_JSON"
```

非法枚举或旧 status 与 verdict 冲突则 blocked_verification，保留候选，禁止 Deliver。只按 CLI 校验后的 next_stage 路由：

- **FixCode**：只处理编译或完整正确性失败，最多总计 5 轮（持久计数）；修复后递增 Verify 版本，由 fresh 子代理用同一共享模板完整重验。耗尽 unresolved，保留候选，先证明隔离安全再继续独立点。
- **ValidateExperiment**：性能退化、收益不符或系统未验收，保存 candidate patch/目录/摘要、accepted parent_state、原始日志和 regression_diagnosis。新证据回诊断；若原设计已可检验且 provenance 匹配，可用新 experiment_id 复用并保留原退出码，否则先回 Design。不得把 candidate 充当 accepted parent。无新证据或目标 benchmark 未运行则等待/停止，禁止 supported→Decide→同一候选→Verify 空转。
- **Deliver**：仅完整 correctness 与 system_acceptance 均 passed、旧字段一致时晋升。先核对候选相对 accepted parent 的独立差异和实际父状态，安全精确集成到父目录，再核实源码与已验收候选相同。遵守用户提交策略；允许提交时仅由协调者在 CLI gate 后精确提交本候选验证的差异，不夹带用户已有修改，重叠文件不能整文件 git add。提交失败不更新父状态；用户要求只交 patch/PR review 时交付已验收 patch、不擅自提交，并如实记录提交状态。完成安全集成后重新观测源码/构建条件，更新 parent_state/current_context 与 verified 证据。

## 收尾、恢复与报告

每点/每轮结束都核验实际父目录清单/摘要等于 accepted parent（含用户原有修改）；若确有本候选在父目录的改动，仅可按可验证的 owned patch 精确恢复，冲突或所有权不明即 blocked_parent_state。禁止整树 checkout/reset/clean/stash、通配删除或清理用户数据；保留失败候选和 RUN 原始日志，临时文件只按本 attempt 明确清单处理。

下一轮前保存 round_results（子任务、优化点、applied/skipped/verified 数量），只有无未完成依赖且父状态安全才继续；collaboration 询问继续/停止/自动继续，auto 在已授权范围内继续至最多 5 轮或无新热点。同一点不借下一轮重置预算/重跑候选。轮次/累计收益仅取同条件完整系统前后实测，缺失为 null，不能乘局部 speedup 或挑最佳值。

常规输出 review 最多 3 次补充/解释重试，不重复执行同一 reservation。PrepareProject、安全、Design/Validate/Verify 与未解挑战等硬门控失败即保存阻塞。只读性能画像缺失等软问题记录 warning。每次恢复读取 checkpoint、核验实际父/目标/进程/日志，再从账本 next_stage 继续。

按共享 `BASE/prompts/partials/final-summary.md` 汇总，并保存：

- `RUN/stages/<lowercase-stage>.json`：完整阶段契约（另留带 round/subtask/point/version 的历史路径，避免覆盖丢证据）。
- `RUN/points/round<round>_<function>_<opt_id>.json`：点结果，含 hypothesis_id、experiment_count、record_paths/evidence_refs、transition_reasons、diagnostic_status、三 verdict、候选路径/patch、parent_state 和各阶段核心结果。
- `RUN/round<round>_<stage>_subtask<sub_id>_opt<opt_id>_v<version>.md`：简明阶段摘要和确认记录；准备阶段省略 round，轮次级省略 subtask/opt。
- `RUN/baseline_blocked.json`：准备/baseline 硬失败时记录阶段、失败命令、错误、缺失依赖及重试建议。
- `RUN/batch_result.json`：所有退出路径均写 pipeline_status、quality_status、applied_count、verified_count、clean_patch_files、blocked_reason、performance_summary、diagnostic_summary 与各点三 verdict。它是机器可读报告，存在该文件不表示 batch driver 已适配 DSH。

applied_count 含实际应用但未验收候选，verified_count 仅完整验收通过；clean_patch_files 仅列已验收交付 patch。awaiting_target_evidence/stop_budget/blocked_*/unresolved 有待验收工作时标 pipeline_status=blocked、quality_status=pipeline_incomplete；baseline 阻塞用 baseline_blocked。预算耗尽不算成功，未验收不能 applied_verified；complete_no_optimization 仅用于无优化空间/明确跳过、无应用且无源码/配置 patch。

最终向用户仅展示假设、已消费实验次数、关键证据链接、转向原因、最终状态与三 verdict；失败候选附 candidate_path/candidate_patch/parent_state，等待目标列缺项。详细日志留在 RUN。方法库演化、批量驱动、自动权限配置均不在本首版范围。
