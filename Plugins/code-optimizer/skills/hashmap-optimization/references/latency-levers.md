# 访存延迟杠杆库

## 适用前提

本文档在瓶颈建模已判定为**访存延迟受限**后使用。判定特征（两个哈希表实测口径一致）：

| 特征 | cuckoo（10M） | swiss（10M） |
|---|---|---|
| Memory Bound 主导 | L3 Bound 71%（24T，DRAM ~0%） | L3 Bound 73.4%（1T）/ 87.2%（24T） |
| IPC | 0.25~0.42 | 0.94（1T）→ 0.36（24T） |
| DDR 带宽 | 远未饱和 | 非硬带宽墙 |

来源：`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 0/9 章；`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Headline findings"/§"24T scalability"。若 top-down 显示 Core Bound 主导或 DDR 带宽饱和，本库不适用，应转向计算侧或带宽侧手段。

**核心原则：布局 > 指令**。swiss 实测 value 内联（packed）+137~248%，远超指令层 CRC hash +4.7% 与 LDAPR +1.9%——只有攻击 **binding constraint**（value 载荷 DRAM 延迟）的候选才显著移动吞吐（`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Shard-count experiment"）。

**因果耦合约束**：各项收益在各自当时基线上测得，不可线性相加；每轮优化暴露下一个瓶颈，耦合是双向的（预取隐藏访存延迟后锁原子成主导、去锁收益才放大 +13.3%→+44.9%；锁延迟主导时预取收益坍缩 +23%→+4.0%，`CUCKOO_QUERY_OPTIMIZATION_REPORT.md` §7；SoA 降低锁开销后，去锁边际收益又缩水）。见 `CUCKOO_OPTIMIZATION_SUMMARY.md` "收益不可简单相加" 与第 7 章 SoA 协同效应。swiss 侧同型例证：ctrl 环与 value 环单独/联合的收益截然不同（`SWISS_L0_L5_ABLATION_REPORT.md` §三）——评估杠杆组合时优先用累积阶梯而非单层削减。

---

## 杠杆清单

### 1. 批量软件流水线预取（ring buffer 软件流水）

**机制**：批量查询入口改 `D+1` 环形缓冲软件流水线。`make_hint` 一次算齐 key 的 hash + 候选桶 i1/i2 存入 ring，提前 `prfm pldl1keep` 把 key `i+D` 的首桶拉进 L1；D 轮后消费端复用该 hint 驱动查找，同时消除每 key 重复的 `hashed_key`。预取是纯 hint，不改变 find 内核与并发语义（`CUCKOO_BATCH_PREFETCH.md` §2）。

**实测收益**（cuckoo，四档 A/B，基线 `11f9f80`，`objdump` 核对各档 `prfm`）：

| 配置 | prfm 指令 | 1T vs off | 24T vs off |
|---|---|---:|---:|
| off（D=0） | 无 | 基线 | 基线 |
| **D=8, L=3（默认）** | pldl1keep | +20.8% | **+23.2%** |
| D=8, L=1（对照） | pldl3keep | +2.2% | +3.0% |
| D=8, L=0（流式） | pldl1strm | +21.9% | +24.8% |

- **locality 才是杠杆**：L=1（pldl3keep）24T 仅 +3.0%，保到 L3 不拉进 L1 等于丢掉几乎全部收益；两版二进制仅差 `prfm` 目标层级，把收益精确归因到 L3→core 访问延迟（`CUCKOO_BATCH_PREFETCH_LOCALITY.md` §"Streaming variant"）。
- 距离扫描：D=4–12 平坦（噪声内），D≥16 缓退（`CUCKOO_BATCH_PREFETCH.md` §2.1）。早期口径（value 128B 负载）：24T +20.0%（46.4M→55.7M）、P99 216→180ms（−17%）（`CUCKOO_BATCH_PREFETCH.md` §4.1）。
- 微架构佐证：IPC 0.31→0.43，停顿占比从 L3 Bound 70.0%→65.6% 前移至 L1 Bound 13.6%→16.7%，DDR 带宽始终未饱和（`CUCKOO_BATCH_PREFETCH.md` §4.2）。注意 IPC 升幅（+39%）大于 QPS 升幅（~+20%），因预取引入额外指令（instr +38%），端到端收益以 QPS 为准。

**swiss 两级变体**（ctrl 环 PD=16 + value 环 VD=8）：解开「ctrl 确认→读 value」的串行依赖——ctrl 环沿探针序列先行探路，使 value 预取能提前 ~16 槽发起，把单 key 三级数据依赖访存变成两级流水线（`SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2⑥，24T +11.9%）。

