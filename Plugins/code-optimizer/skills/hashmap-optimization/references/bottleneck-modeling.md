# 哈希表瓶颈建模与判定（bottleneck-modeling）

> 供 hashmap-optimization skill 在优化前/优化中按需 Read：先判定瓶颈类，再进对应杠杆库。
> 所有实测数字标注来源；命令与阈值以 kp-perf skill 及其 `references/devkit-tuner.md` 为准。

来源缩写：

| 缩写 | 文档 |
|---|---|
| CUCKOO | docs/cuckoo/CUCKOO_OPTIMIZATION_SUMMARY.md |
| SWISS | docs/swiss/SWISS_KPPERF_BOTTLENECK_REPORT.md |
| F14 | docs/f14/F14_MICROARCHITECTURE_ANALYSIS_REPORT.md |
| LOCKFREE | docs/cuckoo/CUCKOO_READ_PATH_LOCKFREE_KPPERF_REPORT.md |
| F14_SHARD | docs/f14/F14_SHARD_GAIN_MECHANISM_REPORT.md |
| CUCKOO_Q | hashmap-0902 仓库根 CUCKOO_QUERY_OPTIMIZATION_REPORT.md |
| CUCKOO_R2 | hashmap-0902 仓库根 CUCKOO_QUERY_OPTIMIZATION_R2_REPORT.md |
| CUCKOO_FB | hashmap-0902 仓库根 SKILL_FEEDBACK_AND_CYCLE_TIME_REPORT.md |
| devkit-tuner | kp-perf skill references/devkit-tuner.md |

## 一、三类瓶颈总览

| 瓶颈类 | top-down 特征 | 典型根因 | 杠杆库 | 本项目实测频率 |
|---|---|---|---|---|
| 访存延迟受限 | Backend>20% → Memory Bound>20%，L3/L2 Bound 主导；DRAM Bound 低；IPC<1；Retiring 低 | 随机 key 探测的 LLC cold miss；value 堆分配的指针追踪；预取未覆盖的访问层 | latency-levers.md（预取 / SoA / value 内联 / footprint 压缩） | 最常见：cuckoo V0、swiss 1T/24T、F14 A/B 场景全部命中 |
| 带宽受限 | Memory Bound + DRAM Bound 主导；memory -m 3 实测带宽接近平台上限 | 全表流式扫描（非随机点查）、工作集远超 LLC 的连续重放 | bandwidth-levers.md（压缩 / footprint 缩减 / 合并访存） | 基本封闭：DRAM Bound 0.00%（CUCKOO §9）、F14 DDR 远低于上限、swiss 满并发未达带宽墙（SWISS） |
| 计算受限 | Retiring>30% / Core Bound>20% / Frontend>10% / Bad Spec>5% 主导 | hash 计算、SIMD key 比对、分支密集 | compute-levers.md | 罕见且被反复证伪：SVE −13%、无分支标量 −8%、批量 hash 向量化已撤回（CUCKOO §8） |

锁/同步是独立第四轴（见第五节）：锁开销在 top-down 中散布于 Backend/Core 且与其余 stall 在乱序核上重叠，不归入三分类，必须用 hotspot 函数占比独立检查。

## 二、采集工作流（kp-perf / devkit tuner）

### 2.1 分层采集流程

`top-down -L 1` 定性 → 按结果分流（kp-perf skill 推荐工作流）：

1. **Backend → Memory Bound** → `top-down -L 3` 看 L1/L2/L3/DRAM 档；部分鲲鹏平台 `-L 3` 无法区分 L3-bound 与 DRAM-bound（以实测确认），需三路三角印证：`miss -m 1`（SPE 源行归因）+ `memory -m 2`（cache/miss 计数）+ `memory -m 3`（DDR 实测带宽）
2. **Backend → Core Bound** → `top-down -L 2` → `hotspot`
3. **Bad Speculation** → `hotspot -e br_mis_pred`；**Frontend** → `hotspot`（icache / 分支密度）
4. **Retiring>30%** → 该路径已高效，转向其他轴

### 2.2 命令模板

