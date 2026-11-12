---
name: kptools-dpdk-tuning
description: >
  DPDK 应用性能调优 Skill：双模式工作流。诊断模式 — 现象驱动（PPS 低于预期、时延高、丢包、
  CPU 打满但吞吐不涨、EAL 启动内存报错），按排查树自底向上定位瓶颈；顾问模式 — 硬件+场景驱动，
  输出分层推荐配置（BIOS / 内核启动参数 / EAL / mbuf 内存池 / PMD 网卡 / 转发应用配置 /
  功耗低时延），不修改业务源码。内容以 DPDK 官方文档（doc.dpdk.org guides，版本溯源见
  references/UPSTREAM.md）为准；目标工作区若为 dpdk 源码仓，其 doc/guides/ 为版本精确来源，
  优先引用。面向鲲鹏/ARM 平台（Kunpeng 920 + hns3/Hi1822 为主场景）。
  触发关键词：dpdk调优, dpdk性能, 性能调优, perf tuning, PPS低, 吞吐低, 时延高, 丢包,
  testpmd基准, EAL参数, 核隔离, isolcpus, hugepages, 大页, NUMA, RSS, 低时延, hns3, 鲲鹏920, mlx5, connectx, mprq, dv_flow
---

# DPDK 应用性能调优 Skill

## 核心理念 ⚠️ 必须遵循

> **1. 先测后调**：任何修改前必须有可复现的基线数据（testpmd / 业务指标），
> 每次只改一个变量，改完复测。没有基线的"调优"是瞎猜。
>
> **2. 分层归因**：性能问题自底向上排查——硬件/BIOS → 内核/OS → EAL →
> 内存/mbuf → PMD/网卡 → 转发应用配置。上层症状往往是下层根因。
>
> **3. 吞吐与时延是不同目标**：吞吐调优（批处理、描述符加大、vector 路径）与
> 时延调优（避免休眠、减少队列切换）经常互相冲突，先问清目标再动手。
>
> **4. 官方文档为准**：给出参数值/阈值/结论时必须能落到出处——优先目标工作区
> dpdk 源码仓的 `doc/guides/`（版本精确），否则 doc.dpdk.org guides（溯源见
> `references/UPSTREAM.md`）。**禁止编造数值**；不确定就标注 UNVERIFIED 并给出验证方法。

## 概述

本 Skill 面向 DPDK 应用（含部署在鲲鹏/ARM 服务器上的转发面、vhost/OVS、多进程业务）的
性能调优，**不修改业务源码**。双模式：

- **诊断**（现象 → 排查树）：输入是症状（PPS 低/时延高/丢包/CPU 满）。
- **顾问**（硬件+场景 → 分层配置建议）：输入是硬件与场景描述（如"2P 128C，2x100G hns3，
  L2 转发，目标 148Mpps"）。

