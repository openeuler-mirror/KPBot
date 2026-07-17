# 服务器应用优化智能体技能：Agent + Skill 让 6 路分析 21 分钟产出 16 个候选动作

服务器性能优化正在进入一个新的阶段：客户现场问题越来越复杂，工程师不再只是自己盯着 `perf top`、`mpstat`、`iostat` 和压测日志反复切换窗口，AI Agent 也开始参与排查、整理证据和生成优化报告。

但 Agent 真正参与性能调优后，一个新问题会很快冒出来：它不能只会“给建议”。服务器优化有风险，改错一个 BIOS/OS 参数、绑错 CPU、误判网络瓶颈，都会让测试结果失真。真正可用的 Agent 工作流，必须知道什么时候该采集证据，什么时候该排除方向，什么时候该停下来复测，还要能把这些过程沉淀成下一次客户支撑可以复用的资产。

这就是服务器应用优化智能体技能（Server Application Optimization Skill）要解决的问题。它不是一个单点 profiling 工具，而是一套面向服务器应用优化的 Agent 编排框架。它把一次调优拆成有门控、有证据、有候选池、有复测、有报告自检的完整流程。

在一个脱敏 MySQL 只读样例中，这套 skill 在 21 分钟内完成 6 个专项方向分析，生成 16 个候选动作。随后通过 13 分 10 秒的线程 sweep 找到低风险运行点：TPS 提升 1.07%，P95 延迟下降 21.85%，最大延迟下降 67.17%。进一步的 perf 证据把热点收敛到字符排序路径，query-mix A/B 验证显示该路径解释了 92.72% 的 TPS 差异空间。更重要的是，框架没有把这个 92.72% 包装成服务器参数优化收益，而是明确标注为 workload/query-mix 诊断发现。对客户支撑团队来说，这类产出意味着更少的人工串行排查、更快的问题收敛、更稳定的交付报告。

## 什么是服务器应用优化智能体技能？

服务器应用优化智能体技能是一个主编排 skill，面向服务器应用优化工程师、数据库性能工程师和需要交付性能分析报告的团队。它覆盖环境备份、目标规格约束、基线测试、非 CPU 瓶颈预筛、证据快照、子 skill 分析、迭代优化、最终报告输出和报告自检。

它适用于这些场景：

- 为 MySQL、数据库型服务或其他服务器应用建立可复现性能基线。
- 判断性能问题是否真的落在 CPU，而不是被网络、磁盘或内存带宽限制。
- 将 BIOS/OS、CPU 亲和性、NUMA、编译器、性能库和应用配置放到同一套分析框架里。
- 让 Claude Code、Codex、Cursor、OpenCode 等支持 `SKILL.md` 的 Agent 参与性能优化，并留下可审计证据。

这个 skill 的设计重点不是“让 Agent 更大胆地改参数”，而是让 Agent 更克制地推进优化。主 skill 负责编排路线、门控和证据组织；具体分析由多个子 skill 承担，例如 `cpu-affinity-optimization`、`bios-optimization`、`os-optimization`、`network-optimization`、`compiler-optimization`、`performance-library-selection`、`application-config-optimization` 和 `database-workload-analysis`。

## 架构和工作流

```text
用户输入
  |-- 优化目标：如 MySQL 8U32G 只读场景
  |-- 部署材料：启动脚本、压测脚本、指标定义
  |-- 约束范围：是否允许修改 BIOS/OS/应用配置
  v

服务器应用优化智能体技能（主编排 Skill）
  |
  |-- Step 0：执行前门控
  |     |-- 读取 report-schema / checklist / workflow
  |     |-- 创建执行检查清单
  |     |-- 建立耗时记录纪律
  |
  |-- Step 1-5：建立可信基线
  |     |-- 环境备份
  |     |-- 场景准备
  |     |-- 目标规格约束
  |     |-- 基线测试
  |     |-- 基线确认
  |
  |-- Step 6：非 CPU 瓶颈预筛
  |     |-- 网络
  |     |-- 磁盘
  |     |-- 内存带宽
  |     |-- 客户端压测能力
  |
  |-- Step 6.5：证据快照
  |     |-- 静态拓扑
  |     |-- 运行期指标
  |     |-- 应用状态
  |
  |-- 候选 skill 分析
  |     |-- cpu-affinity-optimization
  |     |-- bios-optimization
  |     |-- os-optimization
  |     |-- network-optimization
  |     |-- application-config-optimization
  |     |-- compiler-optimization
  |     |-- performance-library-selection
  |     '-- database-workload-analysis（数据库场景按需触发）
  |
  |-- 候选池合并
  |     '-- candidate_pool.json
  |
  |-- 串行迭代验证与复测
  |     |-- 选择低风险动作
  |     |-- 记录实施和验证耗时
  |     '-- 形成 continue / stop 决策
  |
  '-- Step 9-9.5：报告输出和自检
        |-- final-report.md
        |-- improvement_summary.json
        '-- 风险、回退、残留工作
```