```bash
DEVKIT=<DevKit CLI 安装路径> && cd $DEVKIT
# benchmark 先以 --profile_loop --profile_op=query 进入纯查询循环，再 attach

# 一阶定性
./devkit tuner top-down -d 10 -L 1 -p <PID>

# Backend→Memory：L1/L2/L3/DRAM 档细分（部分平台不区分 L3 vs DRAM，见 2.3）
./devkit tuner top-down -d 15 -L 3 -p <PID>

# 三角印证（L3 延迟 vs DRAM 带宽 vs 源行归因）
./devkit tuner miss    -d 15 -m 1 -p <PID>                     # SPE：LLC miss 源行
./devkit tuner miss    -d 15 -m 1 --dwarf -s <src> -p <PID>    # 行级注解，需 -g 构建
./devkit tuner memory  -d 12 -m 2                              # cache 访问/miss（PMU，免 SPE）
./devkit tuner memory  -d 12 -m 3                              # DDR 实测带宽
./devkit tuner memory  -d 12 -m 1 -c <核范围>                      # 全量 + 按核范围

# Backend→Core / 热点 / 分支误预测
./devkit tuner top-down -d 10 -L 2 -p <PID>
./devkit tuner hotspot  -d 15 -t 15 -p <PID>
./devkit tuner hotspot  -d 10 -e br_mis_pred -g -p <PID>
```

### 2.3 采集要点（实战修正，必须遵守）

| 要点 | 内容 | 来源 |
|---|---|---|
| attach 真身 PID | `top-down`/`miss` 必须 attach benchmark 真身进程，不是 numactl 父进程——后者 Cycles=0 全零假象 | F14 §6 |
| per-process 优先 | 多线程用 `-p <PID>` 而非 `-c` 按核：自动隔离共享机他人负载（kernel 项 miss <0.1%）；`memory` 无 per-process 模式，只能 `-c` 按核、结果为系统级 | F14 §6；devkit-tuner |
| -L 3 归层失效 | 部分鲲鹏平台 `top-down -L 3` 把 L3-bound 与 DRAM-bound 合并报 L3 档；且部分平台 memory PMU 不暴露独立 L3 命中率（仅 L1D/L2D+DDR）——L3 vs DDR 归层只能三角印证 | F14 §5 / §6.3 |
| SPE 前提 | `dmesg \| grep -i spe` 验证可用性（openEuler 20.xx/22.xx 配置 SPE）；行级 LLC/TLB miss 归因需 root `perf_event_paranoid=-1`，受限时标记降级、以 per-PID top-down 的 L3 Bound 分解为准 | devkit-tuner；CUCKOO §9 |
| 行级注解 | `--dwarf -s` 需 `-g` 构建；`-O3` release 无行表时退化为函数级归因（函数级 + 代码映射仍足够定轴） | SWISS |
| 系统级污染 | `memory` 系统级采集含共享机他人负载，DDR 数仅作量级旁证 | F14 §6.3 |
| 测量协议 | 满并发为主判据、1T 仅参考；绑核单 NUMA 偶数物理核（SMT 首线程）；交错 A/B 取多轮中位；访存层级计数（LLC miss）为工作量内禀量，作机制证据 | CUCKOO §10 |
| 大页池腿串行 | tcmalloc memfs 大页腿每腿独占池容量（如 34GB/腿 vs 64×1GB 池）——多腿并发即 ENOMEM abort（`TCMALLOC_MEMFS_ABORT_ON_FAIL`）；PMU 复测腿必须串行，腿复位以 `free_hugepages` 回满为准 | CUCKOO_R2 §8 |
| 非大页腿禁并发 | 大 working set 匿名内存多腿并发压垮单 NUMA——内核回收函数霸榜（folio_referenced_one 27% cycles），采样全废；热点结构采样宁可串行多腿 | CUCKOO_R2 §8 |
| attach 时序对准 | 先跑时间线腿测量 preload→查询相位起止，再按时间对准稳定查询窗采样；查询窗 <10s 时缩短采样窗（`-t 5~6`），避免采到 preload/生成器段 | CUCKOO_R2 §8 |
| numactl 传 env | `numactl -C ... --membind=... env LD_PRELOAD=... $BIN`——LD_PRELOAD 必须经 `env` 中转，直接作 numactl 参数会被当命令执行报 No such file or directory | CUCKOO_R2 §8 |