**正确性陷阱**：
- ring 容量必须 `kRingSize = kPD + 1`（cuckoo 参考实现），写端与读端下标差 `D < D+1` 模下不重合，新预取永不覆盖未消费槽（`CUCKOO_BATCH_PREFETCH.md` §2）。
- swiss 复刻此流水线时曾踩 **same-slot 覆写 bug**（先覆写 `ring[j%kPD]` 再消费，用 key[j+8] 的 hash 查 key[j]）：命中率跌至 12.5%（8/64），miss 跳过 128B value 拷贝反而使 QPS 虚高 33-42%；单元测试 4/4 仍通过，bug 完全不可见。修复=先消费后预取，或改用 FIFO 语义的 `RingLookaheadQueue`（`SWISS_FILTER_PIPELINE_REPORT.md` §3）。**教训：benchmark 必须输出 found_count 命中率，性能异常优异先怀疑正确性。**
- 条件激活（swiss 两级流水线）：`is_byte_buffer<ValueType> && spw>1 && copy_enabled && n>kPD+kVD`。packed 无 heap 指针不触发；spw=1 时顺序访问硬件预取已足，软件预取反增内存流量致热降频；n 太小填不满 pipeline（`SWISS_FILTER_PIPELINE_REPORT.md` §4.3）。
- **单行覆盖盲区**：`prfm pldl1keep` 单次只预取一个 64B 缓存行。128B 堆 value（vector<float>×32）需第二行预取（value 地址 +64），否则预取只覆盖 payload 前半——cuckoo 两级预取环 objdump 确认 value 环仅 1 条 prfm，后半 payload 仍是 demand miss；锁轴解决后此为下一个候选（预取收益被锁掩盖期无法单独测出，CUCKOO_Q §5/§8）。**已验证**：extent 驱动多行预取（bitwise 读容器 [begin,end) 逐 64B 行循环预取）在锁写意图预取落地后 24T 中位 **+17.6%**（118.96M→138.76M，6/6 正向，CUCKOO_R2 §7）——通用规则：**预取覆盖必须匹配访问足迹**（payload 跨行则逐行预取），且该类残量 miss 的回收只在锁/主延迟消除后有可测空间。

**风险与反证**：
- 次桶预取（SECOND=1）在 0.95 命中率下中性（i1-only 187.8ms vs i1+i2 187.6ms），为低命中场景保守留开（`CUCKOO_BATCH_PREFETCH_LOCALITY.md` §"Key findings"）。
- 双桶预取（锁首桶后预取 i2）24T Query P95 +8.4% 反向：i2 罕用且污染 L1、增首桶 miss（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 8 章）。
- `pldl1strm` 与 `pldl1keep` 统计持平（+1.3%/+1.0%，落在轮间漂移内），保留 keep 默认（`CUCKOO_BATCH_PREFETCH_LOCALITY.md` §"Streaming variant"）。
- ctrl 环单独开启 24T **-5.7%**——没有 value 环接力，ctrl 环单独发不够早；只有两者叠加才强正。累积式消融阶梯才能捕捉此类交互效应，单层削减实验会把 ctrl 环误判为纯负优化（`SWISS_L0_L5_ABLATION_REPORT.md` §三）。

**验证方式**：`objdump` 核对二进制 `prfm` 指令种类与条数；四档时间相邻配对 A/B 取中位；top-down 确认停顿前移 L1；功能用例（`test_cuckoo_direct` / `test_adapter_contract`）两态全过（`CUCKOO_BATCH_PREFETCH.md` §3）。

**次生热点**：主延迟/锁轴消除后，预取 hint 路径自身的计算成本会浮现——value 环为定位 payload 而做的无锁探测与真实 find 存在重复工作（每 key 多一次 hash + 桶读），实测锁轴关闭后 hint 函数升至 47.4% cycles 成为新热点（CUCKOO_R2 §8）。届时候选：hint 环缓存探测结果供 find 复用、或批内 hash 单算复用。

### 2. 锁行写意图预取（延迟型锁的零语义风险杠杆）

