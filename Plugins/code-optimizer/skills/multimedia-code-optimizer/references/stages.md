# 阶段职责

服务 origin 模式，供 Workflow 阶段代理按当前阶段读取，输出字段由对应 schema 定义。所有阶段在 Linux AArch64 上执行，不安装或升级环境，不 commit、不 push。每个任务共用一个临时 worktree，以版本 ID、Git tree、补丁和 context 关联源码、构建产物与证据。仅 Writeback 可向原仓库应用已验证补丁并清理本任务 worktree；其余阶段不改原仓库工作文件或索引。

BuildRequirements、Knowledge 使用本技能 `agents/` 下的 `multimedia-build-requirements`、`multimedia-knowledge` 只读代理。其余阶段不再委派规划或分析代理。

## BuildRequirements

只读已有构建配置、编译数据库和工具链文件，沿用项目已有或默认构建方式，识别构建范围内的必要工具、依赖、参数与入口，不绑定单个优化目标的实现。返回编译器选择和版本要求、按需 C++、有序编译参数、工具链配置、构建工具及依赖、配置依据与构建入口。`build_tools` 与 `dependencies` 只列所选构建方式的必需项，并与 `build_hint` 一致；互为替代的构建方式不合并为需求。

需求明确即可返回，不定位工具、不编译。用户明确选择的编译器交给 Environment；项目默认 GCC 不等于用户指定 GCC，应识别 LLVM／毕昇构建支持。

## Environment

从 PATH、用户指定目录和工作区定位工具。用户指定优先，否则优先满足项目需求的上游 LLVM，再选鲲鹏毕昇；根据实际版本识别。必需 Git、C 编译器、`llvm-mca`、`llvm-objdump`；项目需要 C++ 时再要求 C++ 编译器，其他工具与依赖按项目要求。分析工具可来自独立 LLVM 安装，不假定毕昇附带；perf 不作为必需项。

运行 skill 目录下的 `scripts/probe_environment.py --compiler <路径> --mca <路径>`，通过重复的 `--build-tool` 检查项目工具与 objdump；C++ 项目加 `--cxx`，编译参数用 `--compiler-arg=<参数>`／`--cxx-arg=<参数>`。用 `python3 -I` 执行。脚本检查工具版本与本机计算扩展的编译能力，临时产物自动清理。不另写 ISA 探针、不做项目构建。

MCA 模型优先使用用户指定值；否则依据固定探测中的 CPU 型号，匹配相邻 `llvm-mca-analysis/scripts/mca_analyze.py` 支持的模型及别名，匹配不到则默认 `hip12`（鲲鹏 950）。在 `reason` 中记录指定、匹配或默认的选型依据。PATH 首位放所选 MCA 工具目录，统一通过该脚本的 `--check-env` 结果核对选定模型支持；不支持则返回 `blocked` 和具体缺项。

原始探测 JSON 保存为 `<preparation_dir>/evidence/check-<check_revision>/environment/probe.json`。返回 CPU、ISA、SVE 位宽、工具绝对路径与版本、参数、构建方式和模型预检；`build_tools` 保留需求中的名称。平台信息由固定探测脚本采集。

工具、依赖、编译能力和模型满足要求即返回 `ready`。缺项返回 `blocked`，提示用户准备。

## SourceAnalysis

先确认 `repo_path` 为 Git 仓库根目录，记录 HEAD 为 `base_commit`。使用临时 `GIT_INDEX_FILE` 从该提交建立索引，收录当前工作文件的已跟踪修改和非忽略的未跟踪源码，写出 `base_tree`；不改原仓库索引，不使用 stash 或创建提交。不能由该 tree 表达的输入（如未提交的子模块内容）先返回阻塞，不遗漏后继续。保存从 `base_commit` 到 `base_tree` 的完整二进制补丁 `run_dir/baseline.patch`，空差异也保留空文件。补丁使用仓库相对路径，涵盖新增、删除、模式与二进制变更。

新任务使用 `mktemp -d` 在 `run_dir` 内以 `worktree-XXXXXXXX` 模板分配唯一空目录，得到八位随机后缀的 `worktree_dir`，不在原仓库内放置任务目录。将 `repo_path/worktree_dir/base_commit/base_tree/baseline_patch_path` 记录到 `run_dir/workspace.json`，在该空目录创建 detached worktree 并还原 `base_tree`。恢复时先读取该记录，核对 Git 注册路径与仓库归属后复用，不重新生成路径或取原仓库当前 HEAD 重建基线。后续阶段与清理只使用记录中的完整路径，不用通配符查找或删除。构建产物、testbench 和证据分别保存在任务指定的构建、验证及证据目录，不放入原仓库。