### 2.4 原生 perf 降级路径

devkit 不可用时：

```bash
perf stat  -p <PID> -e cycles,instructions -- sleep 10          # 手算 IPC
perf list | grep -iE 'cache|llc|mem'                            # 查本机 PMU 事件命名
# 事件组示例（事件名以 perf list 实测为准，禁止照抄未验证名）
perf stat  -p <PID> -e cycles,instructions,branch-misses,cache-misses -- sleep 10
perf record -g --call-graph fp -p <PID> -- sleep 10 && perf report
```

perf 无现成分级 top-down 事件组，L3/DRAM 归层只能靠事件计数差分，判别力降一档；结论尽量以 devkit 采集为准。`--call-graph fp` 在 `-fomit-frame-pointer` 的 release 构建上栈不完整，需 `-fno-omit-frame-pointer` 重编或接受函数级热点。

## 三、阈值判读表

（devkit-tuner 核实）

| 指标 | 阈值 | 含义 | 下一步 |
|---|---|---|---|
| Backend Bound | >20% | 访存/执行停顿 | `-L 2` 或 `-L 3` 下钻 |
| Memory Bound（子项） | >20% | cache miss、带宽饱和 | `miss` / `memory` |
| Core Bound | >20% | 执行单元/端口争用 | `hotspot` |
| Bad Speculation | >5% | 分支误预测伤流水线 | `hotspot -e br_mis_pred` |
| Frontend Bound | >10% | 取指停顿 | `hotspot`（icache/分支密度） |
| Retiring | >30% | 良好，非瓶颈 | 转其他轴 |
| IPC | <1.0 | 严重停顿，倾向 Memory Bound | — |
| IPC | >3.0 | 已优化充分，该路径收益递减 | — |

机型校准（kunpeng-microarch skill + 实测）：目标平台的理论 IPC 上限（取指/译码宽度、执行流水线数等微架构参数）以 kunpeng-microarch skill 查询与本机实测校准为准。哈希表随机访存负载的实测 IPC 通常远低于理论上限（F14 §6.2）；判读时关注 IPC 相对理论上限的比例与 A/B 相对变化，优化态健康阈值以本机 1T 干净采样标定（CUCKOO §9）。

## 四、判定树（三类瓶颈的判定规则）

### 4.1 访存延迟受限（本项目最常见）

判定链：`Backend>20% → Memory Bound>20% → L3 Bound 主导 + DRAM 带宽占比低 + IPC<1`。

判别规则：

- IPC<<1 且 Retiring 低 → 非计算受限
- L3 Bound 主导 + DRAM Bound≈0 → 延迟而非带宽
- Core Bound 仅 8~9% → 计算侧无益的反证
- DTLB<0.2% → TLB 轴封闭

实测锚点：

| 案例 | Backend | Memory | L3 | DRAM | Core | Retiring | IPC | SPE LLC-miss 归因 | 来源 |
|---|---|---|---|---|---|---|---|---|---|
| cuckoo V0 24T | 87% | 82% | 71% | ~0% | — | — | 0.25~0.42 | ~90% 周期在桶数组 LLC cold miss | CUCKOO §0 |
| cuckoo 优化态 1T | 63.7% | 54.5% | 54.1% | 0.00% | 9.1% | — | 1.07 | — | CUCKOO §9 |
| cuckoo 优化态 24T | 71.3% | 63.3% | 62.9% | 0.00% | 8.0% | — | 0.87 | — | CUCKOO §9 |
| swiss 1T | 86.6% | 82.8% | 73.4% | — | 3.9% | 11.7% | 0.94 | ValueConsumer::consume 91.1% | SWISS |
| swiss 24T | 93.9% | 89.4% | 87.2% | — | 4.4% | 4.5% | 0.36 | — | SWISS |
| F14 A vector 1T | 90.79% | — | — | — | — | 6.00% | 0.48 | ValueConsumer 78.6% | F14 §6.2/6.4 |
| F14 B packed 1T | 77.94% | — | — | — | — | 13.44% | 1.08 | F14WrapperBase 99.9% | F14 §6.2/6.4 |