**机制**：延迟型锁 RMW 停顿拆为两段——**数据搬运段**（锁行 64B 跨核取回）+ **独占升级段**（invalidate 往返）。批量查询循环中对 key `i+D` 的锁行提前发**写意图预取**（AArch64 `prfm pstl1keep`；GCC `__builtin_prefetch(p,1,3)`），把数据搬运段重叠进在途 key 的处理时间，RMW 落地时行已在本地、只剩升级段。锁照常获取与释放——**零语义变化，混合读写天然安全**，是延迟型锁的首选杠杆（先于 SeqLock/去锁尝试，尤其当去锁被契约或 value 类型约束否决时）。

**实测收益**（cuckoo 200M keys / 24T / 6 对跨进程交错 A/B，hashmap-0902）：

| 指标 | 数值 | 来源 |
|---|---|---|
| 24T QPS（D=1） | **+92.9%** 中位（66.98M→131.28M），6/6 正向 | CUCKOO_R2 §6 |
| 锁路径热点占比 | 75.77% → 14.5% cycles（锁轴关闭） | 同上 §8 |
| IPC | 0.35 → 0.98 | 同上 §8 |
| 每 key 锁开销 | ~257ns → ~51ns | 同上 §6 |
| batch P99 | 382μs → 207μs（−46%） | 同上 §6 |

距离扫描（同进程 alternate，3 对/点）：D=1 最优（+96.7%）、D=2 +95.6%、D=4 +93.5%、D=8 +85.7%——锁行转移延迟（~110ns）短于单 key 处理时间（~360ns），D=1 窗口已足；D 过大时预取行在轮到前被逐出/偷走。与数据环相反（数据 miss 延迟长，D=8~16 才够）。

**适用条件**：
- 锁为**延迟型**（latency-lock，判读见 bottleneck-modeling.md §5.1）而非争用型——争用型锁行在核间快速轮转，预取行会被立即偷走，无收益
- 锁 stripe 地址可**纯算术推导**（stripe index = f(bucket index)，无访存依赖链）——hash → 锁行地址零内存读，预取才能提前发出
- 批量前瞻窗口（batch ≥ 2）；单 key 路径可退化为 hash 后锁行+首桶行并发双预取
- **硬件兑现写意图**：实施前先微验证——`__builtin_prefetch(p,1,3)` 经 objdump 确认映射 `pstl1keep`。GCC 参数语义反直觉：`(p,1,3)`=pstl1keep（L1）、`(p,1,1)`=pstl3keep（L3），写意图必须用 locality=3
- 硬件行为未逐条承诺（PST 取行是否直达独占态微架构相关），以 A/B 实测为准

**与数据环的配合**：锁环 D 小（1~2）、桶/value 环 D 大（8~16），三环各自独立开关、独立扫描距离；锁影子（锁停顿对数据 miss 的 MLP 掩护）消失后，数据环收益会同步放大——落地后必须重扫数据环距离。

**验证方式**：objdump 确认 pstl1keep 落地（含双 stripe 判重分支）且 swpalb/stlrb 原样保留；同进程 alternate 扫 D 选优；独立二进制跨进程 6 对交错定案；锁占比复测确认锁轴关闭（占比 >30% 重回 §5 消融流程）。

### 3. SoA 键镜像 / 探测数据前移

**机制**：桶内新增 `keys_probe_[SLOT]` 键镜像，与 `partials_`/`occupied_`/`seq_` 一并压进第一条 cache line，`values_` 后移——保证探测判定所需全部数据在一个 cache line 内。API 字节不变，footprint 不变（PackedEntry 仍 128B）（`CUCKOO_SOA_KEYS_REPORT.md` §一）。

**实测收益**（cuckoo，PackedEntry/int64，10 轮交错中位）：

| 指标 | 数值 | 来源 |
|---|---|---|
| 24T QPS | **+31.1%**（121.65M→159.51M），10/10 轮方向一致 | `CUCKOO_SOA_KEYS_REPORT.md` §三 |
| 1T QPS | +23% | 同上 |
| 24T LLC 读 miss | **−25%**（2.08B→1.55B） | 同上 §四 |
| 1T LLC 读 miss | −29% | 同上 |

与预取互补：预取把 line1 拉进 L1，SoA 保证命中判定只碰 line1（`CUCKOO_SOA_KEYS_REPORT.md` §四机制）。根因是 `seq_`/`occupied_` 原落 line2（预取未覆盖、demand miss）。

**适用条件**：key 满足 `std::is_trivially_copyable`；非平凡 key（如 `std::string`）经 `[[no_unique_address]]` 条件成员自动回退交错布局，零开销零风险（`CUCKOO_SOA_KEYS_REPORT.md` §五）。Design A（真拆分）≥8B value 时 A/B=0.98 持平，其专属杠杆仅在 value≤4B 时整桶塌缩 64B 单 line（无此负载，未做端到端）。

