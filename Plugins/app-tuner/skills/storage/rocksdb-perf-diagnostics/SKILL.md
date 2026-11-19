---
name: rocksdb-perf-diagnostics
description: 诊断并实际执行 RocksDB 性能优化。Use this skill whenever the user mentions RocksDB 性能、慢读/慢写、p99 尾延迟、写停顿 / write stall、compaction 抖动或 backlog、LSM 调优、ColumnFamilyOptions / DBOptions、block cache、bloom filter、write/read/space amplification,或对 RocksDB 跑 db_bench / YCSB 遇到瓶颈;even if they don't say "优化" explicitly. 覆盖 C++ 与 Java(JNI) 两种接入,以及只有 LOG 的现场排障。
license: MulanPSL-2.0
compatibility: Agent Skills-compatible runtime; requires bash and python3. Read-only analysis is the default; changing DB options or running writes requires explicit user authorization.
metadata:
  skill_type: debugging
  category: diagnostics
  domain: rocksdb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# RocksDB 性能诊断与优化

这个 skill 的目标不是背参数,而是**用 RocksDB 自己的观测面定位瓶颈,然后真的动手改、复测、验证**。RocksDB 是 LSM 结构,"慢"几乎总能落到三类放大(写放大、读放大、空间放大)之一,或落到后台线程 / IO / 内存的背压上。先把证据拿到,再改。

## 适用范围

**负责:**

- 用 LOG、DB properties、Statistics、perf_context、EventListener、ThreadStatus、ColumnFamilyMetaData、StatsHistory、Trace、`ldb` / `sst_dump`、系统层工具定位 RocksDB 性能瓶颈。
- 覆盖 C++ 直连与 Java(JNI) 两种接入,以及只拿到 LOG 的现场排障。
- 在用户授权下**实际执行**优化:改可动态参数、改配置重编译、重跑压测、复测与回退。
- 产出结构化诊断报告(见 `references/report-template.md`)。

**不负责:**

- 不替代业务侧的容量规划、硬件选型或架构决策。
- 不在未授权时执行破坏性操作(`ldb compact`、删除/移除 SST、写入类 `ldb` 命令)。
- 不为某个固定 workload 提供"一键参数模板";参数必须由证据推出。
- 不把跨版本的 property / ticker / perf 字段当成稳定契约。

## 核心原则

- **无度量不调参**:没有 before/after 数据的改动一律不算优化。
- **一次只动一个变量**:同时改多个参数,复测结果无法归因。
- **先预测再验证**:改动前写下"预期哪个指标怎么变、预期收益多少",复测对不上就说明假设错了。
- **已知手段必须覆盖**:形成假设前先过一遍 `references/optimization-playbook.md`,用覆盖表记录"执行/跳过 + 理由",不能只凭记忆选参数。
- **架构特定优化先查环境**:SVE2/AVX2 等向量化补丁先跑 `scripts/check_arch_sve2.sh` 或等价检查;不满足就跳过并记录证据。
- **源码补丁先验证正确性**:先跑补丁自带的测试(如 `bloom_impl_test`),再做性能对比;测试 skip 或失败不得声称优化有效。
- **无收益不保留**:每个改动必须有 before/after 数字;收益不超过噪声就回退,不能靠"应该有效"保留。
- **优先零侵入观测**:LOG / property → Statistics → perf_context / EventListener → 系统层 tracing,按侵入性和开销递增选择。
- **随时可回退**:改配置前先快照(文件备份 / 记录原值),记录每轮变更。

## 工作流程

下面 Phase 0–6 构成完整闭环。诊断阶段默认只读;执行阶段必须在用户授权下进行。

### Phase 0 — 侦察(先搞清楚在优化什么)

动手前必须确认(缺失就问用户,不要猜):

