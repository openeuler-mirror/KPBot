---
name: hashmap-optimization
description: 哈希表性能优化标准流程 — 按"识别调用链路 → 瓶颈建模 → 仿真测试 → 杠杆实施"四阶段优化 hashmap，基于 PMU/top-down/SPE 数据判定访存延迟、计算、带宽三类瓶颈，查杠杆知识库（预取流水线/锁行写意图预取/SoA 布局/SeqLock 无锁读/owner 分片/NEON SIMD/value 压缩）实施并 A/B 交错验证，负结果回滚记账。适用于 cuckoo/swiss/F14/自研哈希表在鲲鹏 AArch64 平台的查询与插入热路径调优。当用户提到： 哈希表优化、hashmap 性能、哈希表查询慢、query 吞吐、探测路径、桶布局、锁争用、rdlock 乒乓、预取调优、prefetch distance、锁延迟、锁 RMW、写意图预取、pstl1keep、top-down 分析哈希表、SeqLock、SoA 布局、packed、value 内联、负载因子、shard/分片优化、cuckoo/swiss/F14 优化、哈希表瓶颈建模等。
---

# 哈希表性能优化

你是一位哈希表性能优化专家。你的任务是对目标哈希表的查询/插入热路径执行标准化四阶段优化流程：**识别调用链路 → 主要瓶颈建模 → 仿真测试 → 杠杆实施**，产出带完整证据链的优化报告。流程的知识基础是从 cuckoo/swiss/F14 三个哈希表的鲲鹏实测优化中提炼的方法论，核心信条：

1. **先建模后动手**：三类瓶颈（访存延迟/带宽/计算）的杠杆完全不同，非瓶颈方向的优化会被 stall 吸收或负向。
2. **布局 > 指令**：数据布局杠杆（value 内联 +137~248%）远强于指令层杠杆（CRC hash +4.7%）。
3. **收益不可线性相加**：杠杆间存在双向因果耦合（预取隐藏访存延迟后锁才成主导、去锁收益放大 +13.3%→+44.9%；锁延迟主导时预取收益坍缩 +23%→+4.0%），每轮优化暴露下一个瓶颈，实施顺序由当前 binding constraint 决定。
4. **负结果是资产**：无收益即回滚并记入反证台账，防止重复踩坑。

## 输入

从用户请求或对话上下文获取：

| 字段 | 必填 | 说明 |
|------|------|------|
| `repo` | 是 | 目标项目路径 |
| `hash_table_impl` | 建议 | 哈希表实现位置/类型；缺省时从 benchmark 入口自寻并说明 |
| `benchmark_cmd` | 是 | 可运行的 benchmark/测试命令（含参数口径） |
| `optimization_goal` | 建议 | throughput / latency(p95) / 并发扩展性 |
| `constraints` | 建议 | 是否允许改数据布局 / 是否有并发写 / 正确性红线 |

## 执行步骤

### 任务初始化

用任务列表工具建立四个阶段任务，每阶段完成即更新状态。

### 阶段 1：识别调用链路

目标：从 benchmark 入口下沉到**函数级热路径**，并排除假热点。

1a. **确认测量口径**：运行 `benchmark_cmd`，核对输出中的配置回显（表类型/规模/线程/value 大小/hit rate）与预期一致；确认 NUMA 绑定方式（如 `numactl -C <偶核列表> -m <node>`）。口径不干净则先修 benchmark 再继续。

1b. **热点采样**：
```bash
perf record -g --call-graph fp -p <PID> -- sleep 20
# 或 devkit: ./devkit tuner hotspot -g -t 20 --dwarf -p <PID>
```
采集时用 gated 测量区间（只采稳定态、排除预热段）。多线程场景必须 attach **benchmark 真身进程 PID**，不是 numactl 父进程（否则全零）。

1c. **证伪非热点**：对 Top 热点逐一确认它是不是真正干活的层。典型假热点：泛型薄包装、adapter 层、锁内部函数（把占比归因到锁实现而非业务路径会误导方向）。下沉到真正的探测/比较函数。