**swiss 对照（关键认知）**：swiss ring 只预取 ctrl+slot 不预取 value，命中仍停冷 DRAM load——**探测 metadata 便宜、value 载荷才是 binding constraint**（`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Headline findings" 1）。

**验证方式**：交错 A/B 多轮取中位 + 硬件计数（LLC miss、L1D 访问为工作量内禀量，不受噪声漂移）；差分 soak（`test_adapter_differential`）验证镜像一致性。

### 4. value 内联 / packed 布局

**机制**：value 直接内联在 slot/桶内（PackedEntry 8B），消除堆指针追踪与独立分配——slot 预取顺带把 value 拉进缓存。

**实测收益**：

| 实验 | 结果 | 来源 |
|---|---|---|
| swiss packed 8B inline vs vector128 copy=on | 1T **+248%**（5.55M→19.29M）、24T **+137%**（39.95M→94.64M） | `SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Value layout is the lever" |
| cuckoo packed8B vs vector128B（均 locked） | 24T +145%（≈2.5×），2T +213% | `CUCKOO_SEQLOCK_OPTIMISTIC_READ_REPORT.md` §3.2 |
| vector16(16B 堆) vs packed8B(内联) | 1T 8.1M vs 19.3M——同为小 value，2.4× 差距纯来自指针追踪/独立分配 | `SWISS_KPPERF_BOTTLENECK_REPORT.md` 同节 |

这是「布局 > 指令」的最强证据：+137~248% 远超任何指令层手段。

**适用条件**：value 足够小可内联（两表实测均为 8B PackedEntry）；大 value 内联属设计变更（value-type/slot-layout 改动，风险高）。packed 还使 swiss 的 1.6GB working-set 缩回 ~160MB 进 L3，24T 反而 2.4×（`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"24T scalability"）。

**取舍提示**：cuckoo 侧 LEV-5 提出「小 KV 紧凑交错（legacy 单 line）vs SoA_KEYS（镜像+value 后移）在高 hit rate 下的取舍」——推理级 ~100% 命中时 legacy 单行可能更优（hit 不碰 line2），属**开放杠杆、仅分析未实施**，现有 A/B 数据只覆盖 0.8~0.95 训练级命中（`CUCKOO_OPEN_LEVERS_20260626.md` LEV-5）。

**验证方式**：A/B 仅切 value 形态（`--packed` / `--value_size` / `copy_value`），其余不变；SPE LLC-miss 归因确认 miss 从 value 载荷消失。

### 5. SeqLock 乐观读

**机制**：每桶（cuckoo）/每 shard（swiss）引入序列号 `seq_`（偶=稳定、奇=写入中）；读者前后两次采样 seq，校验未变才采信、否则重试，全程不加桶锁——省掉自旋锁原子（LSE acquire RMW + release + 屏障）（`CUCKOO_SEQLOCK_OPTIMISTIC_READ_REPORT.md` §一）。

**实测收益**（cuckoo，packed，seqlock on/off 交错 5 轮中位）：

| 线程 | 2 | 4 | 8 | 16 | 24 |
|---|---:|---:|---:|---:|---:|
| Δ QPS | +12.2% | +8.2% | +32.2% | +49.3% | **+44.9%**（149.03M→215.97M） |

收益随线程数强烈放大（同上 §3.1）。swiss 对照：optimized 构建下 24T **+49.8%**（80.3M→120.3M）（`SWISS_KPPERF_BOTTLENECK_REPORT.md` candidate #2）。

**适用条件**：仅当 key 与 value 均 `std::is_trivially_copyable` 才运行期派发乐观读；`std::vector<uint8_t>` 含内部指针不满足，仍走加锁路径（`CUCKOO_SEQLOCK_OPTIMISTIC_READ_REPORT.md` §二）。

**因果耦合（双向教训）**：预取隐藏访存延迟后，桶锁原子反成主导串行开销，去锁收益才被放大——同一 A/B 在预取尚弱的历史窗口（tfra `e021b34`，2026-05）24T 仅 +13.3%，预取生效后升至 +44.9%（同上 §3.3/§4.1）。镜像反例（CUCKOO_Q §6/§7）：锁延迟主导态下同一两级预取环历史口径 +23% 实测仅 +4.0%——锁未除时数据 miss 已被 OoO 跨迭代 MLP 大部分隐藏，预取只回收暴露残量。**实施顺序由当前 binding constraint 决定，而非固定序列**（见文末实施顺序节）。

**风险与反证**：每 key 2 次 acquire load + retry loop 是残余开销；swiss 实测「分片无锁」还比 SeqLock 单 map 快 +15~19%（省 seq 校验 + cache 局部性，见杠杆 9）。

**验证方式**：on/off 双二进制交错 A/B（cuckoo 须改 `cuckoohash_config.hh` 宏，命令行 `-D` 被覆盖不生效）；差分 soak 验证撕裂读防护。

### 6. 无锁批量查询（query-only 专用）

**机制**：消费端直调 `cuckoo_find`（保留 PD=8 预取 ring），跳过 spinlock（value 路径）与 SeqLock seq 校验+retry（packed 路径）。前提是查询阶段无并发写入（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 7 章）。

**实测收益**（value/vector 路径，SoA=0 口径，三组实现变量隔离 A/B）：

| 口径 | 24T 去锁 Δ | 来源 |
|---|---:|---|
| SoA=0（原测） | **+40.5%**（114.6M→160.9M） | `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 7 章 |
| SoA=1（生产默认，补测） | **+10.3%**（107.2M→118.2M） | 同上"SoA 协同效应" |
| packed SoA=0 | +15.7% | 同上第 7 章 |
| packed SoA=1 | ≈0%（−0.2%，噪声内） | 同上"SoA 协同效应" |