读取准备文件并核对传入对象，在任务 worktree 中依据已确定的 `operator` 定位目标代码，目标为循环或局部片段时一并确认所在函数的上下文。沿调用与分派查找相关的标量、ARM 及已有 x86 实现，不另选优化对象。提炼输入规模、布局、语义约束、调用者数据复用与融合机会，记录实际进入目标实现的条件和已有测试入口。原实现的 ISA 不是优化上限。

读取 `profile_paths` 中与目标代码有关的热点／Top-down 数据，保留对应输入场景。返回 `execution_context` 和 `bottleneck_hypothesis`（类别、依据、证据路径）；用户指定目标但没有运行数据时仅给源码支持的假设，不采集新的性能数据。

## BaselineBuild

读取 `workspace`，在任务 worktree 中还原 `base_tree`。按准备配置优先构建目标及必要依赖到 `run_dir/build/baseline`，不默认构建全库。缺环境返回 `needs_environment` 和缺项、失败命令、工作目录及日志；其他构建错误返回 `blocked`。

在 `context.json` 保存 workspace、准备路径、编译器、构建命令、目标产物和入口。优先使用编译数据库的真实记录，保留参数顺序、生成头文件路径及逐文件 flags。返回基线版本 `{id: baseline, tree: base_tree, patch_path: baseline_patch_path}`、构建目录与 context，不复制源码。保留构建产物供后续复用；不写测试、不计时。随后独立 MCA 分析基线。

## Knowledge

复用源码、已有性能数据与基线 MCA，从 `skill_dir/references/INDEX.md` 按目标代码的计算模式、数据流和瓶颈选择方法，结合相关算法与等价计算模式检索。先在 `isa_comparison` 中结合环境支持、数据布局、工作量和实际调用入口，比较现有实现内优化与适用的跨 ISA 方向；已有 NEON、SVE／SVE2 等实现一并作为参照，说明各方向纳入或不纳入的依据。搜索范围遵循用户明确的 ISA 限制，而非函数名或当前实现使用的 ISA；既考虑局部指令变换，也考虑批处理、布局和融合。ISA 判断分开引用本机执行能力、工具链编译能力与所选 MCA 模型的指令支持；三者不能互相替代，也不凭 CPU 名称印象排除方向。复用环境探测和工具支持证据，依据不足的适用方向保留为待核实，交给 Implement 确认。

`methods` 按尝试优先级交付最多五项。`method` 描述一个核心变换及公式，`rationale` 保存排序依据、收益假设和供 Implement 用 MCA 评价的性能问题，`reason` 说明整体排序。`conditions.confirmed/pending` 分别列已确认／待核实的正确实现条件，每项以 `kind` 标识数学语义、输入范围／布局、指令扩展／VL 或接口／分派，以 `requirement` 陈述所需事实和约束。条件依据源码和准备结果核实；指令成本、资源压力、依赖与 spill 对收益的影响由后续 MCA 评价，留在 `rationale`。`source` 给出知识条目路径。

## Implement

Workflow 以 `media:implement` 启动一个 Implement Agent，交接准备结果、源码约束、基线及其 MCA、Knowledge 完整 `isa_comparison` 和按优先级排列的 `methods`（含 `method/rationale/conditions/source`）。构建命令与产物沿用 `context.json`。同一 Agent 在同一上下文内自主修改、编译、按下节规范调用 MCA 并调整，不设外层 Implement 循环或候选 MCA Agent。

依据知识、编译诊断和 MCA 反馈选择方向，按 `conditions.pending` 核实语义、取值范围、扩展／VL、接口与分派条件，完成实现及必要接入。允许局部编译、条件探查和多种写法；可调整方向或回到较早版本继续，不要求先修复最后一个失败候选。片段 test 与完整正确性验证仍由独立验证阶段完成。

`max_candidates` 默认 4，是新增留档候选预算，不含基线，失败候选也计入。候选按冻结顺序使用 `c1`、`c2`…，始终在同一个任务 worktree 中修改，不复制源码目录、不额外创建 worktree。完整版本进入构建与 MCA 前，将源码（含新增文件）写为 Git tree，并保存相对 `workspace.base_tree` 的完整二进制补丁 `run_dir/candidates/<id>.patch`。构建产物保存到 `run_dir/build/<id>`，日志及 MCA 证据保存到 `run_dir/evidence/<id>`。评价结束立即留档、计入预算并更新 checkpoint，再继续修改。完整内核即使采用局部编译命令、位于临时目录，也不能在完成编译与 MCA 后留在预算外；单独指令探查不计数。构建失败同样留档。

