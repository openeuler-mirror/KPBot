# M1+M2 硬件/BIOS 与 内核/OS 层调优

来源约定：[官方] = DPDK 官方 guides（URL 见 UPSTREAM.md，目标工作区 dpdk 源码仓的
`doc/guides/` 优先）；[经验] = 工程经验，需现场验证。

## M1 硬件与 BIOS

### 内存通道与频率 [官方]
性能受内存通道填充直接影响（官方平台调优指南首条要求）。

```bash
# 每通道至少 1 根 DIMM；核对其数量与频率
dmidecode -t memory | grep Locator
dmidecode -t memory | grep Speed
```

- 判据：每内存通道 ≥1 DIMM（AMD EPYC 建议 DIMM ≥8/16/32GB ECC [官方]）；
  插槽空缺或频率低于平台规格 → 先补齐再谈调优。

### PCIe 链路核对 [官方]

```bash
lspci -s 03:00.1 -vv | grep LnkSta
# 期望: Speed 8GT/s (Gen3) 或 16GT/s (Gen4), Width x8/x16
```

- 判据：100G 网卡跑在 Gen2 或 x4 → 带宽瓶颈；链路协商低于槽位规格 → 检查插卡/riser。
- **经验**：LnkSta 是"Speed"与"Width"都要看；部分背板会把 x16 槽位走 x8。

### 网卡固件与拓扑 [官方]

```bash
lspci -nn | grep Eth                 # 网卡型号与 PCI 地址
cat /sys/bus/pci/devices/0000:82:00.0/numa_node   # 网卡所在 socket
usertools/cpu_layout.py              # 或 lscpu，核对核与 socket 分布
```

- 判据：worker 核、网卡、mbuf 池三者必须同 socket [官方：To get the best performance,
  ensure that the core and NICs are in the same socket]。
