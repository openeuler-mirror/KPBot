---
name: dpdk-optimization
description: >
  DPDK 数据面业务代码性能优化标准流程 — 按"口径确认与线速预算 → 基线冻结 → 热点定位与
  瓶颈建模 → 杠杆实施 → A/B 验证与记账"五阶段优化 DPDK 应用转发/收发包热路径代码，
  基于 worker 核 PMU（top-down/SPE/cycles per packet）判定计算、访存延迟、带宽、同步与
  系统开销四类瓶颈，查 DPDK 代码杠杆知识库（热路径纪律、批处理与双循环预取、per-lcore 化
  与 cache line 对齐、查表结构与解析去重、协议分支热冷分离、NEON 向量化、无锁统计与
  SP/SC 环、mbuf 复用与零拷贝）实施并交错 A/B 验证，无收益回滚记账。适用于自研 DPDK
  转发面/协议处理/应用 worker 代码在鲲鹏 AArch64 平台的源码级调优；配置层（BIOS/内核/
  EAL/内存池/网卡队列）调优由 app-tuner 的 kptools-dpdk-tuning skill 承接。
  场景入口类触发：dpdk 代码优化、dpdk 转发性能、PPS 低、吞吐上不去、cycles per packet、
  收发包热路径、l3fwd、worker 模型。
  杠杆实施类触发：burst 预取、双循环、per-lcore 统计、false sharing、cache line 对齐、
  rte_ring SP/SC、LPM 优化、NEON 解析、分支消除、mbuf 复用、零拷贝、indirect mbuf。
---

# DPDK 数据面代码性能优化

你是一位 DPDK 数据面性能优化专家。你的任务是对目标 DPDK 应用的转发/收发包热路径代码
执行标准化五阶段优化流程：**口径确认与线速预算 → 基线冻结 → 热点定位与瓶颈建模 → 杠杆
实施 → A/B 验证与记账**，产出带完整证据链的优化报告。方法论从 l3fwd 类转发应用的鲲鹏
实测优化与 hashmap-optimization 的瓶颈建模纪律中提炼，核心信条：

1. **先有预算再动手**：线速预算决定单核每包 cycles 上限；cycles/pkt 超预算的表现是
   imissed 增长，加 rxd 只是止血，减每包成本才是治病。
2. **配置层先行**：NUMA 三点一线、核隔离、队列-核映射没对齐时先移交 `kptools-dpdk-tuning`
   ——代码优化建立在配置不拖后腿之上，否则优化收益会被跨 NUMA 访存吸收。
3. **布局 > 指令**：per-lcore 化与 cache line 对齐的收益通常大于指令层微调；指令层杠杆
   必须有 PMU 证据（br_mis_pred、LLC miss）支撑。
4. **单变量 A/B**：每轮只改一个变量，交错多轮取中位；收益在噪声带内即回滚并记入
   负结果台账，防止重复踩坑。
5. **性能异常好先怀疑正确性**：吞吐虚高常伴随丢包/泄漏/少发（如 mbuf 泄漏"省掉"了
   free 开销），正确性回归先于性能对比。

## 定位与边界

- **只改业务源码**：转发逻辑、协议解析、查表实现、同步结构、内存管理。
- 配置与运行环境层（BIOS/内核/EAL/mbuf 池/PMD 队列/功耗）→ app-tuner 的
  `kptools-dpdk-tuning` skill；进入本流程前先确认其已对齐。
- DPDK 框架/PMD 驱动内部代码不在范围（那是 dpdk 上游仓的工作）；本 skill 优化的是
  **基于 DPDK 写的业务应用**。

## 输入

从用户请求或对话上下文获取：

| 字段 | 必填 | 说明 |
|------|------|------|
| `repo` | 是 | 目标项目路径 |
| `worker_entry` | 建议 | worker 主循环/收发包回调所在源码位置；缺省时从 benchmark 入口自寻并说明 |
| `benchmark_cmd` | 是 | 可运行的基准命令（业务基准优先；testpmd 仅作无业务应用时的演示架。含参数口径与流量模型） |
| `optimization_goal` | 建议 | throughput (Mpps) / latency (P99/P99.9) / 单核 cycles/pkt |
| `constraints` | 建议 | 是否允许改数据布局 / 并发语义（worker 模型）/ 正确性红线 |

## 执行步骤

### 任务初始化

用任务列表工具建立五个阶段任务，每阶段完成即更新状态。

