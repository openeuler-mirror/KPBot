# 计算杠杆库（含证伪教训）

> 供 AI 在判定"计算受限"或考虑计算侧优化（SIMD 桶内比较、无分支改造、hash 向量化等）时阅读。
>
> **适用前提**：仅当 top-down 显示 Retiring 高 / Core Bound 或 Frontend / Bad Speculation 主导时才谈计算优化。哈希表随机访存负载下这罕见——cuckoo query 路径实测（CUCKOO_OPTIMIZATION_SUMMARY.md 第 0/9 章，int64 key / 10M / hit-rate 0.95）：
>
> | 指标 | V0 基线 | 1T 全量优化态 | 24T 全量优化态 |
> |---|---|---|---|
> | IPC | 0.25~0.42 | 1.07 | 0.87 |
> | Backend Bound | 87% | 63.7% | 71.3% |
> | └ Memory Bound | 82% | 54.5% | 63.3% |
> | 　　└ L3 Bound | 71% | 54.1% | 62.9% |
> | Core Bound | — | 9.1% | 8.0% |
>
> 即便预取 + SeqLock + SoA 全量优化后，Core Bound 也仅 8~9%，残余瓶颈仍是 L3 访问延迟。
>
> **核心警告**：cuckoo 项目系统性证伪了计算侧优化——"标量命中即早退的短依赖链已是最优态"（SUMMARY 第 8 章总结论）。微基准数据不可直接外推到真实负载（见第一节）。

## 一、微基准陷阱（最重要）

### 1.1 陷阱机制

桶内 key 比较的微基准通常 **L1-hot**：数据驻留 cache、无真实 miss、隔离出纯计算吞吐。这是**吞吐密集场景**；而真实哈希查询是**延迟型**负载——每次比较前先等一次冷 miss 的 cache line 到达，桶到达后的关键路径长度才是计费项。两轴不同：微基准测"单位时间能比多少个 key"，真实瓶颈是"cache line 到达后依赖链有多长"。微基准因此掩盖两项真实成本：

1. **访存成本**：任何新增内存区（如 SVE 影子数组）在微基准里免费（L1 命中），在真实负载上是净增流量；
2. **依赖链成本**：微基准里多条指令可流水重叠，真实负载中比较指令排在 miss 之后，只有串行依赖链长度计费。

### 1.2 实测案例

（CUCKOO_BUCKET_COMPARE_SIMD_REPORT.md §根因 + SUMMARY 第 8 章）

| 方案 | 微基准（L1-hot） | 真实负载（query-only, int64, 10M, 24T） | 反差 |
|---|---|---|---|
| 无分支标量比较 | ~1.6× | **−8%**（clang 59.1M → 54.4M QPS） | 微基准 +60% → 真实 −8% |
| SVE 4-in-1（+SoA 影子数组） | ~2.0× | **−13%**（clang 66.0M → 57.1M；gcc 53.4M → 46.5M） | 微基准 2× → 真实 −13% |

微基准收益来源（吞吐型）与真实瓶颈（延迟型）不同轴，"掩盖了访存与依赖链成本、误判了真实瓶颈"（SIMD 报告原话）。

### 1.3 判别规则

- 微基准结论 → **必须在真实负载 A/B 复验才能采纳**；未复验的微基准收益一律视为未证实。
- 微基准只用于筛选（排除明显更差的方案），不用于证明收益。
- 凡微基准设计含"L1-hot / 数据驻留 / 隔离计算"任一特征，默认其结论不可外推。

## 二、已证伪杠杆（负结果）

每条：手段 / 机制 / 微基准表现 / 真实负载表现 / 根因。共同背景：query 路径访存延迟受限（L3 Bound 54~63%），桶 cache line 到达后比较本身不是吞吐瓶颈。

### 2.1 无分支标量比较（branchless）

