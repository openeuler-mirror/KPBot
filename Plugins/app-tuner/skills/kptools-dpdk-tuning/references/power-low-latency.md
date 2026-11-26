# M7 功耗与低时延特化

来源：[官方] dpdk `doc/guides/prog_guide/power_man.rst`
（https://doc.dpdk.org/guides/prog_guide/power_man.html，26.11.0-rc2 实测核对，
版本溯源见 UPSTREAM.md）、`linux_gsg/enable_func`、`linux_gsg/amd_platform`。

## 目标冲突声明

- **吞吐优先**：BIOS/内核层面彻底关 idle（C-states）、governor=performance、
  不用任何功耗 API。
- **省电优先**（流量低谷期）：PMD power management API，按空转计数触发省电。
- **低时延优先**：不睡眠 + governor=performance + PM QoS 收紧恢复时延。
  低时延与省电天然冲突，先确认业务目标。

## PMD Power Management API（RX 回调省电）[官方]

三种 scheme（按队列经 `rte_power_pmd_mgmt_queue_enable` 挂接）：

| Scheme | 机制 | 限制 |
|--------|------|------|
| monitor | 监听 RX 描述符地址，新流量唤醒 CPU（umonitor/umwait） | x86_64 需 WAITPKG 指令集；多队列监控需 WAITPKG+RTM（RTM 可能需内核参数 `tsx=on`）；CPU 不支持则不可用；并非所有 PMD 支持监控 |
| pause | `rte_power_pause()` 优化睡眠，平台不支持则退化为 `rte_pause()` | 反应最快、最通用 |
| freq-scaling | 按流量调频（librte_power） | 反应比 pause/monitor 慢 |

相关可调：emptypoll max（空转多少次进入省电）、pause duration（µs）、
scaling min/max freq（kHz）——均有 Get/Set API [官方]。
testpmd 侧对应 `--pmd-mgmt=...` 类参数（以目标环境 testpmd 文档为准，版本差异大）。

**注意**：不是所有驱动支持 monitor scheme——查
[overview 特性矩阵](https://doc.dpdk.org/guides/nics/overview.html) 与驱动文档 [官方指引]。
**经验**：aarch64/Kunpeng 上 monitor scheme 通常不可用（依赖 x86 WAITPKG），实际可用
scheme 以目标环境实测为准。

## PM QoS（收紧恢复时延）[官方]

- sysfs：`/sys/devices/system/cpu/cpuX/power/pm_qos_resume_latency_us`
- API：`rte_power_qos_set_cpu_resume_latency()` 设 0 → 严格最低恢复时延，
  时延敏感（含中断收包模式）收益；功耗上升。

## CPU 频率控制（librte_power）[官方]

- 前提：BIOS 开 SpeedStep（Intel）且 C3/C6 开启（enable_func 官方要求）；否则
  `/sys/.../cpufreq` 不存在。aarch64 平台对应 cpufreq 驱动以内核配置为准 [经验]。
- governor 由用户态配置，`scaling_setspeed` 逐核调频；API：freq up/down/max/min/set、
  turbo enable/disable（per-core turbo）。
- SMT 环境下：物理核频率取所有 SMT 兄弟线程的最大值——单线程降频无效 [官方 x86 表述；
  经验：aarch64 SMT 同理]。

## Uncore / SoC 级调优 [官方]

- Intel：`intel-uncore-frequency` 驱动（内核 ≥5.6 + CONFIG_INTEL_UNCORE_FREQ_CONTROL），
  操作 MSR 0x620 设 uncore min/max。
- AMD：HSMP（内核 ≥5.18 + CONFIG_AMD_HSMP），`rte_power_uncore` 自 25.03 支持
  （GENOA/SIENA/TURIN 验证）。
- **吞吐场景结论 [经验]**：uncore 频率被节能拉低会吃掉内存带宽——高吞吐机器确认
  uncore/SoC 互联不在低频状态。aarch64 无统一接口，按 SoC 供应商工具（UNICODE 等）核对。

## AMD EPYC [官方 amd_platform]

- rte_power 支持（23.11 LTS 起）：`amd_pstate` 驱动 passive 模式，内核 ≥6.4 验证。
- 低时延：GRUB `pcie_aspm=off`。
- Solarflare 网卡低时延：内核 ≥6.13、`vfio-pci.disable_idle_d3=1`、
  PMD 参数 `fw_variant=ultra-low-latency,perf_profile=throughput`。

## 低时延 checklist

| 项 | 值 | 出处 |
|----|----|------|
| C-states | BIOS 关闭 + `processor.max_cstate=0 idle=poll`（x86）| 经验 |
| governor | performance | 官方 power 前提 |
| PM QoS resume latency | 0 | 官方 |
| PCIe ASPM | off（AMD 官方明示；x86 同理 [经验]） | amd_platform 官方 |
| 核隔离 | isolcpus+nohz_full，禁用 CPU0 | 官方 enable_func |
| 轮询方式 | 保持忙轮询，禁用 monitor/pause scheme | 官方机制推断 |
| uncore | 频率锁定高端 | 官方 API + 经验 |

测量：`--latencystats`（testpmd 官方）或业务侧打点；改动前后各测一份分布
（均值+P99/P99.9），只看均值会漏尾部问题 [经验]。