1. **RocksDB 版本**:C++ 用 `rocksdb::GetRocksVersionAsString()` 或读 `include/rocksdb/version.h` 的 `ROCKSDB_MAJOR/MINOR/PATCH`;Java 用 `RocksDB.rocksdbVersion()`。版本决定哪些 property / ticker / perf 字段存在,不要跨版本套用。
2. **接入方式**:C++ 应用 / Java(JNI) / 直接用 `db_bench` 或 `ldb` / 只拿到 LOG 和监控数据。
3. **负载与目标**:读密集还是写密集?点查 / 范围扫描 / 混合?目标是吞吐、P50、P99、抖动、还是写停顿?SLO 是多少?
4. **运行形态**:服务能否重启?能否改代码重编译?有没有现成压测(db_bench 参数 / YCSB workload)?
5. **架构/编译条件**:目标 CPU 架构(aarch64 / x86)、SVE2 / AVX2 等特性、SVE 向量长度、编译器版本、能否重新编译 RocksDB,以及**构建机与部署目标机是否同构**(向量化补丁的全局 flags 会绑定 CPU 能力)。源码级/向量化补丁必须先查这些。

完成标准:能用一句话说清"谁的、什么负载、什么指标、什么约束"。

### Phase 1 — 选择观测手段

从最便宜的层级开始,需要更细的证据再往下走。完整清单与 C++/Java 差异见 `references/observability.md`。

| 层级 | 手段 | 改代码/重启 | 能回答什么 | 详见 |
|---|---|---|---|---|
| 1 | **LOG 日志** (`LOG`) | 否 | 是否写停顿及原因、compaction/flush 频率与耗时、后台错误 | `references/log-analysis.md` |
| 2 | **DB properties** (`GetProperty` / `ldb get_property`) | 否 | L0 文件数、pending compaction bytes、`is-write-stopped`、层分布、cache 占用 | `references/properties-tools.md` |
| 3 | **Statistics**(tickers + histograms) | 需挂 `options.statistics`(多数应用已有) | 操作延迟直方图、stall 计数、IO 字节、cache/filter 命中 | `references/statistics-perf.md` |
| 4 | **perf_context / iostats_context** | 需开 `PerfLevel`,**仅 C++** | 单次读写的耗时拆解(seek、block read、mutex 等待…) | `references/statistics-perf.md` |
| 5 | **EventListener / ThreadStatus / ColumnFamilyMetaData** | 需改代码 | 精确 stall 原因、每次 compaction 的读写字节、后台线程状态、LSM 形状 | `references/properties-tools.md` |
| 6 | **系统层**(`iostat`、`perf`、`blktrace`) | 否 | 磁盘饱和、CPU 热点、锁竞争 | `references/observability.md` |

经验:先 1 + 2 就能得到大部分结论;只有当"到底慢在哪一段"说不清时才升到 4 / 5。

### Phase 2 — 采集并定位瓶颈

按 `references/symptom-to-cause.md` 的矩阵走:把观测到的**症状**(例如 `Stalling writes because we have 32 level-0 files`)映射到**瓶颈类别**与**首要怀疑参数**。

四类常见瓶颈:

- **写放大 / compaction 跟不上**:L0 文件数高、pending compaction bytes 高、stall 频繁、compaction 读写字节远大于业务写入字节。
- **读放大**:点查延迟高、block cache 命中低、每层文件多、bloom filter 未生效。
- **空间放大**:SST 总量远大于逻辑数据量、delete 后空间不回收。
- **资源背压**:IO 饱和(rate limiter 延迟)、后台线程不足、memtable 不够导致 flush 跟不上。

完成标准:能写出"瓶颈 = X,证据 = 具体的 LOG 行 / property 值 / 直方图",而不是"可能是参数问题"。

### Phase 2.5 — 已知手段覆盖(强制)

定位到瓶颈后,先打开 `references/optimization-playbook.md`,把与当前瓶颈相关的已知手段逐条过一遍,填 `references/candidate-matrix.md` 的"覆盖表":

