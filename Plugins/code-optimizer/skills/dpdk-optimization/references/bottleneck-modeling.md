# DPDK 数据面瓶颈建模（worker 核 PMU 方法）

目标：把"PPS 低"翻译成可实施的瓶颈模型。输入是阶段 1 的基线档案，输出是
`bottleneck_class / binding_constraint / closed_axes / next_lever_hint`。

## 采样对象与纪律

- **attach worker 真身**：perf/devkit 目标是 worker 所在物理核（`-C <cpu>`）或 EAL
  worker 线程 PID；不是 main lcore，也不是 numactl/systemd 父进程。
- **gated 稳定区间**：只采稳定态、排除预热段；采前核对机器无其他高负载。
- **采集组合**（有 DevKit 用 DevKit，无则 perf 降级，见 SKILL.md 降级表）：

```bash
# 热点与调用链
perf record -g --call-graph fp -C <worker_cpu> -- sleep 20
# 一阶定性：top-down -L1
devkit tuner top-down -L 1 -d 10 -C <worker_cpu>     # 或 -p <PID>
# 行级访存归因（SPE）
devkit tuner miss -m 1 -C <worker_cpu>
# LLC 与 DRAM 带宽
devkit tuner memory -m 2 -C <worker_cpu>   # LLC miss 计数
devkit tuner memory -m 3 -C <worker_cpu>   # DDR 实测带宽
# 干净单 worker cycles/pkt
testpmd --record-core-cycles → read fwd core cycles stats
```

- 部分鲲鹏平台 `top-down -L 3` 无法区分 L3-bound 与 DRAM-bound（以实测确认平台 PMU
  分层能力），必须 LLC 计数 + DDR 实测带宽三角印证 [经验]。

## cycles/pkt 分层分解

把 worker 的 cycles/pkt 拆成四段，确认钱花在哪：

| 段 | 归因方法 |
|----|---------|
| 驱动收包（`rte_eth_rx_burst` 内部） | perf annotate 函数内占比；驱动内部占比高 → vector 路径/offload 问题（配置层，移交 kptools-dpdk-tuning M5），不是本 skill 范围 |
| 应用处理回调 | 业务函数占比 → 本 skill 主战场 |
| 驱动发送（`rte_eth_tx_burst` 内部） | 同收包段；tx 重试/清理占比高 → txd/txfreet（配置层）或发送侧代码 |
| 核间传递（ring/IPC） | `rte_ring_*` 函数占比；ring 满忙等 → 下游阻塞 |

应用内打点：入口/出口 `rte_rdtsc()` 差值统计（临时分支/宏，结论记录后还原，
不进正式提交）。

## 假热点排除

对 Top 热点逐一确认它是不是业务路径：

- 泛型薄包装、adapter 层（把占比归因到底层真函数）。
- 驱动内部函数（`hns3_*`、`*_recv_pkts` 等）→ 归因到配置层问题，非业务代码。
- 日志/统计函数（`rte_log*`、`printf`、共享计数器自增）占比大 → **直接命中纪律表
  （code-levers §0）**，不用继续建模，先修纪律项再复测。

## 线速预算表（工程常数，可用公式复核）

pps = 线速 bps / ((64+20)×8)，20B = 前导+SFD+帧间隙 [经验/工程常数]：

| 链路 | 64B 线速 PPS | 每包时间 | 单核 cycles 预算 @3.0GHz |
|------|-------------|---------|--------------------------|
| 10GE | 14.88 Mpps | 67.2 ns | ~201 |
| 25GE | 37.20 Mpps | 26.9 ns | ~81 |
| 100GE | 148.8 Mpps | 6.72 ns | ~20 |

- 判据 [经验]：单核预算 < 100 cycles 时 run-to-completion 基本不可行（一轮
  rx_burst+业务+tx_burst 的固有开销已超预算），pipeline/多核分流是前提条件，
  微优化无法弥补模型差距。
- 预算换算按目标环境实际频率重算（`lscpu` 实测主频），表中数值仅为示例口径。

## 四类瓶颈判定树

**一阶定性**（top-down -L1，稳定区间）：

| 一阶结果 | 走向 |
|---------|------|
| Retiring 高 + PPS 未达预算 | **compute 类**：指令数多（逐包重函数、未向量化、重复解析）→ code-levers §1/§3/§6 |
| Backend Bound | 分流 ↓ |
| Bad Speculation 高 | 分支误预测（协议类型分支/错误路径内联）→ code-levers §3.4 + `branch-elimination`；先核对 `br_mis_pred` 证据 |
| Frontend Bound | I-cache 压力/热路径过长（少见）→ 热冷分离 + 代码布局 |

**Backend Bound 分流**（三角印证）：

- **memory-latency 类**：SPE `miss -m 1` 行级归因（miss 落在哪个数据结构哪行：
  查表 entry / mbuf data / per-lcore 元数据 / 跨核共享行）+ LLC miss 高 +
  **DDR 带宽不满** → code-levers §2/§3/§5
- **bandwidth 类**：DDR 实测带宽打满 → 先证伪（多数"带宽不足"实为延迟或冗余拷贝）：
  排查每包 memcpy/跨核搬运/重复解析导致的额外流量 → code-levers §5
- **Core Bound**：指令级 hotspot + `llvm-mca-analysis` 静态仿真（不建模 cache，
  仅作计算侧参考）→ code-levers §6

**sync-syscall 轴独立检查**（与上述并行做）：

- 热点中 futex/spinlock/原子 RMW（`lock` 前缀 / `ldaxr|stlxr` / `__atomic_*`）函数
  占比；多 worker 下统计聚合点是否 false sharing：`perf c2c record` 或 SPE 归因到
  同一 cache line 的多核写。
- 抽样确认热路径无系统调用（`strace -c -p <worker_pid>`；`clock_gettime` 高频也是
  命中——vDSO 调用有成本）。
- ring 满/空忙等：ring 满说明下游慢，转 bandwidth/pipeline 问题；ring 空 + worker
  高 CPU 是上游问题。

**DPDK 特有信号联动**：

| 信号 | 含义 | 代码层动作 |
|------|------|-----------|
| imissed 持续增长 + cycles/pkt 超预算 | worker 每包成本过高，取包不及时 | 减每包成本（本 skill），加 rxd 只缓解 |
| imissed 增长 + cycles/pkt 在预算内 | 描述符/burst 配置问题 | 配置层（kptools-dpdk-tuning M5） |
| nombuf 增长 | mbuf 池耗尽或泄漏 | 先审计 tx 路径是否漏 free（正确性 bug 先修），再查池配置 |
| xstats 无异常 + PPS 低 | 纯计算/访存/同步瓶颈 | 本 skill 主流程 |

## 输出瓶颈模型（格式）

```text
bottleneck_class: compute | memory-latency | bandwidth | sync-syscall | mixed
binding_constraint: <哪个函数哪次访存/哪类指令，附证据（PMU 原始值 + 行级归因）>
closed_axes: <已被证据排除的方向（如：跨 NUMA 已排除——三点一线核对通过）>
next_lever_hint: <对应 code-levers 章节>
```
