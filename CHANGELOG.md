# Changelog

所有重要变更记录于此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### Added
- app-tuner 为 `kptools-prompt-capture` 新增八项任务技能，覆盖 proxy 检查与接入、限时采集、请求分布与会话分析、分词核验、vLLM 日志关联及汇总；仅元数据的采集窗口拒绝热更为全文或 golden，附合成数据评测。
- app-tuner 新增 `kptools-tpsplit-report` skill：TP 拆分（如 TP32 跨机 → TP8 单机 + DP）前后收益对比分析。输入两套本地数据目录（TTFT 分阶段表格 ttft_table.csv + 打点原始日志 + config.json），输出 `.md/.html/.json` 三件套报告；按阶段做 Δms/Δ%/贡献度拆解（收益点判定：Δms ≤ −10 且 Δ% ≤ −15；回退点：Δms ≥ +5 或 Δ% ≥ +30），每个收益/回退点带三级证据链（L1 claim → L2 表格分位数 → L3 原始日志行 `文件:行号` + req_id，best/median/worst 代表请求），HTML 为自包含内联 SVG 图表 + 可折叠证据链；自动检出数据质量告警（守恒违例 >5%、0 行可检标不可判定、input_len 桶占比 L1 >0.15 混杂/缺失不可判定、单侧 <100 低样本、阶段一侧恒 0、总 ΔTTFT 近零时贡献度记不可计算、压测参数不一致时排队阶段暂停归因、日志按请求匹配降级、仿真数据来源标记），混杂时以分输入长度分层对比为准，无原始日志时降级声明证据止步表格层、不编造日志引用；吞吐/显存/长稳明示不在结论范围内。附阶段口径与 TP 敏感性 `references/stage-glossary.md`、仿真数据生成器 `scripts/synth_samples.py`（clean/quality-trap/tables-only 三场景）、tier-A 用例 `cases/skill-contract`（契约 13 项 + 脚本行为回归 59 项，含证据链回查与卫生检查）与触发评估集 `evals/`（evals.json 含 assertions、eval_queries.json 正/负例路由矩阵）
- app-tuner 新增 `uadk-accelerator-optimization` subskill：从热点函数匹配 UADK 支持的密码学（AES/SM4/DES/SM3/SHAx/MD5/AEAD/HMAC/RSA/DH）与压缩（gzip/zlib）算法，命中时建议替换鲲鹏硬件加速器并输出建议配置（SVA 驱动加载、WD_*_CTX_NUM 队列、uadk_engine 接入、uadk_tool/openssl speed 验证），未命中输出不适用结论；含算法映射 JSON 与 playbook，注册到主编排候选规则、Dynamic Skill Catalog 与知识路由；附 tier-A 用例 `cases/skill-integrity` 与触发评估集 `evals/`（evals.json 含 expectations、eval_queries.json 正/负例路由矩阵）
- app-tuner 新增 `vllm-eplb-analysis` skill：vLLM MoE 服务的专家负载不均衡分析。用 balancedness、per-engine 在途请求、per-engine TTFT、prompt token 四类独立指标交叉判定（单指标不下结论、要求 ≥3 窗口与持续性），另设显存余量门控（KV 占用、换出增量、可负担副本数）；换出以两份 /metrics 的窗口增量判定，累计计数不参与门控。输出 imbalanced/borderline/balanced/inconclusive 与证据表，并给出可直接执行的模板改动清单（imbalanced 为 dynamic 参数，borderline 为 force 上限探针），不依赖未随本技能交付的脚本；附指标参考 `references/eplb-metrics.md`、tier-A 用例 `cases/skill-contract`（契约 14 项 + 脚本行为 28 项，含自包含检查）与触发评估集 `evals/`
- app-tuner 新增 `microservice-go-syncpool-optimization` 子 skill：面向 Go 微服务 sync.Pool 堆内存颠簸，给 Go runtime 打补丁新增 `GODEBUG=poolretain` 与 `GODEBUG=poolprealloc` 两个控制开关，覆盖 Go 基线锁定、隔离构建根目录、补丁与 Go 基线匹配校验、工具链构建、服务重编译、二进制等价门控、单变量 A/B 和回退；补丁由外部私有制品提供，仓库只保留行为级说明；子 skill 已注册到任务包目录、覆盖集合和回归测试；附补丁/参数/验证三个 references 分册
- code-optimizer 新增 `generic-timer-optimization` skill：aarch64 热路径时钟读取优化——识别自旋等待/轮询/超时判定循环内每轮迭代的 `Clock::now()`/`clock_gettime` vDSO 读取（seqlock 校验 + mult/shift 换算，单次几十 ns，时间检查可占循环体一半以上），改写为直接读 ARM 通用定时器（`mrs cntvct_el0` 一条指令）+ 循环外读一次 `cntfrq_el0`；锚点保留一次 `Clock::now()` 维持 time_point 与 deadline 同域，`#if defined(__aarch64__)` 门控保留非 aarch64 回退路径；含三类改写模板（deadline 锚点式 / tick 域比较免换算 / 分步换算防中间值溢出）、溢出边界计算（`ticks_diff * 1e9 < 2^64`，100MHz 下单次换算安全跨度约 184s）、`asm volatile` 防 CSE / 频率禁硬编码 / 无符号减法防回绕等不变量、`mrs` 编译验证契约（base A64 系统寄存器不在 arm_query.py NEON/SVE 资产内，用编译器反汇编验证）与读取成本微基准/语义一致性/端到端 A/B 验证步骤；独立调用的叶子 skill，不由 apply-optimization 结构化流水线分派；源自 folly `synchronization/detail/Spin.h` 的 `spin_pause_until` 真实优化案例（Baton/SaturatingSemaphore 等自旋路径共用）；附 tier-A 用例 `cases/skill-integrity`（frontmatter 一致性、输出契约字段与 techniques_used 枚举、溢出边界与安全跨度数值复算、模板 aarch64 门控回退/防 CSE/禁硬编码不变量、评审修复负向看护）
- app-tuner 新增 `ceph-optimization` subskill：面向 Ceph 存储集群的系统与集群级调优体检（依据鲲鹏 BoostKit Ceph 指南 9.3.3/9.3.4）；Ceph 专属项（ceph.conf 全量参数、PG 分布与 balancer upmap、OSD 绑核）输出带备份/持久化/逐节点应用/重启窗口/回退的候选动作，OS 层参数（swappiness/MTU/pid_max/file_max/read_ahead/IO 调度器/nr_requests）与 NUMA 不均衡只读体检输出 os_tuning_findings 证据移交 os-optimization；含调优 playbook 与参数知识库 JSON，注册到主编排候选规则、Dynamic Skill Catalog 与知识路由；附 tier-A 用例 `cases/skill-integrity` 与触发评估集 `evals/`（evals.json 含 expectations、eval_queries.json 正/负例路由矩阵）
- code-optimizer 新增 `parallel-accumulator` skill：归约循环串行累加器并行化（识别单累加器延迟绑定，三级改写纯 C 多累加器 / NEON 单累加器 / NEON 多累加器，浮点结合律与位精确门控，尾处理不回退单累加器），附 `accumulator_plan.py`（按 FP 延迟/吞吐计算累加器数量）、tier-A 用例 `cases/protocol-contract`、agent 端到端 A/B 用例 `cases/agent-optimize-dotproduct`（点积多累加器展开），对标 Intel performance-patterns 的 parallel-accumulator pattern
- code-optimizer 新增 `agent-memory-db-optimization` skill：Agent 记忆系统 / 图数据库后端（Graphiti/Neo4j）五阶段优化（冻结确定性负载 → 客户端/驱动/服务端三段归因 → top-down 与缓存缺失率瓶颈建模 → 分层杠杆实施 → 矩阵复测与跨架构对照）；杠杆覆盖载荷瘦身（不用的向量不回传）、PackStream 解码下沉 Rust、时序水合下沉、读路径多副本扩展、服务端数组/属性解码快路径与向量相似度单遍就地；以 LLM/embedding 缓存回放冻结模型调用，门禁为 0 缓存未命中 + 图规模校验和 + 结果逐字节等价；附 4 个 references（测量协议/瓶颈建模/杠杆库/负结果）与 `llm_replay_cache.py` 及单测；接入统一流水线场景路由（4 个入口文件）
- code-optimizer 扩展 `arm-spe-analysis` skill：新增 spe-parser 标准 CSV 管线与深度时延分析。`spe-csv.sh`/`spe-to-csv.py` 把 SPE 记录解析为 13 列标准 CSV（含 weight/data_src/addr，perf 不支持时自动降级），并支持 `--parser` 接入任意开源 spe-parser（自动 schema 校验）；`spe-latency-report.py` 生成门禁式深度报告：指令时延分布（P50/P90/P99/直方图）、缓存命中层级（L1/L2/L3/Peer/Remote/DRAM + 各层级平均时延）、指令追逐（逐 PC/逐函数采样数与加权时延双热点 + P99 尾部指令）、高时延指令根因（单因子归因 + 组合共现）、tlb/remote/p99/tail 门禁判定、前后对比（--baseline），Markdown + JSON 输出；附 `spe_latency_analysis.md` 参考文档与 `test_spe_csv.sh`/`test_spe_latency_report.sh` 双测试套件（52 断言）。
- code-optimizer 新增 `vector-db-optimization-migration` skill：向量数据库/检索库跨版本优化迁移通用能力（引擎中立迁移流程、Faiss/DiskANN adapter 按需路由、资产分型与 KEEP/ADAPT/SPLIT/OPT_IN/REPLACE/DROP/DEFER 处置、依赖分层 patch 序列、G0–G5 等价性与 recall 零下降门禁、同窗 ABBA 性能验收、失败模式与假阳性排查）；附 12 个 references、`scan_version_delta.py` / `check_compile_flags.py` / `compare_topk.py` 三个脚本，以及身份卡/追溯矩阵/交接单/内核位级探针四个模板；`compare_topk.py` 默认严格等价（查询集/条数/rank 连续性/顺序/距离位模式全等，省略 `--k` 全量比对，`overlap@k` 命名），`check_compile_flags.py` 按参数顺序解析 `-D`/`-U`（含空格形态）、识别空格形态 `-march val`、负向 flag（`-fno-lto` 等）记为显式关闭、uniform flag 缺失即分裂且默认失败，`scan_version_delta.py` 支持 CMake 隐式 OFF 与组合 `defined()`；G5 噪声标定统一为同配置 A/A 自对照口径（门禁文档与交付清单一致）；附 tier-A 用例 `cases/script-contract`（58 项检查覆盖评审两轮反例）
- app-tuner 新增 `kptools-ascend-profiling-analysis-nostack` skill：无栈 profiling trace 的 step 识别（`STEP_TIME` → `kernel_details` → `step_trace_time` → `communication` → 纯 trace 降级）、每步时间、P1/P2/P3 近似阶段、模型结构与 Free 去向 HTML 报告；附 `detect_steps` 单测与触发评测；分析前主动收集模型/版本/源码/with-stack，缺失时降级并标低置信度。
- app-tuner 有栈 profiling 报告增强：首页/每步 Computing vs Free 重点框、按层类型聚类的模型结构（GDN/Attention/MoE/压缩层）、MTP/DSpark 外部模型结构，并去除代表实例框与包装层重复帧。
- code-optimizer 新增 `allocation-path-optimization` skill：分配路径优化（分配下限与尺寸类通胀审计、仅元数据对象精确申请、短生命周期对象抖动合并、成对释放、分配器与内核回收成本归因），源自 evbuffer 引用链元数据被 1024 B 下限放大的真实案例；附 `alloc_size_audit.py` / `alloc_probe.c`、七条分配路径不变量、tier-A 用例 `cases/size-class-audit`、tier-B 用例 `cases/metadata-floor`（自包含夹具，编两版比两版），以及只应用本 SKILL 补丁的真实 libevent 端到端用例 `cases/libevent-interfaces`
- app-tuner 新增 `kptools-dpdk-tuning` skill：DPDK 应用性能调优双模式工作流（现象驱动的分层诊断排查树：PPS/时延/丢包/启动内存/CPU 异常；硬件+场景驱动的分层配置建议：BIOS/内核 GRUB/EAL/mbuf 内存池/PMD 网卡/功耗低时延），附 M1-M10 模块参考与官方文档溯源（UPSTREAM.md），面向鲲鹏 920 + hns3/Hi1822 主场景，不修改业务源码
- code-optimizer 新增 `dpdk-optimization` skill：DPDK 数据面热路径代码五阶段优化（口径确认与线速预算 → 基线冻结 → worker 核 PMU 瓶颈建模：compute/访存延迟/带宽/同步四类判定 → 分层代码杠杆库：热路径纪律/双循环预取/per-lcore 布局/查表解析/无锁统计/mbuf 复用 → 交错 A/B 验证与负结果记账），接入统一流水线场景路由（4 个入口文件），配置层联动 app-tuner 的 `kptools-dpdk-tuning`
- code-optimizer 新增 `hot-path-overhead-elimination` skill：热点路径机制开销消除（主导结果内联、叶子入口拆分、常量宽度拷贝替代通用例程、按实测值频率重排分派、无消费者记账跳过并维护不变量、拷贝后别名重载消除），源自事件缓冲区库 evbuffer 热路径的一线优化记录；附 `value_distribution.py` / `codegen_overhead_probe.py` 两个只读分析器、tier-A 用例 `cases/probe-contract`（脚本契约 + 反汇编对照 + frontmatter/路由泛化看护）、tier-B 用例 `cases/append-notify`（自包含三接口夹具，编两版比两版），以及只应用本 SKILL 补丁的真实 libevent 端到端用例 `cases/libevent-interfaces`（需外部 libevent 2.1.12 源码树，未提供时用例 SKIP；提供后 evbuffer 回归组通过、补丁不含分配路径改动；实测记录见用例 README）
- code-optimizer 新增 `openzl-compression-tuning` skill：OpenZL 压缩内核调优（熵解码根表/块恒等、循环携带补码移位、编码批次与展开约束、熵 codec 选型、无损正确性门禁、负结果与对抗审核档案），源自 KV Cache BF16 一线优化案例（Encode 2.16x / Decode 3.44x）；附 tier-A 用例 `cases/skill-integrity`（frontmatter/引用链接/方法论不变量/敏感内容看护，见 docs/case-spec.md）
- code-optimizer 新增 `openzl-bench-protocol` skill：OpenZL 基准测试工程协议（unitBench 场景注册与熵模式、openzl_benchmark 均值口径、KV 数据准备与分布统计、绑核与噪声控制、多机部署复测、topdown TMA 采集），附 `prepare_kvdata.py` / `bench_kv.sh` / `td_loop.sh` 三个脚本与 tier-A 用例 `cases/script-contract`（脚本契约 + mock unitBench 解析 + 分布统计数值回归）

## [1.0.0] - 2026-09-03

### Added
- npm 包发布流程（`package.json` + `bin/kpbot` CLI）
- `kpbot install` / `uninstall` 子命令
- `.gitignore`、`LICENSE`（Mulan PSL v2）、`CHANGELOG.md`

### Changed
- 首个正式版本发布（从 0.1.0 开发版升级）
- README 新增 npm 安装方式（推荐），git clone 降级为开发者途径
- marketplace.json / plugin.json version 对齐至 1.0.0

## [0.1.0] - 2026-07-21

### Added
- 初始开发版本（git clone + install.sh 分发）
- 2 个插件：code-optimizer、app-tuner