binding_constraint 定位：SPE `miss -m 1` 的函数/源行归因直接钉到「哪个数据结构哪次访存」。swiss：91.1% LLC miss 落 value 载荷冷加载（128B 堆 value，ring 预取只覆盖 control+slot 行）；F14 A：78.6% 落 `values_[index]` 间接 + 堆第 3 层；F14 B：99.9% 落 chunk 探测（反向校核：value 内联后 ValueConsumer miss 归零，证明归因模型成立）。

判定是闭环而非一次性：每轮优化后必须重测 top-down 确认瓶颈类是否迁移。cuckoo 案例：V0 Backend 87% → 优化态 64~71%、IPC 0.25→1.07，主类仍是 latency，但预取把访存延迟隐藏后桶锁原子上升为主导串行开销，去锁收益才被放大（同一 A/B 历史窗口 +13.3% → 预取生效后 +44.9%，CUCKOO §0）——「优化暴露下一个瓶颈」的因果耦合意味着 binding_constraint 会随优化推进而移动。镜像反例：锁延迟主导态下因果反向——同一两级预取环在无锁历史窗口 +23%，锁占 75.77% cycles 时仅 +4.0%（数据 miss 已被 OoO 跨迭代 MLP 大部分隐藏，CUCKOO_Q §7）。因果耦合是双向的：锁掩盖预取收益 ⇔ 预取放大去锁收益，实施顺序由当前 binding constraint 决定，而非固定序列。

### 4.2 带宽受限与 MLP 不足（三分判据）

带宽墙判据：`memory -m 3` 实测 DDR 带宽接近平台上限（占比阈值以实测标定，如 ≥70~80% 视为接近带宽墙）。本项目历史实测均未达带宽墙：满并发下实测带宽约为平台上限的一半量级（系统级含污染），DRAM Bound 占比接近 0%（CUCKOO §9）。

| 判据 | 延迟受限 | MLP 不足（outstanding-miss 并发槽位） | 带宽墙 |
|---|---|---|---|
| 触发线程数 | 1T 即 IPC 低 | 低并发即扩展饱和（如 4T） | 带宽随线程数爬升至顶后平台 |
| DDR 带宽 | 远低于上限 | 未打满（占比以实测为准） | 接近上限 |
| 扩展曲线 | 线性/近线性 | 早饱和后平台 | 饱和点≈带宽打满点 |
| 本项目案例 | cuckoo V0、swiss 1T | F14 B packed：4T 即 2.32×、满并发仅 3.27× | 无 |
| 对应杠杆 | latency-levers.md（降单 key 延迟） | 深流水线预取提高 MLP（cuckoo ring PD=8 推迟饱和点） | bandwidth-levers.md |

F14 判例（§6.6）：B packed 满并发在 ~4 线程即饱和但 DDR 占比不高，判为共享 LLC→DRAM outstanding-miss 并发处理槽位（MSHR/延迟）耗尽，而非带宽墙。Little's law：吞吐 = 并发度 / 延迟，并发度上限受 MSHR 条目数 + LLC→DRAM 往返延迟约束。对照 cuckoo packed 同框架满并发近线性扩展（318M）——ring 深流水线 + locality=3 预取把随机 LLC miss 化为预取命中，单线程吞吐↑并推迟 MSHR 饱和点。

### 4.3 计算受限

特征：Retiring>30% / Core Bound>20% / Frontend>10% / Bad Spec>5% 主导。哈希表随机访存负载下罕见，且有两个已付学费的陷阱：

- **微基准 L1-hot 误判**：桶内无分支标量微基准 ~1.6×、SVE+SoA ~2.0×，真实负载 −8% / −13%——吞吐密集的 L1-hot 微基准掩盖访存与依赖链成本（CUCKOO §8，证伪教训详见 compute-levers.md）。
- **小工作集误判**：swiss 曾据 IPC≈1.0~1.05 判 instruction-throughput bound，真实 10M×128B 负载实测 L3/DRAM-latency bound——表全入缓存 / 无 value 成本的口径不算数（SWISS headline 2）。