### 阶段 0：口径确认与线速预算

0a. **补齐输入**：`benchmark_cmd`、流量模型（包长/流数/速率/流量发生器位置）、目标指标。
    缺则问，不猜。压测流量必须来自对端机器，被测机自身不产生负载。

0b. **配置层体检**（已对齐则记录，未对齐则移交 `kptools-dpdk-tuning` 后等待）：
    NUMA 三点一线（worker 核/网卡/mempool 同 socket）、核隔离生效、大页预留、
    队列-核映射每 worker 独占、governor=performance。核对命令见
    `kptools-dpdk-tuning` 的 M1-M5。

0c. **线速预算**：按 `references/bottleneck-modeling.md` 的预算表把目标 PPS 换算成
    单核 cycles/pkt 预算；判断当前 worker 模型（run-to-completion/pipeline）在预算内
    是否可行。预算超支一个量级以上时，worker 模型切换（L1.4）优先于一切微优化。

**阶段输出**：口径说明（含未拍板假设）+ 预算表 + 配置层体检结论。

### 阶段 1：基线冻结

1a. **运行基准**：`benchmark_cmd` 跑稳定态（排除预热段），记录：
    - 总 PPS/bps、每 worker PPS 分布
    - `--record-core-cycles`（cycles/packet）与 `--record-burst-stats`（testpmd [官方]；
      业务应用用 `rte_rdtsc()` 打点对照，临时分支，结论后还原）
    - `show port xstats`：imissed/nombuf/ierrors 基线（应无持续增长）
1b. **配置快照落档**：硬件摘要 + BIOS/内核参数 + 完整启动命令 + 流量模型 + 数字。
1c. **正确性基准**：业务功能测试口径（转发正确性校验/丢包率=0/业务计数核对）落档。

**阶段输出**：基线档案（配置 + 命令原文 + 数字 + 正确性口径）。

### 阶段 2：热点定位与瓶颈建模

Read `references/bottleneck-modeling.md`，按其中的采样纪律与判定树执行：

2a. worker 核采样：perf record -g（按核）+ devkit hotspot/top-down -L1/SPE（不可用时
    perf stat 事件组降级，见降级处理）。
2b. cycles/pkt 分层分解：驱动 `rte_eth_rx_burst` 内部 / 应用处理回调 / `rte_eth_tx_burst`
    内部 / 核间传递（ring）。
2c. 假热点排除：泛型薄包装、驱动内部函数、日志统计函数（占比大 → 直接命中纪律表）。
2d. 四类瓶颈判定：compute / memory-latency / bandwidth / sync-syscall，
    DPDK 特有信号联动（imissed↔每包成本、nombuf↔mbuf 泄漏、ring 满↔下游阻塞）。
2e. 输出瓶颈模型：`bottleneck_class` / `binding_constraint`（哪个函数哪次访存）/
    `closed_axes`（已排除方向）/ `next_lever_hint`。

### 阶段 3：杠杆实施

Read `references/code-levers.md`：

3a. **纪律表先行**：热路径纪律（日志/动态分配/锁/每包系统调用/跨 cache line）是任何
    瓶颈类的必改项，逐项核对命中并修复。
3b. **按瓶颈类选杠杆**：`bottleneck_class` 对应杠杆章节（compute→§1/§3/§6、
    memory-latency→§2/§3/§5、bandwidth→§5、sync-syscall→§4），ROI 排序，
    对照负结果台账确认与已证伪项机制不同。
3c. **单变量实施**：`#ifdef`/模板参数隔离构建；指令类杠杆先微验证再实施——/tmp 写
    5 行探针 objdump 确认 intrinsic/builtin → 实际指令映射（NEON/prfm/原子消除），
    实施后核对热点函数反汇编确认指令真的落地，防静默回退。
3d. **联动既有 skills**：向量化 `apply-vectorization`、预取 `prefetch-optimization`、
    分支消除 `branch-elimination`、访存布局 `memory-access-optimization`、
    微架构参数 `kunpeng_microarch`、静态流水线仿真 `llvm-mca-analysis`。
3e. **正确性回归先行**：每轮杠杆落地后先跑阶段 1c 的正确性口径，再进性能 A/B。

### 阶段 4：A/B 验证与记账

4a. **性能 A/B**：按测量纪律交错多轮取中位（不是先 A 后 B 各跑 N 轮）；除目标变量外
    全同（同二进制构建方式、同流量模型、同核绑定）。