每个候选记录 `version`（`id/tree/patch_path`）、`parent_id`、`transform`、`build_status`、`log_path`、`mca`。`parent_id` 可为基线或较早候选，只记录探索来源；所有候选补丁均直接相对任务基线，不串接父候选补丁。切换方向前保存当前候选或未完成尝试，再在本任务 worktree 还原目标 tree；不覆盖已有补丁、构建产物或证据。`transform` 简述相对父版本的变换；构建配置沿用 context。`build_status` 为 `built` 或 `failed`，失败时 `mca=null`，成功时 `mca.status` 为 `analyzed` 或 `unavailable`。

新任务开始实现前初始化 `run_dir/implementation.json`，字段为 `status/reason/candidates`，未完成时 `status=running`；每个候选评价完成后立即更新完整记录，再开始下一个候选，不在结束时集中补写。失败或放弃的方向及原因随手简记 `run_dir/attempts.md`。无新方向时结束为 `converged`，不必凑满预算；预算用尽为 `budget_exhausted`，无法继续为 `blocked`，`reason` 简述依据。已有候选保留，自测结果单独记录，不计入最终独立验证。

中断恢复复用前置 stage 结果，读取 workspace、checkpoint、attempts 与候选补丁，先保存 worktree 中未留档的尝试，再核对并补齐中断候选记录；已有候选继续计入总预算，沿用 ID，不重复分配或覆盖。恢复依赖持久化证据接续。结束时返回完整 `candidates`，由 Workflow 脚本选优；worktree 当前内容不代表选中版本。

## MCA

本规范由基线 MCA 独立 Agent 与 Implement 共用。复用相邻 `llvm-mca-analysis/scripts/mca_analyze.py`，不改脚本、不调用其整套 Skill。用准备结果的 objdump 从对应冻结版本的真实编译产物提取完整核心计算区域，保留该工作量所需的加载、计算和存储，只用 `--asm`。汇编语法整理仅去除地址列与反汇编注解、将符号操作数转为等价的汇编表示，保持区域内的指令、寄存器、立即数和依赖关系不变；分析前逐条对照原始反汇编。合成汇编只作方向探索，不作为正式候选成本或收益证据。按已确认模型执行，PATH 首位放所选 MCA 工具目录。

原版与候选固定模型、等价计算工作量、迭代数和归一化口径，并记入 context。`unit` 固定为 `cycles/work-item`；`workload` 定义一个逻辑工作项的计算语义和数据规模，由首个有效分析确定，后续结果沿用并由脚本核对该定义，不锁定函数名。基线分析依据传入 `source` 的调用场景与输入输出约束确定计算范围，融合候选也须覆盖同一工作量；超出已确定范围且无法等价比较时，正式结果为 `unavailable`，不把不同计算范围的成本放入同一候选集选优。`work_items_per_iteration` 表示所提取区域每次迭代完成的工作项数，可随批处理或向量宽度变化。确认脚本报告成功后，`cost` 直接取结构化结果 `llvm_mca_analysis_result.summary.cycles_per_iteration` 除以该数量，数值与单位、工作量定义分开交付。

`bottleneck` 提炼主要瓶颈，IPC 辅助解释。资源压力、依赖链、数据搬运与 spill 用于指导后续修改，不另写原始 JSON 解析或二次聚合。不跳过缺调度数据的计算指令来制造可比成本。

保存原始反汇编、区域汇编与脚本报告，核对版本身份，得到成本和主要瓶颈即返回。恢复时复用身份与分析口径一致的已有报告；不另换迭代数复测、不重复探测环境。无法有效分析或口径不可比则 `unavailable`、成本为 null。MCA 评价指令调度成本，不承担缓存未命中、分支预测和多线程的实际收益判断。

## Workflow 选优

脚本核对 workspace、候选 ID、tree、父版本、补丁与证据路径、构建状态及预算，检查 MCA 的版本身份、模型、工作量定义、单位和归一化工作项数。有效结果须构建成功、`mca.status=analyzed` 且成本可比；从 `baseline` 与有效候选中选择最低成本，平局保留较早版本，全部不可用时 `selected=null`。优化输出 `schema_version=5`；`current` 为最后留档候选，无候选时为基线，`selected` 独立保存。选优不另设代理，不限定后续候选必须从 `current` 接续。

## ValidationSetup