SoA 协同效应：SoA 已把 seq/锁 line 分离降低锁竞争 baseline，去锁边际收益大幅缩水——叠加态下各杠杆收益互相侵蚀的典型样本。

**风险**：仅 query-only 阶段安全（阶段隔离、无并发写）；有并发写必须回退加锁/SeqLock 路径。收益口径必须注明 SoA 开关状态。

**验证方式**：`CUCKOO_FORCE_LOCKED` 宏切换 + `--build-subdir` 双二进制 A/B；功能用例先行。

### 7. 单桶优先策略

**机制**：`find_fn` 先只锁首桶查找，命中立即返回、不触碰第二候选桶 i2——命中首桶时的持锁宽度从双桶缩为单桶（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 2 章）。

**实测收益**：V4→V5 query-only 24T Query P95 **−38.7%**（10.30→6.31ms），1T −17.6%；收益随线程数增大（高并发释放更多锁争用）。

**设计依据**：桶命中分布统计是设计输入——实测 93.3% 查询首桶解决、6.7% 次桶（1T 与 24T 一致）（`CUCKOO_BUCKET_PROBE_B3BC3D4_REPORT.md`，转引自 `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 2 章）。

**适用条件**：两候选桶结构对称的开放寻址表（cuckoo i1/i2）；命中率分布已知或可离线统计。

**验证方式**：命中分布统计（per-bucket probe count）+ P95/P99 尾延迟 A/B。

### 8. RCpc 内存序（LDAPR）

**机制**：编译旗标 `-march=armv8.3-a+rcpc` 使合法的 `memory_order_acquire` load 自动映射为 `LDAPR`（RCpc 弱序 acquire），替换 `LDAR` 的全序屏障与流水线停顿，无需改 C++ 源码（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 1 章）。

**实测收益（实现敏感，两表分化）**：

| 表 | 结果 | 来源 |
|---|---|---|
| cuckoo | 1T Query P95 **−37.2%**（204.583→128.385ms）；24T −23.5%；Query 降幅显著高于 Insert（4~15%） | `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 1 章 |
| swiss | 24T +1.9%，≈噪声级（读临界区非 LDAR 密集，读写不对称不足以放大差异） | `SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2②；`SWISS_L0_L5_ABLATION_REPORT.md` §三 |

**验证方式**：`objdump` 核对二进制落地为 `ldapr` 指令（类比预取轴的 prfm 核对协议，`CUCKOO_BATCH_PREFETCH.md` §3）；零源码改动的旗标级 A/B。

### 9. owner 分片（thread_count == shard_count）

**机制**：每线程独占 shard：整窗一次批量锁 owned shard（锁次数 O(key)→O(shard)），窗口内 key 按分片哈希归属过滤；cache 局部性增益——每 shard ctrl 数组 ~1.7MB 驻 L2，对比单 map 10M entries 的 ~40MB 超出 L2（`SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2④；`SWISS_CONCURRENT_QUERY_OPTIMIZATION_REPORT.md` §二/§四）。