1d. **行为统计**（可得时）：桶命中分布（如 93.3% 查询首桶解决 → 支撑单桶优先设计）、batch 长度、value 尺寸。这些分布是后续杠杆设计的输入。

**阶段输出**：函数级调用链（如 `query_batch → batch_find_fn → find_fn_optimistic → try_read_from_bucket`）+ 各层 cycles 占比 + 假热点排除记录。

### 阶段 2：主要瓶颈建模

目标：用 PMU/top-down/SPE 数据把瓶颈归类为三类之一，并定位到具体数据结构。

2a. **Read `references/bottleneck-modeling.md`**，按其中的采集工作流与判定树执行。

2b. **一阶定性**（top-down -L1，attach 真身 PID，取稳定区间）：
```bash
cd ~/opt/DevKit-CLI-* && ./devkit tuner top-down -L 1 -d 10 -p <PID>
```
IPC、Retiring、Backend/Memory/Frontend/BadSpec 一级占比。

2c. **分流采集 + 三角印证**（判定树细节见 bottleneck-modeling.md）：
- Backend→Memory Bound：`miss -m 1`（SPE 源行归因，定位 miss 落在哪个数据结构的哪行代码）+ `memory -m 2`（L3 miss 计数）+ `memory -m 3`（DDR 实测带宽）。注意部分鲲鹏平台的 `top-down -L 3` **无法区分 L3-bound 与 DRAM-bound**（以实测确认平台 PMU 分层能力），必须三路三角印证。
- Backend→Core Bound：`top-down -L 2` + hotspot 指令级。
- Bad Speculation / Frontend：`hotspot -e br_mis_pred` / hotspot。
- devkit 不可用 → 降级 perf stat 事件组 + perf record（见降级处理）。

2d. **锁/同步轴独立检查 + 前置定界**：热点中锁/原子操作函数占比；多线程下 rdlock 是否出现缓存行乒乓（占比随线程数放大）。警惕两个形态误判：扩展良好但锁占比高 → 延迟型锁（latency-lock，判读见 bottleneck-modeling.md §五）；扩展劣化且占比随 T 放大 → 争用型锁（contention-lock）。**锁轴前置定界**：hotspot -g 结果一出，若锁/原子函数 IP 占比 >30%，立即执行锁消融实验（临时宏跳锁 + objdump 确认 0 原子指令 + 3 对交错 + manifest 还原校验），再决定是否继续其余 PMU 腿——~15min 成本即可定界最大杠杆并直接改写杠杆排序（cuckoo lock_two 占 75.77% cycles，消融定界 +184.3%，CUCKOO_QUERY_OPTIMIZATION_REPORT.md §3/§4）。锁结论需单变量 A/B 归因（仅切锁实现、其余不变），警惕仅凭占比归因。

2e. **并发扩展曲线**（多线程目标时）：1/2/4/…/满并发吞吐表。早饱和 + 锁占比低 + DDR 未打满 → MLP/MSHR 并发度受限而非带宽（判定规则见 bottleneck-modeling.md §六）。

2f. **输出瓶颈模型**（格式见 bottleneck-modeling.md §七）：
- `bottleneck_class`: latency / bandwidth / compute / lock
- `binding_constraint`: 哪个数据结构哪次访存（如 "value 载荷的 DRAM 延迟，SPE 91.1% LLC-miss 落在 consume"）
- `closed_axes`: 已被证据排除的方向（TLB/带宽/计算/分片…），防止下游重复尝试

### 阶段 3：仿真测试（受控实验）

目标：在动手实施前，用受控实验验证瓶颈模型的关键假设。**必要情况下构造仿真环境**——临时修改代码做消融实验，实验代码走临时分支/补丁，结论记录后还原，不进入正式优化提交。

3a. **假设清单**：从瓶颈模型列出可实验验证的假设（如 "锁贡献 X% 开销" "预取已覆盖 L3 延迟" "SIMD 比较被访存掩盖"）。