读取优化结果文件，核对与输入对象一致。依据 workspace、基线与选中版本的 tree 和补丁确定修改范围，不能使用 worktree 中最后一次尝试代替选中版本。沿用准备阶段的本机工具链与第二阶段构建 context。在唯一的任务 worktree 中先还原基线、构建并链接独立的基线测试入口，再还原选中 tree、构建并链接候选测试入口；构建目录分别为 `validation_dir/baseline/build` 与 `validation_dir/candidate/build`，不复制源码或创建第二个 worktree。核对选中补丁从基线还原得到的 tree 与 `candidate.tree` 一致，记录两版 `id/tree/patch_path` 到验证 context。两版入口及运行所需资源均保存在验证目录，不能在运行基线测试时引用已切换为候选内容的源码或动态库。

在验证目录的 `harness/` 为修改片段编写 test，复用项目测试设施与比较规则，直接调用原版和选中版的真实实现，以相同输入比较输出。测试范围覆盖相关边界、极值和固定种子随机输入；涉及布局、stride、尾部、非对齐、分派或批处理的改动，覆盖相应影响。不复制一份算法替代被测实现，不默认运行整个媒体端到端流程。

在本机完成两版及测试入口的构建链接，冒烟与完整用例使用独立入口或运行模式。冒烟只用最小输入确认目标实现可调用、输出可读取，执行命令、输入范围、退出码和日志记录在 `smoke`；入口就绪即返回 `ready`，完整正确性由下一阶段执行。

返回固定 `correctness` 配置：`scope` 说明修改及测试范围，`test_paths` 列出验证目录内测试文件的绝对路径，`commands` 仅安排使用已构建入口的输入／原版参考输出准备、完整片段比较、比较器自检及负向对照，不包含构建、源码切换或 testbench 修改命令；同时交付输入覆盖与比较规则。实际构建映射、冒烟记录和配置归档到独立的 `context.json`，不改第二阶段 context。不准备计时入口。

## Correctness

在本机按 `correctness.commands` 顺序使用已构建入口，先准备测试输入和原版参考输出，再执行片段比较及负向对照。确认两版真实实现使用相同输入，按配置覆盖修改及其影响范围。整数精确一致，浮点沿用项目规则并记录误差。入口准备归 ValidationSetup，本阶段只执行测试并交付正确性证据，不修改源码或 testbench、不重复构建、不计时。

返回命令、工作目录、实际与预期退出码、日志、通过／失败数；区分验证、辅助命令和负向对照。实际通过才为 `passed`；错误为 `failed` 并保存可重放输入，未能完成为 `pending_validation`。验证结果附在 `selected` 上，返回使用的两版 `id/tree/patch_path`，不自动修代码、改选或追加迭代。选中原版时直接保留原版，不重复验证。

## Writeback

本阶段只由 Workflow 在非基线候选独立验证 `passed` 且 `writeback=true` 时调用。固定接收目标仓库、workspace、基线、选中版本、正确性结果和 `validation_dir/writeback.json`；不重新分析优化，不改源码或 testbench，不添加注释或兜底代码。

核对验证身份与选中版本一致，确认候选补丁相对任务基线，且补丁内容仍对应选中 tree。检查原仓库当前工作文件：补丁涉及文件必须仍与基线一致，其他文件的用户修改保留。先检查补丁可应用，再记录 `pending` 回执及涉及文件，使用 `git apply` 写入工作文件，不使用 `--index`、`--3way` 或 `--reject`，不修改用户索引。核对涉及文件的内容、模式、新增与删除状态均等于选中 tree，其他文件未改变，保存 `applied` 回执。冲突或应用失败返回 `blocked`，不自行合并或重写代码。

写回核对成功后，检查 `workspace.json` 与 Git worktree 注册信息，确认路径确为本任务创建的 detached worktree，并将 task worktree 中未留档内容保存到任务目录。仅删除该 worktree，不删原仓库、其他 worktree、候选补丁、构建产物或证据。该临时 worktree 含未提交候选时可在上述核对完成后使用 `git worktree remove --force`；不使用通配清理或删除整个任务目录。清理成功后保存 `completed` 回执；清理失败保留 `applied=true`、`cleanup=retained` 并返回阻塞。

恢复先读取回执并核对原仓库涉及文件。已有 `applied` 时只继续清理；`pending` 且涉及文件已全部等于选中 tree 时补记已写回，不重复应用；文件既不等于基线也不等于选中版时返回冲突。worktree 已删除且回执与写回内容一致时直接完成。固定返回 `id/tree/patch_path/status/reason/applied/cleanup/changed_files/log_path`，`cleanup` 为 `removed` 或 `retained`；只在写回和清理都成功时返回 `completed`。