- 多网卡场景：业务使用的多端口尽量同 socket [官方]。
- 刷最新 NVM/firmware [官方]。
- 鲲鹏场景常见形态：Kunpeng 920 + Hi1822（4x25GE）/ hns3 RDMA 网卡控制器，
  驱动行为以 [hns3 PMD 文档](https://doc.dpdk.org/guides/nics/hns3.html) 为准。

### BIOS 设置 [官方]

| 项 | 推荐 | 出处 |
|----|------|------|
| CPU Power & Performance policy | Performance | nic_perf_intel_platform |
| Turbo Boost | 按 CPU/性能策略需要开启 | 同上 |
| C-states | 追求吞吐/低时延时禁用（BIOS 层面）；使用 DPDK 功耗功能时反而需开启 C3/C6 | enable_func |
| VT-d / IOMMU | 用 VFIO 需开启；测试物理口（PF）直通时官方建议关闭虚拟化项 | nic_perf_intel_platform |
| DDIO / SNC 等平台特性 | 按 CPU/平台调优指南（官方有 AMD EPYC 分 SoC 指南：MILAN/GENOA/TURIN） | amd_platform |

- ARM/Kunpeng 平台无官方 DDIO/SNC 等价项，BIOS 项以整机厂商手册为准 [经验]；
  DPDK 通用层（大页/隔离/NUMA 对齐）结论不变。

## M2 内核与 OS

### 内核启动参数（GRUB）[官方]

```text
# 大页：官方推荐 64 位应用用 1G 大页
default_hugepagesz=1G hugepagesz=1G hugepages=8

# 核隔离（DPDK 专用核，例如 2,4,6）
isolcpus=2,4,6 nohz_full=2,4,6 irqaffinity=0,1,3,5,7

# IOMMU/VFIO
# x86: iommu=pt intel_iommu=on
# ARM: 按 SMMU/平台文档配置 passthrough 类参数（openEuler 常见 smmu.bypassdev）
```

要点 [官方 enable_func]：
- **不要把 CPU0 分给 DPDK 轮询应用**——它无法真正隔离。
- `isolcpus` 把核移出通用调度；`nohz_full` 停掉该核的内核时钟 tick；
  `irqaffinity` 把中断压到 housekeeping 核。三者配合使用。
- 更细粒度用 cgroups cpuset / systemd CPUAffinity。

### 运行时隔离核验证

```bash
cat /sys/devices/system/cpu/isolated        # isolcpus 生效确认
cat /sys/bus/workqueue/devices/htbound0/cpuset 2>/dev/null; ps -eLo psr,comm | awk '$1==<isolated_cpu>'  # 该核是否仍有任务
taskset -pc <worker_pid>                     # DPDK worker 是否落在隔离核
perf stat -C <isolated_cpu> -e context-switches -- sleep 5   # 期望接近 0
```

[经验] 注意：DPDK 线程未用 `--lcores` 显式绑定时不会自动挑隔离核——必须核对。

### 大页预留 [官方]

```bash
# 运行时（2M 页示例）
echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
# NUMA 机器按节点预留（推荐，避免默认均分不可控）
echo 1024 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages

# 挂载（多进程必需；单进程可用 --in-memory 免挂载）
mkdir -p /mnt/huge && mount -t hugetlbfs nodev /mnt/huge
# ⚠️ 1G 页必须带 pagesize 挂载专用挂载点（默认 /dev/hugepages 是 2M 页，装不下 1G 页）
mkdir -p /mnt/huge1G && mount -t hugetlbfs -o pagesize=1G nodev /mnt/huge1G
# 应用侧配 --huge-dir /mnt/huge1G 才能用上 1G 页

# 或用官方工具
usertools/dpdk-hugepages.py --reserve 1G / --show / --mount
```

- 部分内核不支持运行时预留 1G 页 → 必须启动参数预留 [官方]。
  [经验/逐机不定] 运行时预留 1G 页**不可依赖**：openEuler 24.03（kernel 6.6，
  Kunpeng 920）同型号两台机器实测结论相反——一台成功（`echo 4 > .../hugepages-1048576kB/nr_hugepages`
  后 `dpdk-hugepages.py --show` 确认），另一台重启清零内存后仍失败（排除碎片化假设）。
  **结论：1G 页直接走 GRUB 启动参数预留**；2M 页运行时预留可靠（多台实测成功）。
  验证方法：预留后 `grep Huge /proc/meminfo`，Total 不涨即回退 GRUB。
- 需要物理连续大块内存（如某些收包场景）→ 启动时预留避免碎片 [官方]。
- CPU 是否支持 1G 页：aarch64 查 CPU features（`lscpu` Flags/CPU 特性输出），
  x86 查 flags 含 `pdpe1gb` [官方]。

### 中断与系统服务

```bash
systemctl disable --now irqbalance   # [经验] 防止中断乱跑；配合 irqaffinity=
grep -E 'CPU[0-9]+' /proc/interrupts | head   # 核对中断分布，DPDK 核上应无 NMI/LOC 增长异常
```

- [经验] `tuned-adm profile throughput-performance` / `cpupower frequency-set -g performance`
  确保 governor=performance（验证：`cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`）。
  governor 由用户态控制是官方 power 库前提（见 power_man 文档）。

### 资源限制与系统要求 [官方]

- 内核版本要求随 DPDK 版本变化（26.11 要求 ≥5.4）；内核需 HUGETLBFS、PROC_PAGE_MONITOR 支持。
- 非 root 运行：RLIMIT_MEMLOCK / RLIMIT_NOFILE / RLIMIT_LOCKS 需放宽
  （`/etc/security/limits.conf`），PA 模式还需 `cap_dac_read_search,cap_ipc_lock,cap_sys_admin`。
- [经验] 透明大页（THP）建议 `never`：DPDK 自己管大页，THP 引入的内核回收/迁移抖动
  影响尾时延。属工程共识，官方 GSG 未强制。

### AMD EPYC 特有 [官方]

- 低时延场景：GRUB 加 `pcie_aspm=off`。
- rte_power 需 amd_pstate 驱动 passive 模式（23.11 LTS 起，内核 ≥6.4 验证）。
- uncore 经 HSMP（内核 ≥5.18，CONFIG_AMD_HSMP）。

## 排查入口速查

| 症状 | 先查 |
|------|------|
| 所有调优无效果 | isolated 核是否真的空 + worker 是否绑上去（上面两条命令） |
| 时延毛刺 | 该核 context-switches / irqbalance 是否在跑 / THP |
| 启动即 OOM/内存不足 | 大页 Free 数量（`grep Huge /proc/meminfo`）|
| 吞吐上不去 | 先回到 M5（队列/描述符），再看本层 |