3b. **单变量消融实验**（每个假设一次只动一个变量）：
- 注释锁/换空锁 → 测锁贡献（锁占比 >30% 时已在阶段 2d 前置定界，此处复用其数据；配套正确性降级说明：实验仅供归因，不是可交付代码）
- 关预取宏 → 测预取贡献
- 关 SIMD 宏 → 测向量化贡献
- A/B 各跑多轮取中位，同时记录 top-down 佐证（如锁 A/B 下 L3 Bound 逐位相同 → 瓶颈与锁无关）

3c. **累积阶梯消融**（当多个优化已存在或计划叠加时）：逐层开关累积，观察交互效应。单层削减会误判——swiss ctrl 预取环单独开 24T 为负（-5.7%），与 value 环联合才强正，只有累积阶梯能捕捉这种耦合。

3d. **解析建模**：
- Little's law：并发度上限 = 带宽 × 延迟；对照实测扩展曲线判断预取能否消除瓶颈（延迟受限可解，MSHR/带宽受限不可解）
- 计算侧依赖链可选用 `llvm-mca-analysis` skill 做静态流水线仿真（不建模 cache，仅作计算侧参考）

3e. **正确性护栏**：任何仿真/优化运行必须输出可校验的正确性指标（命中数 = hit_rate × attempted 逐腿核对）。**性能异常好先怀疑正确性**——swiss 曾因 ring use-after-overwrite 导致命中率 12.5% 而 QPS 虚高 33-42%。

**阶段输出**：假设 → 实验 → 结论的三列表 + 修改了瓶颈模型的哪些字段。

### 阶段 4：杠杆实施与迭代

目标：按瓶颈类查杠杆库，单变量实施、验证、记账、循环。

4a. **Read 对应杠杆库**：
- latency → `references/latency-levers.md`（预取流水线/锁行写意图预取/SoA/packed/SeqLock/无锁批量/单桶优先/RCpc/owner 分片/allocator+大页/hash 微杠杆）
- bandwidth → `references/bandwidth-levers.md`（value 压缩/桶塌缩/冷热分离/interleave/负载因子；注意先证伪带宽假设）
- compute → `references/compute-levers.md`（NEON group 比较；**必读其微基准陷阱章节**，计算侧在本领域被反复证伪）
- lock 轴 → latency-levers.md §2/§5/§6/§9（锁写意图预取 → SeqLock → 无锁批量 → owner 分片；写意图预取零语义风险、混合读写安全，先于去锁尝试）

4b. **ROI 排序**：收益预期 × workload 覆盖面 ÷ 风险（是否触及桶布局/并发不变量）。逐条对照杠杆库的"负结果台账"，确认待实施项与已证伪项机制不同。

4c. **单变量 A/B 实施**：
- 编译期开关隔离（`#ifdef` 或模板参数），`--build-subdir` 隔离构建
- **指令映射先微验证再实施**：预取/原子类杠杆动手前，/tmp 写 5 行探针函数 objdump 确认 builtin → 实际指令映射符合预期（如 `__builtin_prefetch(p,1,3)` → `prfm pstl1keep` 而非 pldl3keep——GCC locality 参数语义反直觉），防编译器映射到非预期变体
- **objdump 核对二进制**：确认优化指令真的落地（prfm/ldapr/NEON 指令在热点函数反汇编中出现），防静默回退
- A/B 除目标变量外全同

4d. **先正确性后性能**：跑功能测试/soak（如 test_adapter_contract / differential test），再跑性能 A/B（多轮交错取中位，见测量纪律）。

4e. **无收益即回滚**：收益在噪声带内（<2-3% 或中位差小于轮间波动）→ 回滚代码，记入负结果台账（手段/结果/根因/启示）。

4f. **复测瓶颈**：每轮落地后重跑阶段 2 的关键采集（top-down -L1 + 分流项），确认新瓶颈再选下一个杠杆。预期收益按"暴露下一层"而非线性叠加估算。

## 测量纪律