**实测收益**：

| 实验 | 结果 | 来源 |
|---|---|---|
| swiss owner 划分（消链 ④） | 24T **+40.1%**（69.0M→96.6M） | `SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2④ |
| 同项（消融 S4 批次） | 24T +50.4%（69.0M→103.8M，`results/20260901/` 另批窗口） | `SWISS_L0_L5_ABLATION_REPORT.md` §二 |
| 分片无锁 vs SeqLock 单 map（24T） | packed **+15%**（433.5M→497.7M）、vector **+19%**（186.6M→222.7M） | `SWISS_CONCURRENT_QUERY_OPTIMIZATION_REPORT.md` §3.3 |
| 分片无锁 vs shard=1 无锁上界 | **超越上界 +6%（packed）/ +9%（vector）** | 同上 §四 |

超越无锁上界证明分片本身的 cache 局部性净收益超过路由开销。

**前提**：query-only 阶段隔离、可全量预处理分 bin（分 bin+permute 落 `startup_ms` 不计 QPS）；真实流式场景（每 batch 1536 key 到达、无法预处理）路由开销进入热路径，需补「每 batch 分 bin」对照实验重新评估（`SWISS_CONCURRENT_QUERY_OPTIMIZATION_REPORT.md` §5.3）。

**反证（重要，两表结论相反）**：
- **cuckoo 上 shard-owner 为负优化**：24T value 分片亲和 **−11.7%**（160.9M→142.1M）。根因：cuckoo 单表 PD=8 预取已隐藏 L3 延迟、分片缩 footprint 的杠杆被预取覆盖，L3 又全 NUMA0 共享无跨核损失，分片只剩 collect 全扫 T 倍 keys 的纯增开销（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 7 章）。swiss 无预取覆盖 L3 时分片杠杆才有效——**已上预取的表再上分片，先验证收益是否被预取吞掉**。
- 只加 shard 数不除锁频率无效：swiss 256→4096 shards（16×）后 rdlock 占比 36.0%→27.9%，但 24T QPS ~40M→~39.5M 持平。分片只除 contention（缓存行乒乓）不除 per-key RMW frequency，且延迟受限路径上释放的锁周期立即被 value DRAM 停顿吸收（OoO 重叠，hotspot 占比不叠加为独立加速）（`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Shard-count experiment"）。
- ARM 上 mutex 争用恶化：phmap 对照中本机 mutex 8T（7.00M）比单线程（9.18M）还慢 24%，ARM `std::mutex` 争用开销远大于 x86——无锁/批量锁是 ARM 的正确路线（`SWISS_PHMAP_BENCHMARK_REPORT.md`）。

**验证方式**：路由遥测确认 owner-filter 生效（1T 时 `spw=192>64` 守卫自动回落，S4/P4@1T ≈0 是 sanity 锚点）；found_count 逐腿校验。

### 10. 运行时环境：allocator + 大页

