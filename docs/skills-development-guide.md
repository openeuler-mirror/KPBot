# KPBot Skills 开发指南

## 1. 内容规范

按调用方要拿到的主要交付物选择类型，逐项核对下面的清单。每个 skill 都应说明适用场景、必需输入、预期产物，以及受阻时能交回什么。兼有多种职责时，补查对应类别。

## 1.1 工具类

以 Python 火焰图 skill 为例。以下命令在 skill 目录执行。输出示例用随包的模拟栈说明如何从一次采样生成图表和 JSON，两份报告是建议的交付格式。[py-spy 使用说明](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/SKILL.md) · [模拟栈样本](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/scripts/fixtures/render-stacks.raw)

**依赖**　列出运行环境、所需软件及支持版本。\
例：支持 Linux x86_64 和 aarch64，采集器使用 py-spy 0.4.x，采样目标是正在运行的 Python 进程。使用随包安装脚本时还需有 `pip3`。

**安装**　给出安装、自检命令和成功判据。\
例：运行 `bash scripts/deploy.sh` 安装，再用 `bash scripts/discover.sh` 自检。看到 `py-spy: READY` 后核对版本和路径；工具缺失时自检也会返回退出码 0，此时状态行是 `NOT_INSTALLED`。

**权限**　说明操作指定目标所需的最小权限，以及受阻时调用方会看到什么。\
例：采集当前用户的测试进程，需要系统允许对该 PID 执行 `ptrace`。若内核拒绝，采集脚本以非零状态退出，并在标准错误中提示“无 ptrace 权限”和目标 PID。

**用法**　给出可复现的命令，标明必填参数和被覆盖的默认值。\
例：将 `1234` 换成获准采样的 Python PID，运行 `bash scripts/collect.sh -p 1234 -d 10 --format raw -o stacks.raw`。`-p` 必填；默认采样 30 秒、输出 SVG，此处改为采样 10 秒并保留原始栈，供两种报告复用。

**降级与备选**　写明主工具何时不可用、可用哪种替代工具或已有产物继续、降级结果能回答哪些问题，以及何时应停止。\
例：py-spy 未安装或当前账号无权采样时，若已有同一进程实例、同一分析窗口的 `stacks.raw`，可离线生成火焰图和摘要，在两份报告中标明采样时间及文件来源。若任务要求了解当前热点，而手头只有旧栈，则返回 `blocked`，说明缺少当前窗口的样本，并请调用方提供匹配的栈文件或安排获授权的采样。

**人读输出**　给出图表的位置，并示范读者能从图中得出什么结论。\
例：将模拟栈渲染为 `flame.svg`，图注写“1,000 个栈样本中，有 710 个落在调用路径 `api → render_template → compile_template` 上”。该路径占图宽的 71%，表示样本占比。

**AI 可读输出**　提供结构化样例，写清字段、单位和失败语义。\
例：建议随图交付 `summary.json`，如 `{"status":"ok","stack":["api","render_template","compile_template"],"samples":710,"total_samples":1000}`。两个计数字段的单位都是栈样本。权限不足时可返回 `{"status":"blocked","reason":"ptrace_denied"}`，省略采样计数。

**输出一致性**　用同一进程、同一窗口的原始样本核对图表和结构化结果。\
例：两份报告都读取同一份 `stacks.raw`。用模拟栈验收时，核对 JSON 中该路径的 `710 / 1000` 等于图中占比 71%；若 JSON 把父帧 `render_template` 的 900 个样本算到这条子路径上，该用例应失败。

## 1.2 部署类

以 CubeSandbox 单机全新部署为例。以下命令都在目标 Linux 主机的 skill 目录执行。[部署入口](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/agentic-infra/workflows/cubesandbox-deployment/SKILL.md) · [配置样例](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/agentic-infra/workflows/cubesandbox-deployment/deployment.env.example) · [验收与故障处理](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/agentic-infra/workflows/cubesandbox-deployment/references/deployment.md)