辅助判据：IPC>1 不等于计算受限（F14 B 1T IPC 1.08 仍 Backend 78% memory-bound），Retiring 才是指令效率判据；计算侧小收益真实存在但天花板在内存——CRC hash 指令数 −5.75% 仅换 QPS +2~4%（SWISS）。hash 优化是计算侧唯一稳定有效的一档，原因是 hash 坐在访存依赖链**之前**（先算 hash 才能定位 bucket/slot），压短它等于提前发出 load；而 key 比对向量化等计算侧改动都在 load **之后**的依赖链上，被访存延迟掩盖、无处可省（CUCKOO §8）。

## 五、锁/同步轴独立检查

锁轴必须独立检查、不能从 top-down 推断：F14 曾据「读读不阻塞 + 扩展早饱和」判非锁，后被推翻——封装层每分片 `shared_mutex` 读锁被每键取一次，去锁 +188~549%，读锁争用是 F14 查询主瓶颈（F14 报告头部更正；F14_SHARD）。

锁占比证据（hotspot 函数 cycles 占比）：

| 案例 | 锁函数占比 | 判定 | 来源 |
|---|---|---|---|
| cuckoo SeqLock 读 | 0%（find_fn_optimistic 77.82% + cuckoo_find 21.73% = 99.5%，无任何用户态锁/原子函数） | 瓶颈非锁 | LOCKFREE §3 |
| cuckoo lock_two（baseline spinlock） | **75.77% cycles**（IP 落点，find_fn 为其 75.58% 调用者）；锁消融 +184.3%（64.75→182.08M，3 对交错） | 延迟型锁 RMW（latency-lock） | CUCKOO_Q §2/§3 |
| swiss 1T→24T | rdlock+unlock 15.9%→36.0%（4.5× 放大，shard reader-counter 缓存行乒乓） | 锁是扩展杀手 | SWISS |
| F14 ring 路径 | 每分片 shared_mutex 每键一次；去锁 1T +38% → 24T +540% | 读锁争用是主瓶颈 | F14_SHARD |

单变量 A/B 归因范式（LOCKFREE，24T packed，唯一变量 = `LIBCUCKOO_DEFAULT_OPTIMISTIC_READ` 宏，其余全冻结；不用 pristine baseline 以免混入其他差异）：

| 指标 | A SeqLock | B Locked | 解读 |
|---|---|---|---|
| QPS | 286.7M | 155.7M | 去锁 +84% |
| hotspot 锁/原子占比 | 0% | LSE 原子内联进 locked 路径 90.3% | 锁存在性 |
| L3 Bound | 70.01% | 69.80% | 逐位相同——访存模式与锁无关 |
| Core Bound | 7.66% | 15.38% | 翻倍——锁原子直接代价 |
| DDR read BW | 基线 | 反降 41% | 锁串行化压制 MLP（绝对值以实测为准） |

锁优化的两层含义：

- **contention（争用）**：分片/条带摊薄。swiss shard 256→4096（16×）：rdlock 36.0%→27.9%，但 QPS ~40M→~39.5M 持平。
- **frequency（每 key RMW 频率）**：SeqLock 乐观读 / 按窗口加锁，直接消除原子。swiss optimistic_read 24T +49.8%（80.3M→120.3M QPS，SWISS）。

机理：分片只除 contention；per-key RMW 的固定原子代价无法分片消除（4096 分片下仍残留 ~24% rdlock），且锁原子与 value DRAM stall 在乱序核上重叠——释放的锁周期立即被 value-load stall 吸收（ValueConsumer 39.6→44.3%、probe 23.2→27.1%），QPS 持平。只有攻击 binding constraint（value DRAM 延迟）或完全消除 per-key RMW 才有净收益（SWISS shard-count experiment）。memory-bound 路径上 hotspot 占比相互重叠、不构成独立可加速项——这是占比证据只能定轴、不能预测收益的根因。

### 5.1 锁形态判读：contention-lock vs latency-lock