一次优化从 Step 0 开始。Agent 必须先读取报告 schema、执行 checklist 和 workflow 规则，创建执行检查清单，并从 Step 1 开始记录每个阶段的分析、实施和验证耗时。这样做的目的很直接：性能优化不是事后补一份报告，而是从第一步就让过程可复盘。

随后流程进入环境备份和目标规格约束。例如用户指定 `8U32G`，基线必须在 8 核、32 GiB 目标资源限制下建立，而不是先用整机资源跑出一个更好看的数字。对容器场景，skill 还会记录 `<容器名>`、`<cpuset>`、内存限制和运行时线索，避免把容器边界误判成系统问题。

基线确认后，skill 会先执行非 CPU 瓶颈预筛。只有网络、磁盘、内存带宽等因素被排除，且业务核或目标 cpuset 已经被压满，才进入 CPU 深挖链路。这个顺序能避免一个常见误区：服务端 CPU 没压满时就开始改 BIOS、绑核或编译参数，最后才发现真正瓶颈在客户端压测能力或网络路径。

## Agent + skill 相比人工调优的效率提升

传统客户支撑里的性能优化往往是串行的：一个工程师先整理环境和压测信息，再判断是否 CPU 瓶颈，然后分别找绑核、OS、网络、数据库、编译器或性能库方向的专家一起看。这个过程对专家经验依赖很强，交接成本也高。服务器应用优化智能体技能把这套流程拆成可执行的门控、脚本、子 skill 和报告契约，让 Agent 先完成可标准化、可复用、可审计的工作。

下面的效率提升为当前脱敏样例和典型人工支撑流程的保守预估。Agent + skill 的实测数据来自样例执行记录，人工耗时按单名性能工程师串行完成同类材料整理、分析和报告的常见投入估算。后续可以用更多客户项目数据校准这组数字。

```text
效率提升预估（人工串行 -> Agent + skill）

执行前门控             83% |#################---|
环境备份和证据整理     87% |#################---|
非 CPU 瓶颈预筛        90% |##################--|
6 路专项分析           88% |##################--|
低风险优化轮次         78% |################----|
报告汇总和自检         83% |#################---|
整体分析交付周期       86% |#################---|
```

| 环节 | 人工串行预估 | Agent + skill 口径 | 效率提升预估 | 对客户支撑的意义 |
|---|---:|---:|---:|---|
| 执行前门控 | 30 min | 5 min | 83% | 快速统一输入、检查项和报告字段，减少漏项返工 |
| 环境备份和证据整理 | 60 min | 8 min | 87% | 把拓扑、系统、容器、应用状态按目录落盘，便于复盘和交接 |
| 非 CPU 瓶颈预筛 | 60 min | 6 min | 90% | 先排除网络、磁盘、内存带宽和客户端压测问题，避免错误方向投入 |
| 6 路专项分析 | 180 min | 21 min | 88% | CPU 亲和性、BIOS/OS、网络、应用配置、编译器、性能库并行分析 |
| 低风险优化轮次 | 60 min | 13 min | 78% | 优先验证无需重启、无需 rebuild 的动作，缩短客户等待时间 |
| 报告汇总和自检 | 120 min | 20 min | 83% | 自动沉淀收益、证据、风险、回退和残留工作 |
| 整体分析交付周期 | 510 min | 73 min | 86% | 从“半天到一天串行分析”压缩到“约 1 小时形成可讨论结论” |