**环境**　列出支持的平台、版本，以及部署前需要通过的检查。\
例：在 aarch64 Linux 主机上，先运行 `bash scripts/deploy.sh --preflight-only`。核对 glibc 至少为 2.31、原生 KVM 可用，且 `/data/cubelet` 位于至少有 50 GiB 可用空间的 XFS 文件系统上。

**配置**　说明必填配置项、默认值及生效位置。\
例：从 `deployment.env.example` 复制一份到 skill 目录外。单网卡主机默认使用默认路由接口的 IPv4 地址；如果预检发现多个候选地址，在副本中将 `CUBE_SANDBOX_NODE_IP` 设为已确认的节点地址，再通过 `--env` 让本次安装读取它。

**权限**　写明安装和运行分别需要的权限。\
例：正式安装需要 root，因为安装器会配置 systemd 和主机网络。部署配置文件归 root 所有，权限设为 `600`。部署完成后，运维人员通过 systemd 管理服务；各服务的运行身份以对应单元配置为准。

**部署**　给出可复现的部署命令，标明目标实例或目录。\
例：在选定主机上运行 `bash scripts/deploy.sh --env /secure/path/deployment.env`，将示例路径替换为实际配置文件。安装根目录固定为 `/usr/local/services/cubetoolbox`。需要复现同一版本时，在配置中用已验证的发布标签固定 `CUBE_VERSION`。

**验收**　给出健康检查命令和预期响应，让调用方能确认服务可用。\
例：部署命令会调用官方安装器及其 quickcheck，检查服务、健康端点和节点注册。两者成功后，再用 `test -f /usr/local/services/cubetoolbox/.one-click.env` 核对安装标记，预期退出码为 0。三项均通过，才报告部署成功。

**重复执行**　说明目标已存在或上次中断时，再次部署会如何处理。\
例：安装目录中已有 `.one-click.env` 时，再次正式部署会提示已有安装并停止。若上次中断，只留下 `CubeMaster` 等组件目录，脚本也会停止并报告部分安装，交由运维人员判断如何恢复。

**恢复**　给出失败后的回滚或卸载步骤，说明如何确认已恢复。\
例：若安装中断，保留 `/var/log/cube-sandbox-one-click/` 下的日志，记录最后成功的阶段。该 skill 仅支持全新安装；运维人员备份现状并取得授权后，按对应版本的官方流程修复。修复后重新核对服务健康、节点注册和安装标记，再交回结果。

## 1.3 知识类

以 ARM 指令查询 skill 为例。下面用同一个 NEON 内联函数 `vaddq_f32` 说明怎样界定问题、引用证据和验证答案。[ARM 指令查询入口](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/core/skills/arm-instructions-query/SKILL.md)

**范围**　明确能回答的问题，以及结论适用的版本、平台或时间范围。\
例：对“在 AArch64 的 C 程序里，`vaddq_f32` 做什么运算？”可以回答：它将两个各含 4 个单精度浮点数的 NEON 向量按对应位置相加，返回同类型向量。这里说明的是 AArch64 下的指令语义。