- **手段**：`try_read_from_bucket` 对 `is_simple` key 改为固定遍历 4 槽、无 `continue` / 提前 `return`，按位与合成命中布尔 + `csel` 累计槽位（开关 `TFRA_CUCKOO_BRANCHLESS_MATCH`）。
- **微基准**：~1.6×（L1-hot）。
- **真实负载**：clang 24T **−8%**（59.1M → 54.4M QPS；SUMMARY 第 8 章反证表 / SIMD 报告 §性能）。
- **根因**：放弃"命中即停"把"平均比 ~2 槽"变成"必比 4 槽"，多出的 µop 未被访存延迟掩盖（SIMD 报告 §根因）。
- **辨析（勿混淆两类分支）**：
  - 桶**间**分支高度可预测：93.3% 查询命中首桶 / 6.7% 次桶（1T 与 24T 一致，SUMMARY 第 2 章）——这是"命中即早退平均只比 ~2 槽"的原因；
  - 桶**内**空槽 `continue` / 命中 `return` 分支在随机 key 下方向不可预测（SIMD 报告 §背景）。
  - 无分支改造移除的是后者，但换来的代价是 µop 增量加在关键路径上——被访存停顿掩盖的分支本来就不计费，新增 µop 却计费，净效果为负。

### 2.2 SVE 一条比 4 槽（4-in-1）

- **手段**：桶内新增连续影子 key 数组 `keys_[SLOT_PER_BUCKET]`（SoA，`setKV` 同步写入），一条 `svld1` + `svcmpeq` 比完 4 槽（本机 SVE2 向量长 256 位，恰好 4×int64），occupied 经 `svld1ub` 加宽为谓词掩码，`svbrkb` + `svcntp` 取首个命中槽（开关 `TFRA_CUCKOO_SVE_MATCH`）。
- **微基准**：SVE+SoA ~2.0×（L1-hot）。
- **真实负载**（SIMD 报告 §性能，24T CV 极小、结果稳健）：

| 编译器 | 线程 | 基线（OFF） | 优化（ON） | 差异 |
|---|---|---|---|---|
| clang | 24 | 66.0M QPS | 57.1M QPS | **−13.4%** |
| gcc | 24 | 53.4M QPS (CV 0.17%) | 46.5M QPS (CV 0.16%) | **−13%** |
| clang | 1 | 4.34M QPS | 3.84M QPS | −11.7% |

- **根因**：(1) 影子 key 数组是**新增内存区**，每次查询多读一段，在访存受限路径上净增流量；(2) SVE 路径依赖链更长（`ld1d→cmpeq→and→ptest→brkb→cntp`），桶到达后才能算，关键路径延迟反高于标量早退（SIMD 报告 §根因）。
- **延伸反证**：同一根因下，SVE + 完整 SoA（去除影子数组的内存重复）预计仍无益于此 query 路径，除非先经 perf 证明比较确实占 find 的可观比例；不建议投入（SIMD 报告 §结论）。

### 2.3 批量 hash 向量化

- **手段**：对批量查询的 hash 计算做向量化。曾实施后撤回。
- **真实负载**：无收益，已撤回（SUMMARY 第 8 章反证表）。
- **根因**：hash 计算便宜（`CrcHash` 仅 `__crc32cd` + 1 次乘法，ARM-LEVERS P3-2），且已被 PD=8 预取流水线掩盖在访存延迟之下，向量化无处可省。

### 2.4 分片亲和模式 B（间接负结果，同源教训）

- **手段**：单表拆 T 个子表，每 worker 全扫 keys、filter 自己的 shard 后 collect。
- **真实负载**：24T value **−11.7%**（160.9M → 142.1M QPS，SUMMARY 第 7 章）。
- **根因**：PD=8 预取已隐藏 L3 延迟，分片缩 footprint 杠杆被预取覆盖，collect 全扫 T 倍 keys 成纯增开销。
- **教训**：预取流水线会掩盖/吸收一批看似独立的计算与布局优化——评估任何"省计算"杠杆前先确认该计算是否已被预取掩盖。