这个提升不只是“写报告更快”。对客户支撑来说，它减少的是现场等待、跨专家沟通和重复采集。对开发团队来说，它把一次临时分析变成可复用的 skill、脚本和子任务契约，下一次遇到类似 workload 时可以直接复用流程，而不是重新组织排查思路。

## Subskills as recipes

可以把每个子 skill 理解成一条专项分析 recipe。主 Agent 在生成候选优化 skill 列表后生成任务包，多个子 skill 围绕同一份证据快照进行分析，并输出结构化候选动作。主 Agent 再把这些结果合并成 `candidate_pool.json`，进入串行迭代验证。

这种方式有两个直接收益：

- 分析效率更高：脱敏样例中，6 个专项 subagent 在 21 分钟内完成分析，产出 16 个候选动作。
- 实施风险更低：候选动作先进入池子，再按证据和风险排序，只有低风险、可复测的动作进入当前轮验证。

对于生产性能优化，这种流程比一次性堆参数更有价值。它能告诉你该改什么，也能告诉你哪些动作现在不该改。对于支撑团队，这意味着一个工程师可以用统一 skill 先完成多方向初筛，再把少数真正需要专家判断的问题交出去；对于开发团队，这意味着调优知识不再只存在于个人经验里，而是沉淀到可执行的工作流里。

## 脱敏案例：MySQL 8U32G 只读场景

以下数据来自一个脱敏验证场景，平台、路径和环境信息以占位符保留，便于发布前按披露策略替换。

| 项目 | 脱敏值 |
|---|---|
| workload | MySQL 8.0.25 readonly |
| server architecture | aarch64 |
| server platform | `<ARM服务器型号>` |
| target profile | 8 vCPU / 32 GiB |
| container | `<容器名>` |
| cpuset | `<目标cpuset>` |
| benchmark mode | remote sysbench |
| raw evidence path | `<证据路径>` |

基线确认结果为 `3821.42 TPS`，40 个客户端线程下 P95 延迟为 `16.02 ms`。Step 6 的瓶颈判断显示，目标 cpuset 繁忙度达到 `99.69%`，iowait 为 `0.00%`，网络利用率平均约 `10.84%`。这意味着网络、磁盘和内存带宽不是主瓶颈，CPU 深挖可以继续。

### 各手段的量化效果

| 阶段 / 手段 | 动作 | 量化结果 | 发布口径 |
|---|---|---|---|
| 非 CPU 瓶颈预筛 | 排除网络、磁盘、内存带宽主瓶颈 | active cpuset busy `99.69%`，iowait `0.00%`，network avg util `10.84%` | 分析范围收敛到 CPU 侧 |
| 候选 skill 分析 | 多个 subagent 按候选列表分析 | `21 min` 产出 `16` 个候选动作 | 提升分析组织效率 |
| 应用配置 / 压测参数 | client threads `40 -> 32` | TPS `3821.42 -> 3862.32`，`+1.07%` | 低风险吞吐收益 |
| 延迟优化 | client threads `40 -> 32` | avg latency `10.46 ms -> 8.28 ms`，`-20.84%`；P95 `16.02 ms -> 12.52 ms`，`-21.85%`；max `77.68 ms -> 25.50 ms`，`-67.17%` | 可对外表达为运行点优化收益 |
| perf 热点定位 | 采集 perf evidence | `my_strnxfrm_any_uca` `13.03%`，`my_hash_sort_any_uca` `3.86%`，malloc `0.57%` | 证明热点在 collation / sort-key 路径，而不是 allocator |
| 风险动作裁剪 | 暂缓 allocator、IRQ/RPS、HugePages、rebuild 等动作 | Round 1 / Round 2 implementation duration 均为 `00:00:00` | 减少无证据变更和重启风险 |
| query-mix A/B | 关闭 order/distinct range 查询做隔离验证 | TPS `3862.32 -> 7443.40`，`+92.72%`；P95 `12.52 ms -> 5.12 ms`，`-59.11%` | 诊断发现，不包装成服务器配置收益 |

第一轮选择的是低风险线程 sweep。结果显示，32 线程比 40 线程更适合这个目标规格：TPS 小幅提升，延迟改善明显。这个结论本身就有交付价值，因为它不需要重启服务、不需要改内核、不需要换库，也没有引入新的系统风险。