- 每个相关条目都要有"执行 / 跳过 + 理由"
- 跳过要写清:条件不满足 / 风险过高 / 已有更强手段 / 需授权
- 证据必须指向 Phase 2 的实际观测
- 源码/架构级方法(如 `optimization-playbook.md` §3 的 SVE2 bloom 补丁)先跑 `scripts/check_arch_sve2.sh`;不满足写"跳过:环境不满足"并附检查输出

完成标准:覆盖表填完,且至少有一个候选进入 Phase 3。没有覆盖表,不得进入 Phase 3。

### Phase 3 — 形成优化假设

候选**只能来自 Phase 2.5 的覆盖表**;如果是表外新假设,必须显式标记"表外假设"并说明为什么覆盖表没有覆盖。对每个候选写清楚:**改什么、预期哪个指标怎么变、预期收益是多少、代价是什么、怎么验证**。参数作用与代价见 `references/options-map.md`;已知手段见 `references/optimization-playbook.md`。

选择原则:优先动**与已定位瓶颈直接相关**的参数;优先**可动态修改**的(`SetDBOptions` / `SetOptions`,C++ 与 Java 都有),避免重启;每次只选一个。

### Phase 4 — 执行优化(真的动手)

按接入方式选择执行路径,每一步记录原值:

- **db_bench**:改命令行参数重跑。常用观测 flag:`--statistics=1 --histogram=1 --perf_level=<n> --stats_interval_seconds=N --report_interval_seconds=N --report_file=...`。先跑 baseline,再改**一个**参数重跑。
- **C++ 应用**:
  - 可动态改的:`db->SetDBOptions({{"max_background_jobs","8"}})`、`db->SetOptions(cf, {{"write_buffer_size","268435456"}})`。
  - 需重启的:改 `Options` / `ColumnFamilyOptions` 构造处,重编译。
- **Java(JNI)**:`rocksdb.setDBOptions(MutableDBOptions)`、`rocksdb.setOptions(cf, MutableColumnFamilyOptions)`(动态);否则改 `Options` / `ColumnFamilyOptions` 后重编译。Java 没有 perf_context,深度拆解要靠 Statistics + EventListener。
- **只读现场**:用 `ldb` 读 property / log、用 `sst_dump` 看 SST 属性,不改配置。

执行纪律:

1. 记录改动前配置(文件快照或参数原值)。
2. 若服务在跑,先确认负载稳定(预热后测),避免把冷启动当优化。
3. 每个变体至少一次 baseline + 一次对照,条件(数据量、并发、硬件)保持一致。
4. 记录环境:版本、CPU、磁盘、内存、数据集大小、并发数。
5. **改动前写下预期收益**(目标指标 + 预期方向/幅度);复测后对不上,按 Phase 5 判为否证并回退。

完成标准:拿到一组可比的 before/after 指标,且改动可以精确复述。

### Phase 5 — 验证与回退

- 用**同一个指标、同一负载、同一环境**复测;建议 3 次取中位数并记录方差。
- 源码补丁必须先跑补丁自带的正确性测试(如 `bloom_impl_test`);测试 skip 或失败不得进入性能结论。
- 填 `references/candidate-matrix.md` 的"收益验证表":baseline / after / delta / 重复与方差 / 目标达成? / 保留或回退。
- **收益必须超过噪声**;无收益、回归或无法验证 → 回退,并明确写"该假设被否"。
- 确认预期指标改善,且没有把别的指标(如 P99、空间、恢复时间)恶化。
- 假设不成立时:**不要在一个无效方向上叠加更多参数**;回退,换下一个候选。
- 回退方法:恢复配置快照 / `SetOptions` 回原值 / 重跑原 baseline 参数。

完成标准:每个改动都有 before/after 数字、delta、重复与方差和"保留 / 回退"结论;没有数字的改动标"未验证"。

### Phase 6 — 输出结构化报告

按 `references/report-template.md` 输出,必须包含:

- 基本信息、现象与 SLO、观测数据、瓶颈定位、根因
- **已知手段覆盖表**(执行/跳过 + 理由)
- 优化方案(预期收益 / 代价 / 风险)
- **收益验证表**(baseline / after / delta / 重复与方差 / 保留或回退)
- 验证与回退、未采用手段及原因、遗留项

