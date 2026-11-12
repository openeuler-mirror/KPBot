# M3 EAL 初始化参数调优

来源：[官方] dpdk `doc/guides/linux_gsg/linux_eal_parameters.rst`
（https://doc.dpdk.org/guides/linux_gsg/linux_eal_parameters.html，26.11.0-rc2 实测核对，
版本溯源见 UPSTREAM.md）。

## ⚠️ 版本注意

- **`--socket-mem`/`--socket-limit` 已更名为 `--numa-mem`/`--numa-limit`**（26.11 官方文档
  仅列新名）。旧版本请以目标环境对应版本的 `linux_eal_parameters` 文档为准。
- 一次只能用 `--lcores`、`-l`、`-c` 中的一个 [官方]。

## 核映射（调优重点）

| 参数 | 说明 |
|------|------|
| `-l <c1[-c2],c3>` | 核列表，lcore id = 物理 CPU id |
| `--lcores <lcores[@cpus]>` | lcore id 与物理核分离映射 |
| `--main-lcore <id>` | 指定 main lcore |
| `-S <list>` | service core 列表 |
| `-R [start]` | 自动重映射 lcore id 为连续段（与 --lcores 的 @/() 语法互斥）[官方] |

官方 `--lcores` 语义示例：
- `--lcores=1@31,2@32,3@33`：lcore 1/2/3 分别独占绑定到物理核 31/32/33（调优推荐形态）。
- `--lcores=1@(1,2)`：单个线程可在两个物理核漂移——**OS 决定位置，破坏 NUMA 亲和**。
- `--lcores='(1-3)@20'`：三个 lcore 共用一个物理核。
- **官方警告：多个 lcore 绑同一物理核会导致 ring/mempool/spinlock 的性能劣化甚至死锁，
  必须非常谨慎。**

要点：
- main lcore 放 housekeeping 核（不参与收包转发），worker 核全部走隔离核 [经验+官方示例风格]。
- worker lcore 与 RX 队列、网卡、mempool 同 socket（M1/M4 联动）。

## 内存参数

| 参数 | 说明 |
|------|------|
| `-n <channels>` | 内存通道数，应等于实际插槽数 [官方语义；经验：设错影响带宽] |
| `--numa-mem 1024,2048` | 每 NUMA 节点预分配（旧名 --socket-mem） |
| `--numa-limit 1024,0` | 每节点上限，0 表示该节点不限制 |
| `--legacy-mem` | 旧内存模式（多进程老用法；新场景不建议） |
| `--huge-dir <path>` | 指定 hugetlbfs 挂载点 |
| `--huge-unlink[=existing\|always\|never]` | 默认 existing；always 不留文件且不支持多进程；never 复用文件加速重启（有数据残留安全问题）[官方] |
| `--in-memory` | 不建共享结构，免挂载大页；隐含 --no-shconf（不支持多进程）[官方] |
| `--single-file-segments` | 减少 hugetlbfs 文件数 |
| `--pagesz-mem 2M:32G` | 按页大小设内存限额 [官方，26.11 新语法] |
| `--huge-worker-stack[=KB]` | worker 线程栈也从大页分配 |
| `--iova-mode <pa\|va>` | 强制 IOVA 模式（见下） |

### --iova-mode 怎么选
- 默认（未指定）：有 IOMMU 时倾向 va，否则 pa [官方语义；具体回退逻辑见对应版本文档]。
- `va`：免物理地址映射开销，vfio 场景通常配合 `iommu=pt`；部分外设/驱动要求 pa。
- **经验**：换 iova 模式前先查所用 PMD 文档（如
  [hns3](https://doc.dpdk.org/guides/nics/hns3.html)）
  是否限制 iova 模式；改完必须回归功能。

## 设备探测

| 参数 | 说明 |
|------|------|
| `-a <pci>` | 白名单，只探测指定设备（调优必备：避免无关设备探测/绑定） |
| `-b <pci>` | 黑名单（与 -a 互斥）[官方] |
| `--no-pci` | 禁 PCI 总线（纯 vdev 场景） |
| `--vfio-intr <legacy\|msi\|msix>` | VFIO 中断模式 |

## SIMD / vector 控制

| 参数 | 说明 |
|------|------|
| `--force-max-simd-bitwidth=512` | 允许到 AVX-512 的 vector 路径（x86） |
| `--force-max-simd-bitwidth=64` | 禁用全部 vector 代码（调试基线用）[官方] |
| `--force-max-simd-bitwidth=0` | 取消限制 |

**经验**：对比 vector 收益的标准方法：上限位宽（x86=512，aarch64=256）与 64 各跑一次
同负载基线；aarch64 上 NEON 路径由 PMD 实现决定，以 EAL 启动日志中 PMD 报告的实际
路径为准。

## 多进程

- `--file-prefix <prefix>`：多组 primary/secondary 共存时必须不同前缀 [官方]。
- `--base-virtaddr`：secondary 因地址冲突起不来时使用 [官方]。

## 其他

| 参数 | 说明 |
|------|------|
| `--telemetry` / `--no-telemetry` | telemetry 默认开；生产高安全场景可关 [官方] |
| `--mbuf-pool-ops-name` | 指定 mempool 操作（如 ring 结构变体）[官方] |
| `--log-level lib.eal:debug` | 日志级别（热路径排查临时用） |

## 启动自检清单（每次调优后核对）

```bash
# 1. 大页够不够：启动前
grep Huge /proc/meminfo
# 2. EAL 日志中核绑定与内存分布是否符合预期（启动输出逐行读）
# 3. worker 线程落在目标核：
ps -eLo pid,psr,comm | grep -i <app>
# 4. telemetry 可用且数据合理
dpdk-telemetry.py
```