第二轮 perf 证据进一步显示，CPU 时间主要集中在 MySQL collation 和 sort-key 相关路径。此时框架没有继续推进 allocator、IRQ/RPS、HugePages 或高风险 rebuild，而是建议围绕查询语义做 A/B 验证。后续关闭 sysbench read-only 中的 `ORDER BY` 和 `DISTINCT` range 查询后，32 线程下 TPS 达到 `7443.40`，P95 延迟为 `5.12 ms`。

这个结果很吸引眼球，但它必须被正确解释。`+92.72%` 不是服务器参数优化收益，而是 query-mix 变化带来的诊断发现。它说明这个场景真正昂贵的路径是 `ORDER BY` / `DISTINCT` 字符排序，而不是通用 OS、网络、内存分配器或磁盘问题。对于后续优化，这比盲目改一串参数更有用：团队可以转向真实 SQL、字符集 / collation、索引设计或生成排序键方案，而不是继续消耗时间在错误方向上。

## 为什么这件事值得做？

服务器应用优化的难点不在于缺少工具，而在于工具、证据、经验和验证之间经常断开。服务器应用优化智能体技能把这些环节组织成 Agent 可以执行的工作流：先建立基线，再排除非 CPU 瓶颈；先生成候选池，再串行验证；先保留证据，再输出报告。

它带来的价值可以拆成三层：

- 对客户支撑：把一次性能分析从“多人串行排查”变成“Agent 先行编排 + 专家确认关键判断”，缩短客户等待时间。
- 对开发效率：把调优经验固化为主 skill、子 skill、脚本和报告契约，新场景可以复用已有流程。
- 对交付质量：报告里同时包含收益、证据、风险、回退建议和残留工作，方便复盘和对外说明。

## 风险提示和自检手段

服务器应用优化智能体技能的定位是“证据化编排 + 工程师确认”，不是让 Agent 在生产环境里自动改参数。性能调优天然带有风险：一次错误的瓶颈判断可能让团队花几天时间走错方向，一个没有回退方案的系统参数修改可能影响稳定性，一个没有区分口径的收益数字也可能误导客户决策。因此，skill 在推广和落地时必须同时说明能力边界、风险提示和自检方法。

### 调优动作风险

| 风险类型 | 可能后果 | 风险提示 |
|---|---|---|
| 瓶颈误判 | 把网络、磁盘、内存带宽或客户端压测瓶颈误判成 CPU 瓶颈 | 未完成 Step 6 非 CPU 瓶颈预筛前，不进入 CPU 深挖 |
| 基线不可信 | 优化收益无法复现，甚至比较对象错误 | 基线必须在目标规格约束下建立，例如 `8U32G` 必须先限制到 8 核 / 32 GiB |
| Agent 过度建议 | 生成看似合理但缺少证据的调优动作 | 候选动作必须进入 `candidate_pool.json`，并标注证据、风险和验证方式 |
| 生产变更风险 | BIOS/OS、IRQ、HugePages、编译重构等动作可能引起服务抖动或不可用 | 高风险动作默认暂缓，必须由工程师确认窗口期、回退方案和影响范围 |
| 远程执行风险 | SSH、容器命令、数据库命令可能误操作目标环境 | 默认只读采集和 dry-run，执行变更前需要明确目标主机、容器、进程和命令 |
| 数据披露风险 | 报告中泄露 IP、用户名、路径、容器名、客户业务信息 | 对外材料统一使用 `<占位符>`，保留技术结论和量化结果即可 |
| 收益口径风险 | 把 query-mix、压测参数或诊断发现包装成服务器配置收益 | 报告必须区分配置收益、运行点收益、诊断发现和 workload 变化 |

### Skill 本身的使用风险

