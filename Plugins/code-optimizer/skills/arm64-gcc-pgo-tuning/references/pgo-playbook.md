# GCC PGO 实施参考

按需查阅以下原理、构建、训练、验证及故障处理章节。

## GCC 插桩 PGO 原理与工程检查

### 基本链路

编译器根据运行反馈调整代码生成。训练分布偏向什么路径，候选就可能偏向什么路径；没有普适收益保证。

| 角色 | 参数变化 | 用途 |
|---|---|---|
| B | 既有发布参数，不含 PGO | 正确性与性能基线 |
| G | 编译及最终链接加入生成参数 | 仅训练 |
| P | 编译及最终链接加入使用参数 | 验证候选 |

依据：[GCC Instrumentation Options](https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html) 要求生成参数覆盖编译与链接；文档也说明构建路径与线程更新影响 profile。执行器因此采用固定 build 槽位，并为 joined_threads 使用原子计数更新。线程未正常收尾仍不能视为可靠采集。

```text
G: -fprofile-generate=<run>/profile-live
G（多线程）: -fprofile-update=atomic
P: -fprofile-use=<run>/profile-live
P: -Werror=missing-profile -Werror=coverage-mismatch
```

### 构建路径与对象身份

源码目录、目标名、对象名及编译参数保持一致，仅切换 PGO 阶段。三次构建使用同一绝对 build 路径，构建前清空专属槽位；profile 位于槽位外。产物快照保存于 artifacts/B、G、P，再还原至相同 build 路径测量，避免 RPATH 指向另一版本。

不要“gen 用 build-gen、use 用 build-pgo，然后改 gcda 文件名”。不同目录方案需要专项验证路径映射；首版没有此适配。

### profile 接受与有效性

[GCC Optimize Options](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html) 描述反馈使用及缺失/mismatch 诊断。本 Skill 对目标范围启用错误判定，同时检查完整日志。不得关闭诊断或使用自动纠正去掩盖错误。构建接受 profile 仍不证明训练代表性和性能提升。