**总结论**：SVE / 无分支都是"拉长 / 加重 load 后依赖链"的方向，实测都退化；此轴无需再投入（SUMMARY 第 8 章）。

### 2.5 实现层附带陷阱：RCPC 与 SVE 的编译标志互斥

计算侧实验开 SVE 时发现（SIMD 报告 §工具链备注）：`-march=armv8.3-a+rcpc` 与 `-mcpu=native` 同时出现时，GCC/Clang 都会以窄基线重置特性集、丢弃 SVE（`__ARM_FEATURE_SVE` 消失）。正确做法是一条标志同时表达两者：GCC 用 `-mcpu=native`（特性集以目标平台实测为准）；Clang 显式 `-march=armv8.3-a+rcpc+sve`。SVE 实验均经 objdump 确认二进制同时含 `whilelt`/`ld1d`/`brkb` 与 `ldapr`。教训：任何 SIMD 落地实验先 objdump 核对指令真实存在，且勿为开 SIMD 牺牲既有内存序优化。

## 三、有效或待验证的计算杠杆

### 3.1 NEON 桶内 / group 比较

**swiss：已落地，有效性有端到端数据支撑**。同 shard-affinity 路径（无锁、无 SeqLock、无 ring prefetch，100M / int64 / packed / murmur）下 swiss 比 phmap 快（SWISS 报告 §9.4）：

| 线程 | swiss/phmap（trimmed QPS） | swiss/phmap（mean QPS） |
|---|---|---|
| 1 | 1.78× | 1.95× |
| 8 | 2.13× | 2.43× |
| 16 | 2.23× | 2.26× |

差距归因（SWISS 报告 §10.4）：

| 差异来源 | 估计贡献 |
|---|---|
| **NEON vs SWAR group probing** | **~15-20%** |
| shard_count == thread_count 精确匹配 | ~10-15% |
| absl vs phmap 引擎实现差异 | ~5-10% |
| 内联 vs 间接调用 | ~0-5% |

NEON 项机制：absl `GroupAArch64Impl` 用 `vceq_u8` 一次比较 8 字节 metadata；phmap `GroupPortableImpl` 用 SWAR `(x - lsbs) & ~x & msbs` 标量位运算；两者 group width 均为 8（SWISS 报告 §10.3）。**适用条件**：metadata group 连续布局（swiss ctrl 数组）；比较宽度恰好匹配 group 大小（8B group = 一条 NEON 指令宽度）。

**cuckoo：部分落地，待扩展**。`try_read_from_bucket` 仅 `int64_t` + 4-slot 特化已落地（`vceqq_s64` 并行比较，2 条 CEQ；ARM-LEVERS §一）。**LEV-1：`uint64_t` / `int32_t` / `uint32_t` / `PackedEntry`(8B) 的 NEON 快路径缺失**，全部走标量逐槽回退：

- `uint64_t`：`vreinterpretq_s64_u64` 后直接复用现有 CEQ 流水；`int32_t`/`uint32_t`：一条 `vld1q_s32` + 一条 `vceqq_s32` 比完 4 槽；`PackedEntry`（{uint32,uint32}）8B 可按 int64 位比较（ARM-LEVERS P0-1）。
- `uint64_t` 是 embedding 查询主力类型之一，覆盖面广；风险低（类型特化局部改动，不改桶布局与锁协议）。
- ARM-LEVERS P0-1 预期 32-bit key 提速 30~50%——**预期值，未实测**。
- **前置警示**（LEV-1 原文）：先确认第二节 −8%~−13% 回退结论是否针对 uint64 复测——既有反证仅基于 int64。微基准陷阱在此完全适用，端到端收益必须真实负载 A/B 验证。

### 3.2 cache-line 对齐桶（基础项，已落地）