| 风险类型 | 可能后果 | 用户需要注意 |
|---|---|---|
| 输入材料不足 | Agent 基于不完整信息生成错误计划或过宽假设 | 提供应用版本、部署方式、压测命令、目标规格、变更边界和已有观测数据；缺失项要保留为 `<待补充>` |
| 过度信任 Agent 判断 | 将候选动作直接当成最终调优结论 | Agent 输出是分析建议，生产变更必须由工程师 review 后执行 |
| 权限边界不清 | Agent 可能尝试执行 SSH、容器、数据库或系统命令 | 运行前明确只读采集、dry-run、允许执行的主机和命令范围 |
| Skill 版本漂移 | 不同 Agent 环境加载到旧版 skill 或轻量入口未同步 | 使用前确认主源 `skills/kpbot-app-tuner/` 与 `.claude/`、`.opencode/`、`.agents/` 入口一致 |
| 子 skill 输出不完整 | 候选池缺少某个方向的分析，导致决策偏向已有证据 | 检查 `candidate_skill_list` 中所有候选和 coverage subagent 是否返回 `ok` 或明确 `degraded`/`blocked` 原因 |
| 脚本依赖缺失 | `perf`、`sysstat`、`iostat`、数据库客户端等工具缺失，导致采集降级 | 报告必须记录依赖状态、缺失工具和降级影响，不把降级结果当完整结论 |
| 上下文压缩或长会话丢信息 | Agent 在长流程中遗漏早期约束、路径或用户确认 | 关键参数写入 checklist、checkpoint、report input，不只保存在对话里 |
| 多轮优化混淆 | 不同轮次配置、压测参数或 workload 变化混在一起比较 | 每轮记录当前有效配置、变更项、原始日志路径和可比性说明 |
| 对外传播误读 | 宣传材料把预估效率或单场景收益理解成所有场景保证值 | 明确标注“样例实测”“保守预估”“待更多项目校准”的边界 |

### 用户使用注意事项

| 阶段 | 注意事项 |
|---|---|
| 使用前 | 先确认目标环境是否允许 Agent 访问，尤其是生产系统、客户内网、数据库账号和 SSH 权限 |
| 使用前 | 明确 `change_scope`：只读分析、允许应用参数变更、允许 OS 参数变更、允许重启、允许重新编译要分开写 |
| 使用前 | 准备最小输入材料：应用名、版本、部署方式、目标规格、压测命令、指标口径、历史基线或问题现象 |
| 使用中 | 每次进入优化动作前检查候选动作的证据、风险、验证方法和回退方案 |
| 使用中 | 不要同时引入多个高风险变量；优先执行无需重启、无需 rebuild、能快速回退的动作 |
| 使用中 | 远程压测场景必须同时观察客户端和服务端，服务端 CPU 不满时先排查客户端压测瓶颈 |
| 使用后 | 对外报告必须脱敏，并区分配置收益、运行点收益、诊断发现和 workload 变化 |
| 使用后 | 将最终报告、候选池、原始日志、回退说明和残留工作一起归档，方便后续客户支撑复用 |

### Skill vetter 扫描结果

以下结果按 `skill-vetter` 安全审查协议整理，扫描对象为仓库内主源目录 `skills/kpbot-app-tuner/`。扫描时间为 `2026-05-21`，用于发布前安全说明；正式发布前建议再次扫描一次，以最终仓库状态为准。

```text
SKILL VETTING REPORT
═══════════════════════════════════════
Skill: kpbot-app-tuner
Source: repo-local skill directory
Author: <作者/团队待补充>
Version: <版本号待补充>
Last repository update for skill path: c5b1258 2026-05-15
───────────────────────────────────────
METRICS:
• Downloads/Stars: N/A（本地仓库扫描，未查询公开指标）
• Files Reviewed: 50 total
• Text files reviewed: 47
• Binary/cache files found: 0
───────────────────────────────────────
RED FLAGS / FINDINGS:
• No hardcoded production credentials found.
• No base64 decode, browser cookie access, or hidden credential-file harvesting found.
• Redfish examples use BMC_HOST / BMC_USER / BMC_PASS and curl to BMC endpoints. This is expected for BIOS collection, but must be opt-in.
• optimize_network.sh defaults to dry-run; real execution requires --execute and --approved-change-id, and can change firewalld, iptables, RPS, sysctl and ethtool settings.
• apply_optimization_action.sh defaults to dry-run; real execution requires --execute and --approved-change-id, and can change sysctl, THP, CPU governor and network settings.
• install_external_library_replacement.sh is offline-only by default; external clone requires explicit --allow-clone after source review.
───────────────────────────────────────
PERMISSIONS NEEDED:
• Files: reads evidence, system proc/sysfs views, application status; writes output reports, evidence snapshots, rollback notes.
• Network: optional Redfish/BMC collection; optional reviewed external git clone when --allow-clone is explicitly provided; optional SSH patterns in workflow documentation.
• Commands: perf, pidstat, mpstat, sar, ethtool, mysql, readelf, ldd, sysctl, taskset, docker, optional iptables/systemctl/ethtool root operations.
───────────────────────────────────────
RISK LEVEL:
• Analysis-only use: MEDIUM
• System/network/BIOS execution use: HIGH
• Direct production execution without human approval: NOT RECOMMENDED

VERDICT:
• INSTALL / USE WITH CAUTION
• Safe for controlled analysis workflow when run in read-only or dry-run mode.
• Production changes require engineer review, approved change window, backup and rollback plan.
═══════════════════════════════════════
```