**机制**：tcmalloc 消除 malloc 竞争 + 大页（1GB memfs / 2MB THP）把页表项从数万降到个位数，随机访问 TLB miss 大幅下降（`SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2①）。

**实测收益（负载形态敏感，两表结论相反）**：

| 实验 | 结果 | 来源 |
|---|---|---|
| swiss vector128 + tcmalloc-memfs-1gb | 24T **+37.2%**（47.1M→64.7M），1T +50.4%；TLB miss 几乎归零 | `SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2① |
| swiss packed（8B 内联，分配器只作用于表结构） | 24T 仅 +3.6%，并发下近零 | `SWISS_L0_L5_ABLATION_REPORT.md` §三 |
| cuckoo packed + 2MB 大页 AB | 24T **−2.0%**、1T −1.1%（噪声内无收益）；DTLB miss 仅 0.09~0.17% 非瓶颈 | `CUCKOO_HUGEPAGE_AB_REPORT.md` §3.2；`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 6/9 章 |

swiss 100M entry 表 ~50GB 地址空间下 4KB 页 TLB 压力是真实开销；cuckoo 10M 负载地址翻译早已不是瓶颈。

**适用条件**：先测 DTLB 占比（top-down L1 子项）再决定——DTLB 占比高、堆分配大（vector value）才上；延迟受限且 DTLB 已 <0.2% 时大页只剩管理开销（显式 memfs 路径微负）。

**验证方式**：`--allocator` 单变量 A/B（同二进制仅切分配器）；`TCMALLOC_MEMFS_ABORT_ON_FAIL=true` 证明大页确实分配成功；top-down 看 DTLB 子项归零。

### 11. hash 与路由微杠杆

**机制与实测**：延迟受限路径上指令层手段收益有限，但在依赖链或路由放大场景仍有正贡献：

| 杠杆 | 机制 | 实测 | 来源 |
|---|---|---|---|
| CRC 硬件 hash | murmur3 多轮乘法移位 → 单条 CRC32 指令 + 单乘 finalizer | swiss 24T **+4.7%**；指令数 **−5.75%**（152.96B→144.16B），cycles −4.6%（gated PMU 口径 QPS +2~4%） | `SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2③；`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Headline findings" 4 |
| fibonacci 路由免除法 | owner 过滤内层一次 UDIV（多周期非流水化）→ fibonacci fast-range 乘法右移（4 周期流水化） | swiss 24T **+12.7%**（96.6M→109.0M）——24T 时过滤执行 1536 次路由而探针仅 ~64 key，路由被放大 T 倍成为显著开销 | `SWISS_L0_L5_OPTIMIZATION_REPORT.md` §3.2⑤ |
| hashpower 批内单次读取（LEV-6） | `make_hint` 每 key 一次 `hashpower()` atomic acquire load → batch 入口读一次复用 | **开放杠杆，仅分析未实施**，预期消除每 key 一次 atomic load，L3-bound 下可能被掩盖 | `CUCKOO_OPEN_LEVERS_20260626.md` LEV-6 |

CRC 有效的原因：hash 位于访存之前的依赖链上，缩短它等于提前发起 miss；但天花板由访存决定。

**验证方式**：gated PMU（`perf stat --control fifo`）对比指令数/cycles；found_count 校验散列质量（CRC 低位聚集需 finalizer 摊开）。

---

## 实施顺序：由当前 binding constraint 决定

**实施顺序由当前 binding constraint 决定，而非固定序列**：每轮优化后重测 top-down + hotspot 锁占比，攻当前主导轴。锁占比 >50% cycles 时优先锁轴——**先锁写意图预取（零语义风险、混合读写安全），再消融定界，最后才考虑消除/降频 per-key RMW**；数据 miss 主导时优先预取/布局。「先预取后去锁」仅在预取轴尚未实施且锁占比低时成立。判断依据用**消融定界数据**而非历史窗口收益——同一杠杆在不同瓶颈态下收益差 5 倍以上（两级预取环 lock 主导态 +4.0% vs 历史无锁态 +23%，CUCKOO_Q §5/§7）。因果耦合是双向的：锁掩盖预取收益 ⇔ 预取放大去锁收益（SeqLock 历史窗口 +13.3% → 预取生效后 +44.9%，`CUCKOO_SEQLOCK_OPTIMISTIC_READ_REPORT.md` §3.3/§4.1）。**正向亦有压缩效应**：锁写意图预取落地后（锁占比 75.77%→14.5%），单桶优先锁的预估 ROI 从 +40~60% 压缩至 ~5% 而被跳过（CUCKOO_R2 §5/§8）——每轮落地后必须复测重排，不可沿用上一轮的 ROI 排序。

各轴量级与条件依赖（供 ROI 预估，非固定顺序）：

| 轴 | 典型收益 | 条件依赖 |
|---|---|---|
| 布局（SoA/packed/value 内联） | +31~248% | 直接改变 binding constraint；布局决定预取的行覆盖率——SoA 保证预取拉进的 line1 恰含全部探测所需 |
| 预取（ring 软件流水） | 无锁/弱锁态 +11.9~23.2%；lock 主导态坍缩至 +4.0% | 上预取前先看锁占比（负结果台账对照）；128B value 需第二行预取 |
| **锁写意图预取（pstl1keep 环）** | **延迟型锁态 +92.9%（D=1）** | 零语义风险、混合读写安全——锁轴首选；其成功会压缩后续锁杠杆 ROI（单桶优先 +40~60%→~5%），落地后必须复测重排 |
| 锁消除（SeqLock → 无锁批量 → owner 分片） | lock 主导态消融上限 +184.3%；SoA=1 叠加态缩至 +10.3%；cuckoo owner 分片 −11.7% | 写意图预取之后仍残留的锁开销才值得动语义；须按当前基线消融定界后重估；锁消除内部子顺序亦有耦合——swiss「分片无锁」在 SeqLock 之上再拿 +15~19%，叠加递进而非互斥 |
| 环境层（allocator/大页/编译旗标） | swiss +37.2% vs cuckoo −2.0% | DTLB 占比实测决定去留，负载形态敏感 |
| 微杠杆（CRC/fibonacci 路由） | ≤5% | 前面杠杆解决主瓶颈后才有可测空间；hash 坐在访存依赖链之前，是计算侧例外 |

每步一次 commit、A/B 仅差单变量、先过功能用例再测性能、无收益即回滚并记入负结果台账（`CUCKOO_OPTIMIZATION_SUMMARY.md` 第 10 章）。

---

## 负结果台账

| 手段 | 结果 | 根因 | 启示 | 来源 |
|---|---|---|---|---|
| hugepage（cuckoo 10M/packed） | 24T −2.0%、1T −1.1%，无收益 | DTLB miss 仅 0.09~0.17% 非瓶颈；大页不改 L3 footprint | 先测 DTLB 占比再上大页 | `CUCKOO_HUGEPAGE_AB_REPORT.md` |
| shard-owner 分片亲和（cuckoo value） | 24T −11.7% | 单表 PD=8 预取已覆盖 L3 延迟，分片只剩 collect 全扫 T 倍 keys 纯增开销 | 已上预取的表，分片 footprint 杠杆被吞掉 | `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 7/8 章 |
| 双桶预取（锁首桶后预取 i2） | 24T Query P95 +8.4% 反向 | 0.95 命中下 i2 罕用，预取污染 L1、增首桶 miss | 预取目标须匹配实际访问分布 | `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 8 章 |
| ctrl 环单独开启（swiss） | 24T −5.7% | 无 value 环接力，ctrl 环单独发不够早；仅两环叠加强正 | 交互效应须用累积阶梯捕捉，单层削减会误判 | `SWISS_L0_L5_ABLATION_REPORT.md` §三 |
| 次桶预取（cuckoo SECOND） | 0.95 命中下中性（i1-only 187.8ms vs i1+i2 187.6ms） | 首桶即解决，i2 预取罕用 | 中性手段为低命中场景可保守留开，不算收益 | `CUCKOO_BATCH_PREFETCH_LOCALITY.md` |
| pldl1strm 流式预取 | 与 pldl1keep 持平（+1.3%/+1.0%，漂移内） | 桶确属一次性访问，流式无额外净收益 | 无理由从 keep 切换；单次访问负载二者等价 | `CUCKOO_BATCH_PREFETCH_LOCALITY.md` §"Streaming variant" |
| 加 shard 数除锁争用（swiss 256→4096） | 锁占比 36.0%→27.9% 但 QPS 持平（~40M→~39.5M） | 分片只除 contention 不除 per-key RMW frequency；释放周期被 value DRAM 停顿吸收 | 要除 frequency：批量锁/无锁读，不是更多 shard | `SWISS_KPPERF_BOTTLENECK_REPORT.md` |
| 桶内无分支/SVE 比对、批量 hash 向量化（cuckoo） | 24T −8%~−13% | 拉长/加重 load 后依赖链，放弃「命中即早退」短链 | memory-bound 路径计算侧整体无益，标量早退已最优 | `CUCKOO_OPTIMIZATION_SUMMARY.md` 第 8 章 |
| 两级预取环（PD=16 桶环 + VD=8 value 环，pldl1keep）在 lock-latency 主导态 | 24T 仅 +4.0%（6 对交错，6/6 正向），远低于同杠杆无锁/弱锁历史窗口的 +23% | 锁 RMW 延迟主导（锁占 75.77% cycles）下，数据 miss 已被 OoO 跨 key 重叠隐藏，预取只回收暴露残量——预取收益 = 暴露的数据 miss 部分 | **上预取前先看锁占比**；杠杆 ROI 必须在当前瓶颈态下重估，不可沿用历史窗口收益 | `CUCKOO_QUERY_OPTIMIZATION_REPORT.md` §5/§6/§7 |

台账使用规则：负结果不重开，除非基线结构性变化（如 swiss 换 fork 后 NTA 预取改变 baseline，历史 +8~10% 不再复现——`SWISS_KPPERF_BOTTLENECK_REPORT.md` §"Ring precompute"）。

台账与本库的关系：负结果台账是杠杆清单的「反证面」——每条反证都圈定了一条杠杆的失效边界（预取已覆盖时分片失效、DTLB 非瓶颈时大页失效、命中分布决定预取目标、交互效应决定单层评估不可信）。选杠杆时先查台账排除失效条件，再核对该杠杆的适用条件。