`.gcda` 与 LLVM `.profraw/.profdata` 不互换。根据 [GCC 数据文件说明](https://gcc.gnu.org/onlinedocs/gcc/Gcov-Data-Files.html)，`.gcno` 需要相应覆盖率编译支持；不能假设开启 PGO 就能直接得到完整源码覆盖率。本执行器使用匹配版本的 gcov-dump 校验 runs，并把 profile-use 构建诊断作为后续检查。

### ABI、LTO 与架构

保持 B/P 的 ABI、宏、依赖、LTO、优化级别及 ISA 参数一致。检查输出 ELF 和真实工具链；不自动添加 native/SVE/fast-math。对 ABI 的要求参考 [GCC AArch64 Options](https://gcc.gnu.org/onlinedocs/gcc/AArch64-Options.html)。ABI 是否满足调用方仍需项目测试，ELF 检查不替代 ABI 回归。

项目自带 PGO、代码生成器、编译器 launcher、unity/object library、多层 ExternalProject 或自定义链接规则可能需要适配。自动检查无法解释时应停止，而不是从 CMakeCache 推断已经正确完成。


## CMake 库与调用方接入

### 参数注入

执行器通过 CMAKE_PROJECT_TOP_LEVEL_INCLUDES 加载 `scripts/cmake/pgo-targets.cmake`，在顶层配置结束时处理明确 target。要求 CMake >= 3.24。不会改用户 CMakeLists.txt，不使用全局 CFLAGS 给所有依赖插桩。

当前链接证据收集针对 Unix Makefiles 的 link.txt 和 GNU 风格链接 map。Ninja 等 generator 未适配，不能通过更改契约字符串强行执行。

### 静态库

`build.targets` 包含库及必要的 driver target；`build.link_targets` 包含最终可执行程序。`libraries` 指定 static 和 link_target。静态库没有普通最终链接步骤，不能对它套用共享库链接流程，参见 [CMake target_link_options](https://cmake.org/cmake/help/latest/command/target_link_options.html)。

脚本为最终链接输出 map，并检查目标 archive 记录；还需用户明确 correctness 的调用语义。链接 map 证明参与链接，不证明每个 API 被执行。

### 共享库

库和 driver 进入适当 targets/link_targets。构建产物路径来自用户确认的项目配置。身份检查使用 glibc `LD_DEBUG=files` 的实际初始化记录，解析真实路径，不把搜索候选或 ldd 文本当加载证据。

训练和独立身份检查带加载器诊断，正式计时不带。测试 B/P 时还原同一 build 路径，检查产物哈希，防止快照 RPATH 指向当前其他版本。

若库通过不受支持的 namespace、特殊加载器、musl 或不能观察的路径加载，返回 shared_identity_unproven，先完善适配。不要关闭身份检查。

### 调用方与模板

模板/inline 代码可能在 driver 内编译，单独对 archive 插桩没有意义；必须由 Agent 明确编译范围。目标可以是多个编译单元，但不能把 INTERFACE target 当作实际编译目标。

编译数据库检查 target 归属和阶段参数，链接检查最终参数。自定义规则如果未出现在 compile_commands 或 link.txt 中，执行器拒绝；这属于适配缺口而非训练数据问题。

G/P 的编译单元数量和逐项编译、链接命令会与 B 比较，只允许既定 PGO 阶段参数变化；其他变化返回 build_not_equivalent/link_not_equivalent。执行器清除继承的 CFLAGS/CXXFLAGS/LDFLAGS/CPPFLAGS，用户需要保留的设置必须明确放入 build.configure_args，不能依赖未审阅的 shell 环境。

### 负载 wrapper

现有 benchmark 需要参数转换或输出转换时，可以创建 wrapper，但它应由用户批准的命令语义驱动：不得改变输入、线程数、工作量、返回判定或误差容限。避免把 JSON 解析成功当 correctness 成功。固定工具和依赖，不自动安装缺失组件。


## 训练数据与 Profile 质量

### 先明确训练对象

向用户确认调用方、API/操作模式、数据规模和分布、线程数、运行次数、错误/边界路径及业务比例。压缩库需区分压缩和解压，解析库需区分输入结构和错误比例；这些是提问维度，不是自动数据配方。

用户给了目录但没有选择文件时，先只读列出清单，待用户指定或批准明确清单后登记 SHA256。运行期间数据变化立即使实验失效。

### 工作量与合并

每场景执行批准的 runs，采集槽位清空后运行，再归档原始目录。多场景用匹配 GCC 的 gcov-tool 合并，依据 [gcov-tool 官方说明](https://gcc.gnu.org/onlinedocs/gcc/Invoking-Gcov-tool.html)。

首版不提供自动加权：用户期望的请求比例不能直接解释为 raw counter 权重。不同场景的每次运行工作量必须明确；通过获批工作量组合实现训练分布，再无权重合并。保留所有原始场景数据和命令。

按“运行一分钟”训练时，插桩开销会改变完成请求数。开放式服务负载不属于首版范围；有限 driver 应报告自身完成量，由 Agent 与训练合同核对。

### 技术有效性与语义代表性

| 检查 | 证明内容 | 不证明什么 |
|---|---|---|
| 正常退出、无残留子进程 | 生命周期基本完整 | 所有业务路径都被覆盖 |
| profile 有效 runs、版本匹配 | 发生过有效采集 | 数据足够代表真实业务 |
| profile-use 构建无缺失/mismatch | 目标反馈被接受 | 当前目标一定加速 |
| 目标库身份证据 | 调用了正确产物 | 每个 API 调用次数合理 |
| 独立验证 | 获批验证分布上的效果 | 任意未来分布的效果 |

没有通用“覆盖率达到 30% 即可”规则。必须场景没运行就失败；profile 缺少某些目标对象时不能临时忽略。若某模块确实不在业务范围，应先修订明确范围并新建实验。

### 退出与并发

仅支持单进程以及正常 join 的多线程。执行 supervisor 追踪 session，超时或进程退出后留下同 session 子进程会杀掉残留并判失败；这不是面向恶意程序的沙箱，也不能保证捕捉主动 setsid 脱离的进程。未知进程模型必须先向用户确认，不应运行。

异常退出、SIGKILL、profile 损坏时不继续。多线程原子更新也不能替代正确的线程生命周期管理。不要为了采集成功私自降低并发或用单线程 profile 冒充业务多线程训练。

### 数据泄漏与过拟合

训练和验证文件哈希不同只是必要检查，不足以证明分布独立。不得反复试验后挑选验证集上的最佳配置并继续把同一验证集称为未见数据。需要改训练设计时，明确新的实验和验证策略，不能暗中复用结果调阈值。


## 正确性与性能验证

### 验证顺序

先完成 B/P 的获批正确性检查和独立验证，再做性能比较。correct:true 必须来自真实项目校验；容差、输出格式、功能范围由用户明确。若存在 ABI/SONAME/导出符号要求，把检查纳入正确性命令，不在脚本中假装通用 ABI 工具已经验证。

G 不参与性能对比。B/P 除 PGO 阶段外的源码、编译器、优化等级、LTO、CPU 参数和依赖保持一致。

### 测量协议

固定获批数据、CPU/线程/NUMA 策略、指标方向、预热和重复次数。B/P 按 BP、PB 交错；每次还原相同路径并验证身份。正式计时关闭 LD_DEBUG，避免诊断影响结果。

重复与预热方法可参考 [Google Benchmark User Guide](https://google.github.io/benchmark/user_guide.html)；本执行器不依赖该库，要求统一 JSON 指标协议。独立进程轮次是样本单位，不能拿进程内部循环次数充当独立样本。

### 统计实现

首版对每个场景的 B/P 配对做确定性种子的有放回 bootstrap，估计均值比对应的收益区间。吞吐为 P/B−1，延迟下降为 1−P/B。统计参数事前冻结，至少 6 对、1000 次 bootstrap、95% 综合区间；这些最低值不是统计充分性保证。

综合指标为用户权重下的场景百分比收益算术平均，不声称等于真实系统总吞吐。场景退化检查对场景数作 Bonferroni 调整，报告区间会比单场景 95% 更宽。bootstrap 不会修复样本偏差、时间自相关、共享机器干扰或不足的尾延迟请求数。

| 结论 | 判定 |
|---|---|
| improved | 综合区间下限达到最小收益；所有场景退化保护通过；噪声满足合同 |
| regressed | 区间支持整体下降或某场景超过允许退化 |
| no_gain | 数据质量合格，但综合区间上限仍不足最小收益 |
| inconclusive | 噪声太高或区间无法作出以上判定 |

即使均值改善，也可能因不确定性返回 inconclusive。预算到期不追加未经批准的重测。原始配对样本与所有日志保留，禁止删除离群点来得到加速结论。

### 环境限制

脚本核对选定 hostname、工具与文件指纹，但不会证明机器无外部负载、温控稳定或 NUMA 合适。Agent 必须按照获批 isolation 条件核实运行环境；不符合则停止。延迟分位指标需用户提供具备足够请求样本的 driver，不能从几次进程耗时推导 P99。


## 失败诊断与恢复

| code | 原因 | 下一步 |
|---|---|---|
| input_gate | 必要字段缺失或无效 | 报告 blockers，等待用户明确，不补业务默认值 |
| unsupported_host / wrong_host | 非 Linux ARM64 或机器不符 | 用户指定可用目标环境 |
| toolchain_mismatch | GCC/G++/gcov 工具版本不同 | 明确同一套已安装工具，不自动换工具链 |
| run_dir_exists | 目录已存在 | 指定全新实验目录，旧实验不覆盖 |
| inputs_changed | 源码/数据/工具/契约变化 | 新建实验；不能改 state 指纹 |
| build_not_equivalent / link_not_equivalent | 非 PGO 编译或链接参数改变 | 检查构建条件，不把混合变更收益归因于 PGO |
| missing_receipt / stage_evidence_changed | 阶段证据缺失或被更改 | 保留现场，新建实验；不修改 completed 或 receipts 伪造完成 |
| shared_identity_unproven | 未观察到实际目标库加载 | 检查 RPATH、driver、库类型；不能用关闭门控解决 |
| wrong_static_library | 链接 map 无目标 archive | 核对 link_target 与项目真实链接 |
| empty_profile / profile_not_executed | 无有效运行计数 | 查调用身份、退出、权限、训练路径 |
| profile_changed / invalid_profile | profile 改动或格式不匹配 | 保留现场，重建实验 |
| profile_build_diagnostic | profile 使用缺失或 mismatch | 核对阶段参数/路径/对象；不关闭诊断 |
| driver_protocol | stdout 非 JSON 或缺 correct:true | 审查 benchmark wrapper，不伪造正确性 |
| command_failed | 工具/driver 失败、超时或残留进程 | 查看同序号 stdout/stderr/command.json |
| budget_exhausted / disk_budget | 达到批准资源限制 | 报告实际消耗，请用户明确新预算 |
| interrupted_stage | 上次阶段中断 | 本版不复用部分阶段；新建实验 |
| experiment_locked | 并发或异常遗留锁 | 确认无执行进程，再人工移除锁；不能自动抢锁 |

`resume` 是从已完整通过的阶段继续，不是自动修复失败阶段。程序采用保守失败恢复：一旦阶段已经开始却未完成，保留日志并要求新实验。可只读查看旧 report/state/log，不能把旧 partial 产物直接复制成已完成。

实验目录含 `.pgo-owned.json`；清理只允许本目录的 build/profile-live。不要手工改 owner 迁移实验；构建路径属于 profile 身份的一部分。

指纹还包含本包执行脚本、外部命令入口和继承环境的摘要，避免恢复时静默改变执行条件。更换终端环境可能使恢复停止；这是保守检查，不应通过编辑指纹绕过。证据链检测意外变化，不是防止恶意操作者同时重写全部状态的密码学授权系统。

脚本不是安全沙箱：项目构建、driver 和 wrapper 本身是代码，需要在用户已明确的开发环境运行；不得用不受信任的命令或把原始输入插进 shell。


## 原理来源与维护

查阅日期：2026-11-13。链接为官方文档；运行时必须对应实际 GCC/CMake 版本。以下区分官方机制与本包工程选择，不能将本包策略描述为 GCC 强制标准。

| 来源 | 用途 |
|---|---|
| [GCC Instrumentation Options](https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html) | 生成反馈、路径、计数更新与生命周期 |
| [GCC Optimize Options](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html) | 使用反馈、优化行为、缺失与 mismatch 诊断 |
| [GCC Gcov Data Files](https://gcc.gnu.org/onlinedocs/gcc/Gcov-Data-Files.html) | gcda/gcno 的来源与边界 |
| [GCC Invoking Gcov-tool](https://gcc.gnu.org/onlinedocs/gcc/Invoking-Gcov-tool.html) | 离线合并及权重能力 |
| [GCC AArch64 Options](https://gcc.gnu.org/onlinedocs/gcc/AArch64-Options.html) | ABI、CPU/架构参数 |
| [CMake target_link_options](https://cmake.org/cmake/help/latest/command/target_link_options.html) | 静态库与最终链接目标差别 |
| [CMake CMAKE_PROJECT_TOP_LEVEL_INCLUDES](https://cmake.org/cmake/help/latest/variable/CMAKE_PROJECT_TOP_LEVEL_INCLUDES.html) | 无源码改动的顶层接入 |
| [Google Benchmark User Guide](https://google.github.io/benchmark/user_guide.html) | 预热、重复、统计及交错测量 |

本包自己的工程策略：显式用户触发、缺失输入阻断、固定 build 槽位、严格诊断、独立验证、配对 bootstrap、单进程首版范围。它们服务于可审计实验，不是对官方文档的逐字复述。

未采纳已有 KPBot 中的固定收益估计、按源码变化百分比判断 profile 有效、统一覆盖率阈值，以及把框架累计收益归因为纯 PGO 的做法。

更新规则：新增工具版本、generator、进程模型或 profile 类型时，先增加正向/故障注入测试，在实际环境确认，再修改适用范围。不能只更新文档中的“支持”字样。