**出处**　每项关键结论附可定位的文档章节、条目或数据来源。\
例：写出“在 AArch64 上映射为 `FADD Vd.4S,Vn.4S,Vm.4S`”时，注明依据是随包 ACLE 数据库中 `neon / vaddq_f32` 条目的指令映射，并附上该条目指向的 [Arm《FADD (vector)》指令页](https://developer.arm.com/documentation/ddi0602/2025-06/SIMD-FP-Instructions/FADD--vector---Floating-point-add--vector--)。

**命中验证**　用一个典型问题核对答案，并确认引用能支持该结论。\
例：提问“使用 `vaddq_f32` 要包含哪个头文件？返回什么类型？”在 skill 目录运行 `python scripts/acle_query.py info vaddq_f32 --family neon`。回答中的头文件和返回类型应分别与查询结果的 `Header`（`<arm_neon.h>`）和 `Returns`（`float32x4_t`）一致。

**越界验证**　用一个超出范围的问题验证返回内容是否指出缺少的依据。\
例：提问“把我的循环改成 `vaddq_f32` 能快 4 倍吗？”预期回答应指出：4 表示每个向量的元素数。要判断这个循环的实际加速比，还需要目标 CPU、编译选项，以及相同输入下标量版与向量版的耗时。

## 1.4 经验类

以 Lance 在鲲鹏 950 上用 NEON 加速整数解包为例。这里选取 W1：每个整数在压缩输入中占 1 位，每次调用解出 1,024 个 `u32` 整数。[仓库案例](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/methods/skills/kernel-paired-benchmark/references/bp-case.md)

**现场特征**　描述最初的问题、环境和可观察现象，方便调用方判断是否命中。\
例：开发者在鲲鹏 950 上分别运行原版和 NEON 版解包程序。有些位宽在多轮测试中都显示加速，但加速幅度波动较大，单轮数字很难代表稳定收益。

**判断依据**　列出支持当时判断的日志、指标或对照证据。\
例：先让原版与自身比、NEON 版与自身比，检查相同实现的耗时比是否接近 1。本案例要求自对照中位数落在 0.97～1.03，并检查两端分位数；自对照通过后，再解读两个实现之间的差距。

**处置动作**　说明采取了什么动作，以及选择该动作的理由。\
例：让同一个 Rust 程序保留原版和 NEON 解包入口，两者交替处理同一块压缩数据、复用同一输出缓冲区。把分配和预热移出计时区间，使耗时差对应解包路径。

**实测效果**　给出动作前后的结果，并注明测量条件。\
例：固定 CPU 核和 NUMA 节点后，W1 在三个独立进程中各测了 21 组。以“原版耗时 ÷ NEON 版耗时”计算，各轮加速比中位数为 **1.953、2.024、1.554**。三轮都显示加速，同时保留了轮间波动。[W1 历史测量摘录](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/methods/skills/kernel-paired-benchmark/assets/bp-w1-evidence.json)

**复验方法**　说明在另一现场怎样验证这条经验是否成立。\
例：在目标机器上准备同一份 W1 输入，逐项核对两条路径解出的整数，确认候选实际进入 NEON 实现。固定绑核和计时范围，先跑两组自对照，再跨进程重测原版与候选，逐轮保存配对结果。[配对测量协议](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/methods/skills/kernel-paired-benchmark/references/protocol.md)

**适用边界**　给出一个看似相似却容易误用的情形，以及区分它的线索。\
例：同一解码器处理 32 位满宽数据（W32）时，候选入口会回退到原版。若线上数据主要是 W32，估算收益前要先统计实际位宽分布，再按业务负载测量端到端耗时。

## 1.5 工作流类

以 vLLM-Ascend 首 token 时延（TTFT）回归排查为例。工作流先判读归档，再决定是否需要 Python 栈，最后把诊断交给发起方。[工作流入口](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/ascend-kunpeng-ai/skills/vllm-ascend-ttft-regression/SKILL.md) · [归档输入契约](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/ascend-kunpeng-ai/skills/vllm-ascend-ttft-regression/archive-input.md)

**起点**　写明启动流程所需的输入和前置证据。\
例：准备同一模型、数据集、并发和预热条件下的基线与当前两份 profiling 归档。比较清单给出回归阈值，并把两侧客户端逐请求 TTFT 对应到采集窗口和各 rank 的 trace 事件。回归指标取客户端从发出请求到收到首 token 的 p95。

**节点输出**　逐个列出关键节点消费的输入、产出的文件或状态，以及下游读取的位置。\
例：这条工作流有三个交接点，每个节点都能指出下一步读取的产物。\
① **判读**读取比较清单，将分支 `decision` 和理由 `reason` 写入输出目录的 `decision.json`，连同已算出的 `client_p95_ms`、已对齐的请求和证据引用交给分派节点。\
② **按需补栈**读取 `decision.json`。现场补采成功后，工具在新采样目录写入折叠栈 `stacks.raw` 和回执 `receipt.json`；回执关联本次决定、目标实例和采样窗口。走复用或跳过分支时，直接沿用已有证据。\
③ **交回父任务**读取决定、App Tuner 派发的任务和可选回执，校验后把分析结果写到父任务指定的路径。父流程从该文件读取 `status`、`findings` 和 `required_evidence`。

**分支条件**　写明根据哪个输出字段选择下一步，并给出每个取值的判据。\
例：分派节点读取 `decision.json` 的 `decision` 字段，按以下五种结果处理。\
`no_regression`：客户端 p95 增幅小于或等于清单阈值，交回测量值，结束归因。\
`device_or_wait`：各配对请求的关键路径显示设备计算或通信区间增长足以解释回归，保留对应 rank 的证据，跳过 Python 采样。\
`sufficient_stack`：增长来自主机侧 Python 执行，已校验的同实例、同窗口栈能定位增长函数的下一级调用，直接复用该栈。\
`collect_python`：API 服务进程的线程 CPU 时间增长能解释回归，但缺少子调用栈。模拟输入走回放；现场补采还须具备同实例、同负载的复测窗口、本轮授权和采样预算。\
`need_evidence`：例如缺少一个预期 rank 的 trace，无法对齐关键路径。按 `reason` 返回具体缺项，等待补证。

**异常处理**　说明缺输入、缺权限或节点失败时，何时补证、跳过或回退。\
例：判读已得到 `collect_python`，但现场缺少本轮采样授权。保留 `decision.json`，交回时省略 `--receipt`。归档模式下父任务收到 `status=blocked`、`findings.sampling=unavailable` 和待补栈说明，仍可查看已有归因证据。

**终止条件**　给出正常完成和提前停止的判据，并写明各自应保留的产物。\
例：p95 增幅恰好等于阈值时，以 `no_regression` 正常结束，保留两侧 p95；有父任务时交回空的候选动作列表。若缺关键 rank 而提前停止，则保留 `need_evidence` 决定、原因和已有 trace 引用，让下一轮能补齐对应归档。

**交回**　指定结果接收方，列出最终决定、证据引用和仍待补齐的项目。\
例：用户直接调用时，返回决定文件路径，以及可用的栈或缺项。App Tuner 委托时，由父流程读取指定结果文件中的 `findings`（决定和采样状态）、`evidence_sources`（证据路径）、`required_evidence`（缺项）和 `candidate_actions`（候选动作）。候选优化的收益由父流程在关闭采样后用同负载 A/B 验证。

## 1.6 模板类

以 vLLM-Ascend 性能分析的 HTML 报告模板为例。以下示例使用单次采集模式，`SKILL_DIR` 指向 `vllm-ascend-profiling` 目录，命令在存放样例数据的目录运行，Python 环境需安装 Jinja2。[报告模板](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/ascend-kunpeng-ai/skills/vllm-ascend-profiling/templates/report.html.j2) · [生成脚本](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/ascend-kunpeng-ai/skills/vllm-ascend-profiling/scripts/generate_report.py)

**参数**　列出必填项、可选项和默认值，说明缺少必填项时如何提示。\
例：必须提供一份 trace 事件数组。脚本默认读取当前目录的 `trace_view.json`，也可用 `--trace-view` 指定路径。标题 `--title` 可选，输出路径 `--out` 默认为 `report.html`。输入文件缺失时，脚本以退出码 2 结束，并在标准错误的 JSON 字段 `missing` 中列出缺失路径。

**生成样例**　提供一组最小输入及对应的完整产物，标明生成位置。\
例：将下面这条模拟事件保存为 `trace_view.json`，运行 `python "$SKILL_DIR/scripts/generate_report.py" --title "Trace demo"`。完整产物是当前目录的 `report.html`；用浏览器打开，页签标题应为 `Trace demo`，页首应显示“事件 1”。

```json
[{"name":"demo","ph":"X","pid":1,"tid":1,"ts":100,"dur":10}]
```

**覆盖行为**　说明目标已存在时如何处理，用重复生成验证人工修改是否按约定保留。\
例：在样例的 `report.html` 末尾加上 `<!-- manual-note -->`，再用相同输入运行同一命令。脚本会整份重写 HTML，搜索该批注应无结果。要保留批注版，可在重跑时指定 `--out report-v2.html`，让两份报告分别保存。

## 1.7 方法与验证类

以端到端增量验证 skill 中“单独启用优化 A”的报告测试为例。仓库用三组配对的合成耗时检查两件事：条件一致时怎么算收益，条件变化时应撤回哪项结论。[验收案例](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/methods/skills/incremental-e2e-validation/references/acceptance.md) · [可运行的报告测试](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/methods/skills/incremental-e2e-validation/tests/test_report.py)

**基线**　说明比较对象、工作负载和需要保持一致的环境条件。\
例：基线 `c0` 关闭全部优化，候选 `c1` 只开启 A。两者执行同一端到端任务，并引用同一份条件记录 `conditions-1`。按“基线、候选、候选、基线”（ABBA）的顺序配对，每组保留两侧的构建标识、运行日志和正确性记录。

**指标**　定义指标、单位、优劣方向和测量方法。\
例：指标为任务完整耗时，单位是毫秒，越低越好。每组基线为 100 ms、候选为 90 ms，耗时降幅按 `(100 − 90) / 100 = 10%` 计算。报告同时保留原始耗时和组数，方便复算。

**通过条件**　事先写明正确性要求和验收阈值，给出一组满足条件的样例。\
例：本用例要求两侧正确性通过、候选已实际命中、环境稳定，至少有 3 组有效对照。三组降幅均为 10%，超过本例自对照给出的 1% 波动参考线，报告应显示“观察到改善”。若项目还要求最低业务收益，应在采样前另行约定。

**不可比条件**　给出条件漂移或证据不足的反例，说明如何报告原因和待补证据。\
例：保留 100 ms 和 90 ms，只把第一组候选的 `condition_id` 改为 `changed`。报告应指出第 0 组条件不一致，标为“条件不可比”，收益留空。回查该组运行日志，恢复基线条件后补测，再重新汇总。

## 2. 命名规范：入口带 `kpbot-`，内部能力按职责命名

面向用户的主入口采用 `kpbot-<用途>`，如 `kpbot-app-tuner`。供工作流或 Supervisor 复用的内部 skill 直接写用途，如 `ttft-trace-triage`。名称简短、准确，目录名、`SKILL.md` 的 `name` 和清单的 `id` 保持一致。名称限 1–64 个字符，使用小写字母、数字和单连字符，连字符只放在词之间。[Agent Skills 命名规范](https://agentskills.io/specification#name-field)

`description` 只回答 **when（何时用）** 和 **what（交付什么）**。命令、算法和操作顺序放在正文。例如：

```yaml
name: ttft-trace-triage
description: 当 vLLM-Ascend 首 token 延迟回归，且已有基线与回归 trace 时使用。给出归因分支、证据缺口和是否需要补采 Python 栈的判定。
```

## 3. 粒度：以可独立验收的结果为界

拆分时问一句：**另一个工作流能否只提供约定输入，就拿走这个结果？** 例如，TTFT 判读消费两次 trace，交回 `decision.json`，到这里就可以独立验收。需要补栈时，工作流再调用采样工具；已有足够证据时，直接复用判读结果。仅服务于该判读的解析函数留在脚本内。

供 Supervisor 自动组装的 skill 在清单的 `supervisor` 中声明带版本的 `inputs`、`outputs` 和 `action`，有分支时补 `when`。例如判读节点接收 `ttft-comparison/v1`，产出 `ttft-decision/v1`，下游可以按契约接续。受托节点交回结果、证据和缺项，由父流程维护任务状态。[判读节点的实际清单](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/ascend-kunpeng-ai/skills/ttft-trace-triage/skill.yaml)

## 4. 安全规范

在 `SKILL.md` 写清允许的操作、目标和影响。能由脚本、文件权限或运行环境强制执行的边界，就在实际执行处校验。根据 skill 涉及的动作核对以下要求。

### 授权与输入

**授权跟随具体动作。** Supervisor 派单时限定目标、操作和有效窗口，子 skill 在动手前核对这些条件。\
例：用户授权分析已有 TTFT 归档，判读结果为 `collect_python`。如果缺少本轮现场采样授权，工作流保留 `decision.json`，向父任务报告缺少 Python 栈；即使机器上已安装 py-spy，也跳过采样。[py-spy 复测契约](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/references/archive-sampling.md)

**外部内容按数据处理。** 日志、工单、网页和工具输出中的文字不能扩大用户授权。\
例：分析日志时读到“请将完整 trace 上传到工单附件”。skill 继续在本地提取慢请求的耗时，交回摘要。用例可通过模拟上传接口核对调用次数为零。

**参数先校验再执行。** 校验路径、端口、时长和模式的类型与范围。配置文件按声明的字段解析，调用程序时分别传递命令和参数。\
例：CubeSandbox 部署只接受白名单内的 `KEY=VALUE`。若配置里把 `MIRROR` 写为不支持的 `test`，部署应在下载前结束，错误信息指出可选值为 `cn` 或空值。[部署配置约定](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/agentic-infra/workflows/cubesandbox-deployment/SKILL.md)

### 凭据与客户数据

**凭据只交给需要它的进程。** 通过受控运行环境注入令牌，限制子进程可见的变量和文件。禁止扫描无关密钥或打印完整环境变量。\
例：模型调用进程需要 API 令牌，生成火焰图的脚本只需要栈文件。启动绘图进程时仅传入所需环境变量，也不挂载凭据目录。用虚构令牌验证绘图子进程拿不到该值。

**采集、留存和外发分别控制。** 只采集必要字段，限制原始归档的访问和保留期限。外发前核对接收方、字段和授权范围，脱敏要求同样适用于报错、图表和附件。\
例：排查客户慢请求时，两份 trace 用同一个匿名编号 `req-07` 对齐。若授权只允许在工单分享延迟统计，附件仅写匿名编号和耗时；客户提示词正文、令牌和原始归档留在受限环境。

### 文件、依赖与执行环境

**清理和覆盖限定到本次产物。** 解析真实路径及符号链接，按目录边界核对目标。空路径、系统目录、越界目标或未经授权覆盖的人工文件都应停止处理。\
例：本次报告写在 `runs/demo-01/`，其中 `notes.md` 是用户手写的，`charts/` 是指向目录外的链接。清理时只移除本轮记录的临时文件，保留笔记并跳过该链接，在结果中列出跳过原因。

**下载和运行分开校验。** 记录依赖的来源和版本，使用可信签名或摘要核对下载物。Agent 生成的程序先在非特权环境验证，限制其文件和网络访问。\
例：部署包下载完成后，其 SHA-256 与本轮固定的发布摘要不符。部署脚本应在解包前停止，记录版本、预期摘要和实际摘要；用例应确认安装器调用次数为零。[CubeSandbox 部署脚本](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/domains/agentic-infra/workflows/cubesandbox-deployment/scripts/deploy.sh)

**限制资源并准备恢复。** 对采样、压测和重试设上限；改配置前记录原值、恢复动作和健康检查，中断后先核对实际状态。\
例：某次压测约定并发上限为 8、运行 60 秒，测试前把服务工作线程从 4 调到 8。若压测超时，结束本轮负载，核对当前配置后恢复为 4；重新读取生效值并通过健康检查，再报告恢复完成。

### 安全验收

**反例要检查实际副作用。** 使用临时目录和合成数据，同时核对返回状态、已执行动作和受保护对象。\
例：在测试临时目录内准备两个相邻目录，只授权写入第一个。让报告输出路径经符号链接指向第二个目录的哨兵文件。验收时同时断言写入被拒绝、哨兵文件的 SHA-256 前后一致，并核对错误日志中没有测试用的虚构令牌。[Agent 用例运行规范](agent-case-security-notes.md)

## 5. 开发流程：从主源到安装验证

以可独立安装的 Python 采样工具 `kptools-py-spy` 为例。前四步从 KPBot 仓库根目录开始，第五步在空配置中检查用户实际装到的包。

### 5.1 选目录

按复用范围找主源。领域内的单项能力放在 `Plugins/domains/<domain>/skills/`，领域流程通常放在同域的 `workflows/`；公共知识、方法分别归入 `Plugins/shared/core/`、`Plugins/shared/methods/`，可单装的公共工具归入 `Plugins/shared/tools/`。

例：py-spy 既能由用户直接调用，也能被 TTFT 工作流复用，因此主源是 `Plugins/shared/tools/kptools-py-spy/`。改采样逻辑时编辑这里的 `scripts/collect.sh`。改动已迁移的入口时，按迁移清单确认主源，再按构建说明同步兼容副本。[工具主源](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/SKILL.md) · [迁移清单](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/docs/kpbot-skills-inventory-current.md)

### 5.2 写清单

`SKILL.md` 写触发条件和用法。`skill.yaml` 写入口 ID、能力、版本化契约、输入、输出和随包资源；工作流用 `workflow.yaml` 并声明 `steps`。所属包的 `plugin.yaml` 负责导出入口和声明插件依赖。

例：py-spy 的 `name` 和 `id` 都是 `kptools-py-spy`，能力是 `python.stack.sample`，契约是 `python-process-sample/v1`。独立采集接收 Python PID，交付栈文件或诊断；离线回放接收模拟决定，交付回执。`resources` 列出 `SKILL.md`、`scripts`、`references`、`evals` 和 `cases`。包清单的 `exports: ["skill.yaml"]` 让构建器找到这个入口。[能力清单](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/skill.yaml) · [包清单](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/plugin.yaml)

### 5.3 编写 cases

在 `cases/<场景>/` 下，用 `case.yaml` 写清运行条件、入口和断言，用脚本准备输入并核对产物。至少覆盖一条成功路径和一条容易误判的异常路径。

例：`record-flamegraph` 是需要 py-spy 和目标采样权限的 Linux 用例。它启动自己的 Python 工作负载，检查 SVG 非空、原始栈里出现 `workload.py`、线程快照含 `Thread`。验收看退出码为 0，且 `results/result.json` 的 `business_frame_visible` 为 `true`。补缺依赖反例时，可在没有 py-spy 的环境传入有效格式的 PID，断言采集脚本返回 2 并提示缺少工具。[case 定义](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/cases/record-flamegraph/case.yaml) · [执行脚本](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/Plugins/shared/tools/kptools-py-spy/cases/record-flamegraph/entrypoint.sh)

### 5.4 导出发布包

新增独立包时，在 `catalog/packages.json` 登记发布名和主源路径；扩展已有包时更新其 `exports`。新增的运行资源先加入 Git 索引，再从仓库根目录运行：

```sh
node --test tests/test_plugin_build.mjs
npm run build:plugins
claude plugin validate dist/claude/.claude-plugin/marketplace.json
```

例：构建后核对 `dist/claude/Plugins/kptools-py-spy/skills/kptools-py-spy/`。其中应有 `SKILL.md`、回放脚本 `scripts/replay.py` 和它读取的 `scripts/fixtures/render-stacks.raw`。同时核对发布目录的 `catalog/capabilities.json` 中能查到 `python.stack.sample`。能力声明变更时，一并提交构建刷新的 Team 能力索引。[发布包注册表](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/catalog/packages.json) · [构建与兼容同步](https://gitcode.com/mxj1314/KPBot/blob/355e8c5b92279ffb13a5ce54969c4205d489295d/docs/claude-plugin-build.md)

### 5.5 安装验证

用新的 Claude 配置只装目标包，先核对宿主发现的入口，再从安装缓存运行一个最小用例。下面的命令使用 Bash，在新开的终端中执行，将市场路径换成本次构建目录的绝对路径。

```sh
export CLAUDE_CONFIG_DIR="$(mktemp -d)"
claude plugin marketplace add "/本次构建目录的绝对路径/dist/claude"
claude plugin install kptools-py-spy@kpbot-marketplace
claude plugin list --json
```

例：安装记录应只有 `kptools-py-spy@kpbot-marketplace`。将该记录的 `installPath` 赋给变量 `PLUGIN_DIR`。在临时工作目录保存以下模拟输入为 `decision.json`：

```json
{"decision":"collect_python","simulated":true,"run_id":"guide-demo","target":{"role":"api-server","pid":1234}}
```

在该工作目录执行回放，使用新的回执文件名：

```sh
python "$PLUGIN_DIR/skills/kptools-py-spy/scripts/replay.py" decision.json receipt.json
```

核对回执中的 `provider=kptools-py-spy`、`simulated=true` 和 `samples=1000`。这一步验证了已安装脚本能够找到随包数据。验收现场采样能力时，在具备权限的 Linux 目标机上，从安装目录运行 `bash cases/record-flamegraph/entrypoint.sh`，再核对第 3 步的断言。

## 6. PR 规范：用动机、原理和证据说明变更

下面是一份可直接参考的 PR 描述。提交自己的 PR 时，把验证命令、结果和待测项换成本次记录；涉及性能收益时，附同环境、同负载的基线和对照数据。

**示例标题**　支持独立安装 py-spy 采样工具，并接入 vLLM-Ascend TTFT 回归排查

### 为什么做

排查 vLLM-Ascend 首 token 时延（TTFT）回归时，trace 有时只能定位到 Python 渲染阶段，无法看清耗时集中在哪个子调用。把 Python 栈采样封装为独立的 `kptools-py-spy` 插件后，开发者可以只装这个工具排查某个进程，TTFT 工作流也能在缺栈时复用同一个入口。已有栈足够或耗时增长集中在设备侧时，工作流直接使用已有证据。

### 实现原理

插件在 `skill.yaml` 中导出能力 `python.stack.sample`，契约为 `python-process-sample/v1`。直接调用时，`collect.sh` 接收 Python PID 和采样选项，返回火焰图或原始栈路径。TTFT 工作流把该能力登记为可选依赖，仅在判读结果为 `collect_python` 时请求补栈。

模拟输入由 `replay.py` 读取随包的固定栈，生成带 `simulated: true` 的 `receipt.json`。现场复测由 `archive_sample.py` 核对目标进程、本轮采样授权、时间窗口和预算；成功后交回原始栈和回执。缺少工具或授权时，父流程仍能取得已有判读，并看到待补的证据。

### 验证效果

**构建与单装。** `node --test tests/test_plugin_build.mjs` 的 7 个测试全部通过。Claude 市场清单校验通过，包含部分元数据字段被忽略的警告。在空 Claude 配置中安装 `kptools-py-spy@kpbot-marketplace` 后，安装记录中只有该插件。

**模拟回放。** 从安装缓存运行 `python "$PLUGIN_DIR/skills/kptools-py-spy/scripts/replay.py" decision.json receipt.json`，其中 `PLUGIN_DIR` 为安装记录的 `installPath`。输入模拟的 `collect_python` 决定，实际回执为 `provider=kptools-py-spy`、`simulated=true`、`samples=1000`。其中 710 个样本落在 `api → render_template → compile_template`，与随包栈文件一致。将决定改为 `device_or_wait`，向新的回执路径重跑，脚本返回退出码 2，未生成回执。

**现场采样待验。** 以上结果验证了安装与回放。Linux 现场采样仍待在具备 py-spy 和采样权限的目标机执行。复核命令为 `bash cases/record-flamegraph/entrypoint.sh`（从已安装的 skill 目录运行）。通过条件为退出码 0、火焰图非空、原始栈可见测试工作负载，且 `cases/record-flamegraph/results/result.json` 中 `business_frame_visible=true`。