鲲鹏主场景：Kunpeng 920（aarch64，HiSilicon hns3 类 PMD / Hi1822 网卡），
涉及 ARM 时参考
[DPDK ARM64 交叉编译指南](https://doc.dpdk.org/guides/linux_gsg/cross_build_dpdk_for_arm64.html)
与 [hns3 PMD 文档](https://doc.dpdk.org/guides/nics/hns3.html)。

### 与其他 skill 的分界

- 本 Skill 只做**配置与运行环境层**调优（BIOS/内核/EAL/内存池/PMD/应用启动参数）。
- 需要修改 DPDK 业务代码（转发逻辑、热路径函数）的优化由 **code-optimizer 插件的
  `dpdk-optimization` skill** 承接；诊断定位到代码层时移交给它。
- 通用服务器性能采集（CPU/内存/IO/网络）由 `kptools-nmon`、`kptools-proc-collector`、
  `kptools-network-collector` 等覆盖；本 Skill 只在需要 DPDK 特有观测
  （xstats、telemetry、testpmd 基准）时介入。
- **与通用优化 subskill（os-optimization / network-optimization /
  cpu-affinity-optimization）的分界**：目标应用是 DPDK 轮询模型时，收发包不经过
  内核网络栈，network-optimization 的内核态手段（ethtool 队列/RPS/XPS、softirq/NAPI
  分析、TCP 栈调优）对 DPDK worker 基本不适用，应优先走本 Skill 的 M1-M10 路径；
  cpu-affinity-optimization 的运行时 taskset 绑线程也不适用于 EAL `--lcores` 静态
  映射模型。governor/大页预留/THP/irqbalance 等底层项两边结论一致，可互为校验，
  但 DPDK 侧有其特有下半段（1G 页 pagesize 挂载、`--huge-dir`、核隔离三件套与
  worker 绑定核对）。目标应用是普通内核栈网络服务（Nginx/数据库/Java 等）时，
  才走通用 subskill。

## 模式一 · 诊断模式（现象驱动）

输入是症状（PPS 低/时延高/丢包/CPU 满）。执行：

1. 按 `references/workflow-diagnose.md` 的「事实收集清单」采集硬件拓扑、内核参数、
   EAL 参数、运行时统计（全部有具体命令）。
2. 沿排查树自底向上定位，每个分支给出验证命令与判据。
3. 输出：瓶颈结论 + 证据链 + 修复建议（改动最小化，逐项可回退）。

## 模式二 · 顾问模式（配置建议）

输入是硬件+场景描述。执行：

1. 按 `references/workflow-advise.md` 先补齐缺失的关键输入（NUMA 拓扑、目标指标）。
2. 输出分层推荐配置表：每项含推荐值、理由、官方出处、验证方法。
3. 按性价比排序给出调优顺序，并强制要求实施前先跑基线。

## 模块路由表

| 模块 | 参考文件 | 典型触发 |
|------|----------|----------|
| M1+M2 硬件/BIOS/内核/OS | `references/platform-os.md` | BIOS、isolcpus、hugepages、中断绑核、governor |
| M3 EAL 参数 | `references/eal-args.md` | -l/--lcores、--iova-mode、大页参数、--numa-mem |
| M4 内存/mbuf | `references/memory-mbuf.md` | mempool、跨 NUMA、mbuf 大小、cache |
| M5 PMD/网卡 | `references/pmd-nic.md` | 队列数、描述符、burst、vector、offload、RSS、hns3/Hi1822、mlx5/connectx |
| M6 转发应用配置 | `references/forwarding-logic.md` | worker 模型、burst、热路径纪律检查、查表配置 |
| M7 功耗/低时延 | `references/power-low-latency.md` | RTE_POWER、monitor/pause、PM QoS、uncore |
| M8 观测/基准 | `references/observability-benchmark.md` | testpmd 方法、telemetry、procinfo、perf |
| M9 场景化 | `references/scenarios.md` | vhost/OVS/多进程/Kunpeng ARM/容器 |
| M10 反模式 | `references/antipatterns-checklist.md` | 现象→常见坑速查表 |

## Guidelines

- **禁止盲改**：每个建议附带验证命令；无法验证的建议显式标注。
- **禁止编造数字**：PPS 基线、描述符"最优值"等随硬件/驱动变化，一律给测量方法而非绝对值。
- **版本核对**：EAL 参数与 testpmd 默认值随版本变化（如 `--socket-mem`→`--numa-mem`），
  给建议前先确认目标环境 DPDK 版本（`dpdk-testpmd --version` 或 EAL 启动日志）。
- **本地文档优先**：目标工作区是 dpdk 源码仓时，涉及具体 PMD（hns3/i40e/mlx5/virtio 等）
  行为先查其 `doc/guides/nics/<driver>.rst`；非源码仓场景用 doc.dpdk.org 对应页面。
- **区分事实与经验**：官方文档结论标 [官方]；工程经验标 [经验]，两者不得混淆陈述。
- 诊断定位到**业务代码层**（M6 纪律表逐项命中且需改代码修复）时，移交
  code-optimizer 插件的 `dpdk-optimization` skill，本 Skill 不给出改码方案。

## 参考资料

- 诊断/顾问工作流：`references/workflow-diagnose.md` / `references/workflow-advise.md`
- 官方文档溯源与版本差异：`references/UPSTREAM.md`