「扩展随线程劣化且锁占比高 → 锁争用」无法覆盖 latency-lock 形态——扩展看似良好（24T 扩展效率 79%、48T 不劣化），但锁占 75.77% cycles；按旧规则会误判为「非锁，MLP/延迟受限」。两形态判据与对策：

| 维度 | contention-lock（争用型） | latency-lock（延迟型 RMW） |
|---|---|---|
| 扩展曲线 | 扩展随线程劣化，锁占比随 T 放大（swiss rdlock 15.9%→36.0%，4.5×） | 扩展良好（24T 79% 效率、48T 不劣化）但锁函数 IP 占比高（75.77%） |
| 每 key 成本 | 随 T 上升 | **高并发区持平**：24T 389ns ≈ 48T 392ns——一致性织物未饱和的关键证据（若织物饱和则每线程成本随 T 上升） |
| 机理 | 多线程争抢同一锁行，缓存行乒乓随线程数放大 | 每 key 锁 RMW 触发缓存行所有权跨核转移的**固定延迟**，在持锁串行语义下无法被乱序执行隐藏；织物带宽未饱和 |
| 有效杠杆 | 分片/条带摊薄 contention | **分片无效（只除 contention）**——须直接消除 per-key RMW 频率：锁消融定界 / SeqLock 乐观读 / 相位契约（query-only 阶段无并发写） |
| 案例 | swiss rdlock 乒乓（SWISS）；F14 每分片 shared_mutex 每键一次（F14_SHARD） | cuckoo lock_two：75.77% cycles + 48T 每线程成本持平 + 消融定界 +184.3%（CUCKOO_Q §3/§4） |

判读要点：热点里锁占比高时，先算每 key 成本随 T 的走势再定性——成本随 T 上升为争用型（分片可解），成本持平为延迟型（只有去 RMW 频率可解）。延迟型锁与「锁开销被访存 stall 吸收」的 swiss 形态也互为镜像：前者锁延迟本身是 binding constraint（OoO 隐藏不掉持锁串行语义内的等待），后者锁只是与访存重叠的次级开销（占比高但消融定界才是裁决）。

## 六、并发扩展曲线（1T→24T）

方法：1/2/4/8/12/16/24T 吞吐表并计算加速比。F14 实测（§6.6，10M query-only）：

| 线程 | A vector QPS | 加速比 | B packed QPS | 加速比 |
|---|---|---|---|---|
| 1 | 3.97M | 1.00× | 12.7M | 1.00× |
| 2 | 6.62M | 1.67× | 20.5M | 1.61× |
| 4 | 12.4M | 3.13× | 29.5M | 2.32× |
| 8 | 20.2M | 5.08× | 35.5M | 2.79× |
| 12 | 25.2M | 6.34× | 37.6M | 2.96× |
| 16 | 26.2M | 6.60× | 40.2M | 3.16× |
| 24 | 26.7M | 6.73× | 41.6M | 3.27× |

用途与交叉验证规则：

- B packed 4T 即饱和 + 锁占比低 + DDR 未打满 → MLP/延迟受限；同时排除「线程同步开销线性叠加」假设——若是同步开销，饱和应随线程渐进而非 4T 即平台（F14 §6.6）。
- A vector ~12T 饱和（6.73×）；cuckoo packed 近 24× 线性（24T 318M，ring 预取推迟饱和点）；swiss 1T→24T 7.3×（≈30% 效率）、IPC 0.94→0.36 崩塌（SWISS）。
- 判读：扩展早饱和 + 锁占比低 + DDR 未打满 → MLP/延迟受限；扩展随线程劣化且锁占比高 → 锁争用（contention-lock）；扩展良好但锁 IP 占比高且每 key 成本不随 T 上升 → 延迟型锁 RMW（latency-lock，勿因扩展良好误判为非锁，判读见 §5.1）；饱和点与 DDR 打满点重合 → 带宽墙。

## 七、瓶颈模型输出格式

每次判定产出结构化结论：Markdown 表（证据链）+ JSON（机器可读）。`bottleneck_class` 取 `latency | bandwidth | compute | lock`；lock 可与主类共存，以 `secondary_class` 标注。`closed_axes` 必须显式列出已被证据排除的轴，防止重复投入。

