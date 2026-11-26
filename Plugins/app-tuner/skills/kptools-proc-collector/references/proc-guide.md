# proc-collector — 进程/线程级采集指南

## 概述

采集进程/线程级证据：线程-CPU 分布、上下文切换、CPU 迁移、亲和性、内存映射（DSO）、NUMA 归属、cgroup 限制、二进制信息。数据供 cpu-affinity 优化、library-selection、os-optimization 等 subskill 消费。

## 模式与指标

| 模式 | 工具 | 指标 | 典型用途 |
|------|------|------|---------|
| `--threads` | ps -L | tid、psr（当前核）、pcpu、stat、comm、wchan | 线程-CPU 分布、均衡性输入 |
| `--cpu` | pidstat -t -u | 每线程 %usr/%system/%CPU | 热点线程识别 |
| `--ctx` | pidstat -t -w | 每线程 cswch/s（自愿）、nvcswch/s（非自愿） | 锁竞争/调度干扰 |
| `--migration` | perf stat | context-switches、cpu-migrations 计数 | 迁移级别判定 |
| `--affinity` | taskset | 主进程与每线程 CPU 掩码 | 绑核现状确认 |
| `--maps` | /proc/maps | 映射区间、权限、路径 | DSO 归属 |
| `--numa` | numa_maps + numastat | 节点页分布 | 跨 NUMA 访问定位 |
| `--cgroup` | sysfs + /proc/PID/cgroup | PID 所在 cgroup 的 cpuset/cpu.max/memory.max（v2 含最近限制祖先回溯，v1 自动适配） | 容器资源边界 |
| `--binary` | readelf/ldd | ISA（AAPCS64 等）、NEEDED 库 | 编译优化/库选择 |
| `--env` | environ | OMP/MKL_NUM_THREADS 等 | 线程池过提交分析 |

## 阈值参考（源自 app-tuner cpu-affinity 实践）

| 指标 | 阈值 | 判定 |
|------|------|------|
| 线程分布 skew | max-min >= 2 | skewed（线程集中于部分核） |
| 迁移次数（窗口内） | >100 high / >10 medium / 其余 low | high 时收紧亲和性 |
| nvcswch/s（每线程） | > 500 | 非自愿切换频繁，锁竞争/过调度 |
| context-switches/s（进程级） | > 100K | 调度压力大 |
| 热点 TID 连续 3 轮不变 | - | 稳定热点，适合细粒度绑核 |

## check_balance 输出格式

```json
{
  "balance_status": "balanced|skewed|single_cpu_or_single_thread|ps_thread_view_empty",
  "hot_cpu_list": [12, 13],
  "thread_distribution": {"12": 4, "13": 4, "30": 2},
  "max_threads_on_cpu": 4,
  "min_threads_on_cpu": 2
}
```

## parse_maps 输出格式

```json
[{"path": "/home/app/myserver", "size_kb": 20480, "type": "main_binary"},
 {"path": "/usr/lib64/libfoo.so", "size_kb": 512, "type": "third_party"},
 {"path": "/usr/lib64/libc.so.6", "size_kb": 1800, "type": "libc"}]
```

分类规则：与 `/proc/PID/exe` 相同 → main_binary；libc/ld- → libc；`*.so` → third_party；其余 → other。按映射大小降序。

## 工具分工

| 采集需求 | 工具 |
|---------|------|
| 系统级资源（CPU/内存/磁盘/网络） | kptools-nmon |
| 进程/线程级（本 SKILL） | kptools-proc-collector |
| 微架构/topdown/cache miss | kptools-devkit |
| 网络深度（丢包/IRQ/RSS） | kptools-network-collector |

## 约束

- 跨 uid 读取 /proc/PID/{maps,numa_maps,environ}、taskset 需同 uid 或 root
- perf 事件受 `perf_event_paranoid` 限制；`sched:sched_migrate_task` tracepoint 需 root
- pidstat 依赖 sysstat 包；缺失时 --cpu/--ctx 不可用（--threads 仍可提供瞬时分布）
- 容器场景：容器内视角 cgroup 为容器边界；宿主机视角需在宿主机执行
