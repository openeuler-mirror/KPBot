# kptools-os-collector 使用指南

## 1. 定位

OS/内核级只读采集，环境诊断层（Step2）与 os-optimization 的证据来源。采集与分析分离：本 SKILL 采集，os-optimization 分析+优化。

## 2. 关键指标解读

| 指标 | 阈值/期望 | 关联优化 |
|------|----------|---------|
| vm.swappiness | 数据库/低时延场景 ≤10 | os-optimization |
| vm.dirty_ratio / dirty_background_ratio | 写多场景适当调大 | os-optimization |
| THP enabled | 数据库多为 never/madvise；JVM/HPC 多为 always | os-optimization |
| HugePages_Total | 大内存应用预分配 | os-optimization |
| ulimit nofile | 高并发服务 ≥ 65535 | os-optimization |
| tuned profile | 期望 throughput-performance/high-performance | os-optimization |
| irqbalance | 静态 IRQ 绑核场景建议停止 | network/cpu-affinity |
| I/O scheduler | NVMe→none；SSD→mq-deadline | os-optimization |
| numa_balancing | 静态绑核场景关闭（0） | cpu-affinity |
| perf_event_paranoid | ≤1 才能采足够事件；2+ 限制内核事件 | devkit/proc-collector 前置 |
| kptr_restrict | 0 才能看到内核符号 | devkit 前置 |

## 3. 虚拟化检测多级降级

1. `systemd-detect-virt`（存在则可信）
2. `/proc/cpuinfo` hypervisor 标志
3. `/proc/1/cgroup` + `/.dockerenv`/`/run/.containerenv` 容器标记

## 4. cgroup 边界（v2/v1）

- v2：读 `/sys/fs/cgroup/{cpuset.cpus.effective,cpu.max,memory.max,memory.high}`；`max` 表示未限制
- v1：按控制器目录（cpuset/cpu/memory）分别读取
- 容器内读到的即容器自身边界；对照宿主机需在宿主机执行

## 5. 与其他 SKILL 的关系

| 采集项 | 归属 | 说明 |
|--------|------|------|
| 进程级 limits/cgroup/environ | kptools-proc-collector | 进程视角 |
| BIOS 层（SMT/Turbo/ASPM 等） | kptools-bios-collector | 固件视角 |
| 内核层（sysctl/THP/服务） | 本 SKILL | 系统视角 |