扫描结论不是“无风险认证”。它说明当前 skill 适合作为受控分析和编排框架使用，但不应默认授予自动变更生产环境的权限。发布或交付前应再次运行 `scripts/validate_skill_quality.py`，并确认 `analysis_only`、`dry-run`、`--execute`、`--approved-change-id` 和 `--allow-clone` 的边界已被使用方理解。

### 落地前自检清单

| 自检项 | 检查方法 | 通过标准 |
|---|---|---|
| 输入完整性 | 检查优化目标、应用版本、目标规格、压测方式、变更范围 | 缺失项以 `<待补充>` 标注，不用猜测补齐 |
| 环境备份 | 检查 CPU、NUMA、内存、磁盘、网卡、OS、容器、应用配置是否落盘 | 关键环境信息可追溯，路径写入报告 |
| 基线可信度 | 检查压测命令、线程数、运行时间、目标资源限制和原始日志 | 基线能复跑，且与目标规格一致 |
| 非 CPU 瓶颈预筛 | 检查 CPU busy、iowait、网络利用率、客户端资源和压测工具限制 | 有证据支持“允许进入 CPU 深挖”或“应先优化其他子系统” |
| 候选动作质量 | 检查每个动作是否有证据、预期收益、风险、验证方法和回退建议 | 无证据动作不得直接进入优化轮次 |
| 串行验证 | 检查每轮只引入少量可解释变化，并记录前后指标 | 能说明本轮变化和收益之间的关系 |
| 报告自检 | 检查收益表、耗时表、风险表、回退建议和残留工作 | 报告不是只给结论，也给证据和边界 |
| 脱敏检查 | 检查 IP、账号、路径、容器名、客户名、内部仓库地址 | 对外发布版本只保留必要技术信息和占位符 |

### 自检命令和产物

实际使用时，可以把自检落实到几个固定产物上。`backup_environment.sh` 负责建立环境证据目录，`collect_evidence_snapshot.sh` 负责在专项分析前统一采集证据，`merge_subagent_results.py` 负责校验并合并 subagent 输出，`summarize_improvement.py` 负责汇总收益，`generate_report.py` 负责生成最终报告。最终交付时，至少应能回答四个问题：

- 这次优化的基线是否可信？
- 为什么判断当前可以进入 CPU 深挖？
- 每个已执行动作分别带来了多少收益，哪些动作被拒绝或暂缓？
- 如果优化效果不符合预期，如何回退到上一轮状态？

## Getting started

当前仓库提供主 skill、8 个子 skill、参考文档、报告模板，以及环境备份、证据快照、MySQL 状态采集、候选动作合并、收益汇总和报告生成等辅助脚本。

快速体验可以从主 skill 开始：

```bash
git clone <公开仓库地址>
cd <repo-dir>
skills/kpbot-app-tuner/scripts/backup_environment.sh ./output/env
skills/kpbot-app-tuner/scripts/init_report.sh ./output/report demo-scenario
```

在支持 `SKILL.md` 的 Agent 环境中，可以直接描述优化目标：

```text
使用服务器应用优化智能体技能帮我优化 <应用名称> 的 <目标规格> 场景。
压测方式是 <本地/远端压测>，请先建立基线；不确定的配置先列出确认项。
```

也可以使用更接近案例的输入：

```text
使用 kpbot-app-tuner skill 帮我优化 MySQL 8U32G 只读场景。
远端压测，先建立基线，部署和压测方法参考 <测试说明文档路径>。
```

从这里开始，性能优化不再只是“看一眼指标然后调几个参数”，而是一条能复用、能验证、能交付的工程路径。