## 护栏

- 不要在生产库上做**破坏性**操作(`ldb compact`、删除/移除 SST、写入类 `ldb` 命令)除非用户明确授权并已备份。
- 不要凭"常见调优"一次改一堆参数;没有证据的改动要标为"待验证假设"。
- 注意 tradeoff:调大 `write_buffer_size` 换吞吐 = 更多内存 + 崩溃恢复更慢;提高压缩 = CPU↑、写放大可能变化。
- 版本差异:property 名、ticker 枚举、perf 字段随版本变化,**以实际版本源码/文档为准**;读到空值先怀疑版本不匹配。

## 脚本工具

- `scripts/parse_rocksdb_log.py` — 从 LOG 抽取 stall / compaction / flush 事件并汇总;只读,支持 `--json`。
- `scripts/collect_properties.sh` — 用 `ldb` 零改代码采集关键 property;`--full` 附带 `rocksdb.stats`。
- `scripts/check_arch_sve2.sh` — SVE2 bloom 补丁的环境前置检查:架构、SVE2、256-bit 向量、编译器、RocksDB 版本、`git apply --check`;只读,退出码 0/1/2。

脚本遵循 PEP 8 / 小写下划线命名;运行前确认 `python3` 与 `ldb` 可用。

## 参考资料

- `references/observability.md` — 全部观测面、侵入性阶梯、C++/Java 能力矩阵、系统层工具。
- `references/log-analysis.md` — LOG 结构、关键行含义、stall / compaction / flush 读法。
- `references/statistics-perf.md` — Statistics(StatsLevel、ticker/histogram)、perf_context / iostats_context / PerfLevel。
- `references/properties-tools.md` — DB properties 清单、EventListener、ThreadStatus、ColumnFamilyMetaData、ldb/sst_dump/db_bench。
- `references/symptom-to-cause.md` — 症状 → 证据 → 病因 → 优化方向 对照表。
- `references/options-map.md` — 参数按读/写/空间放大归类 + 代价与副作用。
- `references/optimization-playbook.md` — 已知优化手段:命中条件、确切参数、配置写法、生效范围、代价、验证指标、源码依据(含 §3 SVE2 bloom 补丁)。
- `references/patches/bloomfilter_sve2.diff` — RocksDB 6.26.1 的 ARM SVE2 bloom filter 向量化补丁(含 ARM CRC32C 汇编与 `bloom_impl_test`)。
- `references/candidate-matrix.md` — 已知手段覆盖表 + 收益验证表模板(Phase 2.5 / Phase 5 强制产物)。
- `references/report-template.md` — 结构化诊断报告模板。

## 信息来源与置信度

- 主要来源:RocksDB 源码,核对版本 **6.26.1**(`include/rocksdb/version.h`)。
  - `include/rocksdb/{statistics,perf_context,perf_level,iostats_context,listener,thread_status,db,stats_history,options,advanced_options,table,convenience}.h`
  - `db/column_family.cc`(stall 日志)、`db/internal_stats.cc`(stats dump / `Stalls(count)`)、`db/compaction/compaction_job.cc`(compaction 日志)
  - 工具:`tools/db_bench_tool.cc`、`tools/ldb_cmd_impl.h`、`tools/sst_dump_tool.cc`
- Java API 来源:`java/src/main/java/org/rocksdb/`(`Statistics`、`StatsLevel`、`TickerType`、`HistogramType`、`EventListener`、`ColumnFamilyMetaData`、`ThreadStatus`、`RocksDB`)。
- **置信度与限制**:默认值以 6.26 为准;property 名、ticker / histogram 枚举、perf 字段、可动态修改的选项**随版本变化**,使用前必须按实际版本源码核对。日志与指标结论的可信度取决于采样窗口和观测面覆盖度;证据不足时明确标注"未验证"。