Markdown 证据链表样例（swiss 1T/24T，来源 SWISS）：

| 证据项 | 1T | 24T | 判读 |
|---|---|---|---|
| Backend / Memory / L3 Bound | 86.6 / 82.8 / 73.4% | 93.9 / 89.4 / 87.2% | latency 主导 |
| IPC / Retiring | 0.94 / 11.7% | 0.36 / 4.5% | 非计算 |
| SPE LLC-miss 首归因 | ValueConsumer 91.1% | — | value 载荷冷加载 |
| DDR read | — | 未达上限 | 非带宽墙 |
| 锁 hotspot 占比 | 15.9% | 36.0% | 锁为 secondary |
| 扩展加速比 | — | 7.3×（30% 效率） | 内存争用劣化 |

JSON 样例：

```json
{
  "bottleneck_class": "latency",
  "secondary_class": "lock",
  "confidence": "high",
  "evidence": {
    "top_down_l1": {"backend_pct": 86.6, "retiring_pct": 11.7, "ipc": 0.94},
    "top_down_l3": {"memory_bound_pct": 82.8, "l3_bound_pct": 73.4,
                    "core_bound_pct": 3.9, "dram_bound_pct": null},
    "spe_llc_miss": {"top_function": "ValueConsumer::consume", "pct": 91.1},
    "ddr": {"read_gb_s": 25, "platform_ref_limit_gb_s": null, "near_wall": false},
    "lock_hotspot_pct": {"1t": 15.9, "24t": 36.0},
    "scaling": {"1t_qps_m": 5.55, "24t_qps_m": 40.6, "speedup": 7.3}
  },
  "binding_constraint": "value 载荷（128B 堆分配）经 slot 指针的 DRAM 冷加载；ring 预取仅覆盖 control+slot 行，未覆盖 value 行",
  "closed_axes": {"tlb": "DTLB 0.9%~4.9%，非主导", "bandwidth": "DDR 远低于上限，非墙",
                  "compute": "Core 3.9% / Retiring 11.7%；CRC 仅 +2~4%",
                  "sharding": "4096 shards QPS 持平，只除 contention 不除 frequency"},
  "next_lever": "references/latency-levers.md（value 预取 / packed 内联）；锁轴走 frequency 路线（optimistic_read）",
  "source_docs": ["SWISS"]
}
```

要点：`evidence` 只放原始测量值（禁放推断）；`binding_constraint` 必须落到数据结构 + 访存层级（如「value 载荷 DRAM 延迟」而非「内存慢」）；`next_lever` 指向本 skill 杠杆库三文档之一。

## 八、常见误判速查

| 误判 | 真相 | 反例证据 |
|---|---|---|
| attach numactl 父进程采到全零 | 必须 attach benchmark 真身 PID | F14 §6 采集方法修正 |
| 用 `top-down -L 3` 直接分 L3/DRAM | 部分鲲鹏平台上两档合并，只能三角印证 | F14 §5 |
| IPC≈1 判指令吞吐受限 | 真实负载是 latency bound，IPC>1 也可能 memory-bound | swiss headline 2；F14 B 1T IPC 1.08 |
| L1-hot 微基准显示 SIMD 大幅加速 | 掩盖访存与依赖链成本，真实负载退化 | CUCKOO §8（−8%/−13%） |
| Memory Bound 主导 ⇒ 锁没问题 | 锁开销与访存 stall 重叠，必须 hotspot 独查 | F14「非锁」结论被推翻（+188~549%） |
| 加分片就能提升吞吐 | 分片只除 contention 不除 per-key RMW 频率 | swiss 4096 shards QPS 持平 |
| hotspot 占比 = 可回收收益 | memory-bound 路径上占比重叠、不叠加为独立加速 | swiss shard experiment |
| 扩展早饱和 = 同步开销 | 低并发即平台 + DDR 未满 = MLP/并发槽位受限 | F14 B 4T 饱和，DDR 未打满 |
| 大页必受益 | DTLB 占比 <0.2% 时 TLB 轴已封闭 | CUCKOO §6/§9（24T −2.0% 噪声内） |