64 字节对齐 + padding 填充至 cache line 整数倍（`libcuckoo_bucket_container.hh`，ARM-LEVERS §一）。消除桶跨行分裂，是一切访存/比较优化的地基；无独立收益数据（与预取、SoA 耦合）。

### 3.3 编译层：-march 启用 CRC32 等

`-march` 含 `+crc` 后 `CrcHash` 走 `__crc32cd` 硬件指令（需 `__ARM_FEATURE_CRC32`；ARM-LEVERS §一）。与 RCpc/LDAPR 同属编译标志族，两者可共存于一条 `-march`（见 2.5）；完整编译标志杠杆见 latency-levers.md 第 10 条。

### 3.4 分支预测友好模式：保持现状即是最优

命中即早退 + 桶间高可预测分支（93.3% 首桶）本身就是最优态——**不要"优化"掉**（SUMMARY 第 8 章总结论）。任何把"平均比 ~2 槽"变成"必比 N 槽"的改造方向均已被证伪（见 2.1/2.2）。

## 四、何时计算杠杆才值得尝试

判定清单（**全部满足**才动手，缺一即放弃）：

| # | 条件 | 类型 | 依据 |
|---|---|---|---|
| 1 | top-down 显示 Core Bound / Frontend / Bad Speculation 主导（非 Backend Memory） | 确实计算受限 | cuckoo 全量优化后 Core Bound 仅 8~9.1%、L3 Bound 54~63%（SUMMARY 第 9 章） |
| 2 | 或工作集 fits in LLC（无冷 miss） | 确实计算受限 | 微基准 L1-hot 陷阱的反面条件 |
| 3 | 或 Retiring 低但停顿不在 Memory（计算依赖链主导） | 确实计算受限 | top-down 定位 |
| 4 | 微基准收益倍数 > 2× | 收益能活下来 | 1.6×/2.0× 微基准收益在真实负载被访存噪声淹没且反转为 −8%/−13% |
| 5 | 准备好真实负载 A/B 复验与回滚 | 收益能活下来 | 第一节判别规则 + SUMMARY 第 10 章协议 |

条件 1/2/3 是"确实是计算受限"的证据（满足其一即可），条件 4/5 是"收益能活下来"的证据（必须全备）。swiss NEON group probing 是唯一同时满足而成功的案例：metadata 连续、比较宽度匹配、且它替换的是本来就短的 SWAR 链（净缩短依赖链），而非在长链上追加指令——与被证伪的 SVE 路径（`ld1d→cmpeq→and→ptest→brkb→cntp` 长链）形成机制对照。

## 五、验证方式

1. **微基准**：仅用于筛选与机制验证，结论一律标记"未证实"，不得据此合入。
2. **真实负载 A/B**：编译期开关隔离，A/B 两版仅差开关；多轮交错取中位（共享机协议），24T 为主判据、1T 仅参考；先过功能用例（`test_cuckoo_direct` / `test_adapter_contract` / `test_adapter_differential` soak）再测性能（SUMMARY 第 10 章）。
3. **objdump 核对指令落地**：确认 SIMD 指令真实存在于二进制（如 SVE 实验 `whilelt`/`ld1d`/`brkb` 与 RCPC `ldapr` 共存核对，SIMD 报告 §工具链备注）——排除 2.5 类编译标志陷阱。
4. **无收益即回滚记反证**：不留半成品在主线，负结果写入反证表（SUMMARY 第 8 章）供后人避坑。

---

**来源缩写**（均位于 `docs/cuckoo/` 与 `docs/swiss/`）：SUMMARY = CUCKOO_OPTIMIZATION_SUMMARY.md；SIMD 报告 = CUCKOO_BUCKET_COMPARE_SIMD_REPORT.md；ARM-LEVERS = CUCKOO_ARM_OPTIMIZATION_LEVERS.md；SWISS 报告 = SWISS_PHMAP_BENCHMARK_REPORT.md；LEV = CUCKOO_OPEN_LEVERS_20260626.md。