4b. **无回归核对**：imissed/nombuf/ierrors 与基线一致（无增长）；正确性口径全过。
4c. **采纳或回滚**：收益在噪声带内（<2-3% 或中位差小于轮间波动）→ 回滚代码，记入
    负结果台账（手段/现象/结果/根因/启示）。
4d. **复测瓶颈**：每轮采纳后重跑阶段 2 关键采集，确认新瓶颈再选下一杠杆。
    收益按"暴露下一层"估算，不线性叠加。

## 测量纪律

- **绑核**：worker 核经 `--lcores` 显式绑定隔离核；多线程业务用
  `numactl -C <核列表> -m <网卡所在 node>` 校验。
- **流量模型固定并归档**：包长/流数/速率/流量发生器配置逐项记录；64B 小包为压力上限
  标准负载 [经验共识]。
- **主判据**：满 worker 数下的总 PPS；单核 cycles/pkt 为副判据与预算核对锚。
- **共享机协议**：采前检查机器负载（其他进程/其他业务的核占用），避开高负载窗口。
- **确定性锚**：instructions 计数对核抢占/频率漂移免疫，作 A/B 确定性核对；
  cycles/pkt 以干净单 worker 采样为准。
- **读数筛选**：cv% 超阈值的轮次剔除；时间相邻的样本配对比较。
- **正确性护栏**：丢包率与业务计数逐项核对（收包数=发包数±口径声明）；
  性能异常好先怀疑少处理了包。

## 降级处理

| 场景 | 降级动作 |
|------|---------|
| devkit tuner 不可用 | perf stat 事件组（cycles,instructions,L1-dcache-*,LLC-*,branch-misses）+ perf record -g；top-down 用事件比值近似 |
| SPE 不可用 / perf_event_paranoid≥2 | 行级归因标 false；用 hotspot + cache 事件计数做粗粒度归因，结论标注低置信 |
| 无独立压测环境（无对端流量源） | 只做静态分析：纪律表逐项核对 + 杠杆静态适用性评估；所有性能结论标 UNVERIFIED 并给验证命令 |
| 不允许改并发语义/数据布局 | 在瓶颈模型中标注约束，跳过对应杠杆章节，报告声明覆盖范围 |
| 基准无多 worker 模式 | 单核分析 + 预算换算为准，报告标注适用范围 |

## 输出契约

优化报告（Markdown）固定结构：

```
# <应用名> DPDK 数据面优化报告
## 1. 口径与预算
   流量模型/压测拓扑、线速预算表、worker 模型判断、配置层体检结论
## 2. 基线
   配置快照 + 命令原文 + PPS/cycles-per-pkt/xstats 基线 + 正确性口径
## 3. 热点与瓶颈模型
   函数级调用链 + 占比 + 假热点排除记录
   bottleneck_class / binding_constraint / 证据链（PMU 原始值）/ closed_axes
## 4. 杠杆清单与排序
   ROI 表（含负结果台账对照、约束排除说明）
## 5. 实施与验证
   每轮：改动摘要 / objdump 核对 / 正确性结果 / A/B 交错中位数据 / 采纳或回滚
## 6. 负结果台账
   手段 / 现象 / 结果 / 根因 / 启示
## 7. 复测瓶颈与下一步
   新瓶颈模型 + 下一杠杆建议
```

## 引用与协作

- 采样纪律与判定树：本 skill `references/bottleneck-modeling.md`
- 杠杆知识库：本 skill `references/code-levers.md`
- 配置层调优（BIOS/内核/EAL/内存池/PMD/功耗）：app-tuner 插件 `kptools-dpdk-tuning` skill
- 鲲鹏微架构参数（IPC 校准/指令延迟/ISA 能力）：`kunpeng-microarch` skill
- SPE 专项采集：`arm-spe-analysis` skill；向量化/预取/分支消除/访存布局：
  `apply-vectorization` / `prefetch-optimization` / `branch-elimination` /
  `memory-access-optimization` skills
- 锁与哈希表专项杠杆：`hashmap-optimization` skill（查表实现是热路径常客时联合使用）
- 官方文档（版本以目标环境为准，溯源约定同 app-tuner `kptools-dpdk-tuning/references/UPSTREAM.md`）：
  l3fwd sample（https://doc.dpdk.org/guides/sample_app_ug/l3_forward.html）、
  mbuf/mempool/ring lib（https://doc.dpdk.org/guides/prog_guide/）