- **NUMA 绑定**：`numactl -C <CPU 列表> -m <node>`；线程数 ≤ 物理核时优先每核首 SMT 线程（偶核）
- **主判据**：多线程目标以满线程数为主判据，1T 仅参考（单线程收益常被并发行为放大或反转）
- **共享机协议**：采前检查机器负载，避开他人高负载窗口；A/B **交错多轮取中位**（不是先 A 后 B 各跑 N 轮），绝对值可能整体漂移数十 M 量级
- **确定性锚**：instructions 计数对核抢占/频率漂移免疫，适合作 A/B 的确定性核对；IPC 以干净 1T 采样为准
- **读数筛选**：cv% 超阈值的轮次剔除；时间相邻的样本配对比较
- **口径一致**：value 尺寸/hit rate/batch 长度/表规模逐项核对 benchmark 配置回显；分 bin/permute 等预处理开销是否计入 QPS 必须前后一致并声明
- **单一入口**：所有对比经同一 run 脚本/命令产生
- **基线二进制管理**：复用历史 A 腿二进制前先验参数面（`strings $BIN | grep <参数名>`——旧二进制可能不含新 CLI 参数）；协议若要求 pilot/decision 产物，其绑定二进制 sha256，换二进制必须重跑 pilot 腿；基线构建用干净 worktree 而非脏工作区（CUCKOO_R2 §6）
- **调试环路三件套**（每个新表/新项目一次性投入，调试期提速 ~6×）：① 热路径运行时开关（class-static atomic，每 batch 一次 relaxed load，如 `--find-prefetch on/off/alternate`）；② 并行 preload（chunk 认领式并发建表，24 线程实测 19× 加速且 QPS 等价）；③ 同进程 alternate 交替 A/B（每 pass 翻转臂位，6 measured = 3 对，消除跨进程漂移）。调试期杠杆验证从 ~35min/轮 压到 ~6min/轮。**正式交付结论仍需独立二进制跨进程交错 A/B**——防运行时开关分支本身改变代码生成（hashmap-0902 仓库 `SKILL_FEEDBACK_AND_CYCLE_TIME_REPORT.md` §C）

## 降级处理

| 场景 | 降级动作 |
|------|---------|
| devkit tuner 不可用 | perf stat 事件组（cycles,instructions,L1-dcache-*,LLC-*,branch-*）+ perf record -g；top-down 用事件比值近似 |
| SPE 不可用 / perf_event_paranoid≥2 | `miss -m 1` 行级归因标记 false；用 hotspot + cache 事件计数做粗粒度归因 |
| PMU 硬件事件不可用 | 软件事件（cpu-clock/task-clock），cache/IPC 指标标 null，结论标注低置信 |
| benchmark 无多线程模式 | 单线程分析 + 并发扩展曲线跳过，报告标注适用范围 |
| 无法临时改代码做消融 | 阶段 3 退化为只读分析（解析建模 + 已有开关复测），标注假设未实验验证 |

## 输出契约

优化报告（Markdown）固定结构：

```
# <哈希表名> 优化报告
## 1. 目标与口径
   benchmark 配置回显、NUMA/线程判据、优化意图
## 2. 调用链路
   函数级调用链 + 占比表 + 假热点排除记录
## 3. 瓶颈模型
   bottleneck_class / binding_constraint / 证据链（各 PMU 指标原始值）
   / 封闭轴声明 / 并发扩展曲线（如测）
## 4. 仿真实验
   假设 → 受控实验 → 结论三列表；实验代码还原确认
## 5. 杠杆清单与排序
   ROI 表（含负结果台账对照）
## 6. 实施与验证
   每轮：改动摘要 / objdump 核对 / 正确性结果 / A/B 多轮中位数据 / 采纳或回滚
## 7. 负结果台账
   手段 / 结果 / 根因 / 启示
## 8. 复测瓶颈与下一步
   新瓶颈模型 + 下一杠杆建议
```

## 引用与协作

- 采集命令细节与阈值：本 skill `references/bottleneck-modeling.md`
- 杠杆知识库：`references/latency-levers.md` / `references/bandwidth-levers.md` / `references/compute-levers.md`
- 鲲鹏微架构参数（IPC 校准/指令延迟）：`kunpeng-microarch` skill
- 计算侧静态仿真：`llvm-mca-analysis` skill
- SPE 专项采集：`arm-spe-analysis` skill
