# top-down — 微架构分析指南

## 概述

基于 ARM PMU（Performance Monitor Unit）事件，分析指令在 CPU 流水线上的运行情况，
帮助快速定位当前应用在 CPU 上的性能瓶颈。

## 命令

```bash
devkit tuner top-down [-h] [-c {n | n,m | n-m}] [-d <sec>] [-D <sec>] [-l {0,1,2,3}] [-L {0,1,2,3,4,5,6}] [-i <sec>] [-p {PID1 | PID1,PID2 | ALL}] [-r {user,kernel,all}] [-G cgroup_name] [workload...]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-c <cpu>` | CPU 核列表：`n`、`n,m`、`n-m` | 全部 |
| `-d <sec>` | 采集时长（秒），最小值 1。默认持续采集，可用 Ctrl+\ 取消或 Ctrl+C 停止 | 持续 |
| `-D <sec>` | 延迟启动采集（秒），需小于采集时长 | 0 |
| `-l {0-3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error | 1 |
| `-L {0-6}` | profile 级别（见下表） | 0 (ALL) |
| `-i <sec>` | 采样间隔（秒），生成子报告，需 ≤ 采集时长 | 1 |
| `-p <PID>` | 目标进程 PID（逗号分隔多个，`ALL`=全系统） | 全系统 |
| `-r` | 采集范围：`user`/`kernel`/`all` | all |
| `-G` | cgroup 名称（支持 cgroup v1 和 v2，不支持混合模式） | - |
| `workload` | 应用程序及参数，运行并采集 | - |

## profile 级别

| 级别 | 名称 | 含义 |
|------|------|------|
| 0 | ALL | 采集以下所有维度（默认） |
| 1 | Level1 | Backend Bound、Bad Speculation、Frontend Bound、Retiring |
| 2 | Core Bound | Backend Bound 子类 — 执行单元资源不足导致的性能瓶颈 |
| 3 | Memory Bound | Backend Bound 子类 — 等待数据读/写导致的流水线阻塞 |
| 4 | Resource Bound | Backend Bound 子类 — 缺乏资源把微指令分发给乱序执行调度器（README 标注仅鲲鹏 920 系列，但鲲鹏 950 实测可用） |
| 5 | Bad Speculation | 错误的指令预测操作导致的流水线资源浪费 |
| 6 | Frontend Bound | 指令获取单元未充分利用 |

> 注：级别 4 单独采集（`-L 4`）README 标注仅鲲鹏 920 系列，但鲲鹏 950 实测可用。
> 但 `-L 0`（ALL）在鲲鹏 950 上会输出 Resource Bound 子项（Rob_stall/MapQ_stall 等）。

## 输出结构

### Top-down 指标树

`-L 0`（ALL）输出完整 7 级树：

```
Top-down metrics of the system:
Cycles              129,522,462,089
Instructions        178,765,476,766
IPC                 1.38

  Top-down Metrics                        Bound(%)
  Bad Speculation                             3.10
  ├── Branch Mispredicts                      1.89
  │   ├── Indirect Branch                     0.11
  │   ├── Push Branch                         0.05
  │   ├── Pop Branch                          0.05
  │   └── Other Branch                        1.69
  └── Machine Clears                          1.21
      ├── Nuke Flush                          0.26
      └── Other Flush                         0.94

  Frontend Bound                             10.81
  ├── Fetch Latency Bound                     7.02
  │   ├── L1I TLB or Cache Miss               5.41
  │   │   ├── L1I TLB Miss                    0.38
  │   │   └── L1I Cache Miss                  5.04
  │   ├── Bru Flush                           0.48
  │   └── BPU Q Stall                         0.10
  └── Fetch Bandwidth Bound                   3.78

  Retiring                                   17.25

  Backend Bound                              68.84
  ├── Core Bound                             33.76
  │   ├── Resource Bound                      1.32
  │   │   ├── Rob_stall                       0.15
  │   │   ├── MapQ_stall                      0.96
  │   │   └── DSP_stall                       0.16
  │   └── Exe Ports Util                     32.42
  │       ├── 0 ports serialize               4.86
  │       ├── 0 ports non serialize          14.95
  │       └── ...
  └── Memory Bound                           35.08
      ├── L1 Bound                           21.30
      │   ├── DTLB                            2.56
      │   ├── Forward hazard                 12.36
      │   └── Pipeline                        4.35
      ├── L2 Bound                            4.99
      ├── L3 Bound                            7.67
      ├── Mem Bound                           1.10
      └── Store Bound                         0.02
```

### PMU 事件表

PMU 事件原始计数（含事件编码）：

```
  PMU Event                                  Count
  r0008                            178,765,476,766
  r0011                            129,522,462,089
  r001b                            210,893,654,679
  ...
```

关键 PMU 事件：
- `r0008` = 已退休指令数
- `r0011` = 周期数
- 其余为架构相关 PMU 计数器

### 优化建议

输出末尾包含进一步分析提示：

```
Note: To view the hotspot data. You can run devkit tuner hotspot -e [Preferred Sampling Event]
```

## 结果解读

| 指标 | 高值含义 | 优化方向 |
|------|----------|----------|
| Backend Bound > 60% | 执行/内存瓶颈 | 查看 Memory Bound vs Core Bound 细分 |
| Memory Bound > 30% | 数据访问停顿 | 改善缓存局部性，减少内存访问，使用预取 |
| L1 Bound 高 | L1 缓存压力 | 减少数据足迹，改善访问模式 |
| Forward hazard 高 | 加载/存储队列冲突 | 减少重叠 load/store，调整数据布局 |
| Core Bound > 30% | 执行单元饱和 | 减少指令依赖，提升 ILP |
| 0 ports non-serialize 高 | 指令等待资源 | 减少序列化指令 |
| Frontend Bound > 15% | 指令获取瓶颈 | 代码体积缩减，指令缓存优化 |
| L1I Cache Miss 高 | 指令缓存未命中 | 函数拆分/冷热分离 |
| Bad Speculation > 5% | 分支预测失败 | 分支友好算法，likely/unlikely 提示 |
| Retiring < 20% | 有效工作比例低 | 需算法层面优化 |

## 子报告

设置 `-i` 后，每个采样间隔生成子报告，可分析指标随时间变化趋势。

## 约束

- 无 OS 版本限制
- 无虚拟机/容器限制
- 仅 aarch64 架构
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`
- `-L 4` README 标注仅鲲鹏 920 系列，鲲鹏 950 实测可用

## 示例

```bash
# 系统级全量 topdown，采集 3 秒
bash scripts/run_devkit.sh tuner top-down -d 3 -L 0

# 指定进程，仅 level1
bash scripts/run_devkit.sh tuner top-down -p 1234 -d 30 -L 1

# 指定 CPU 核，Memory Bound 细分
bash scripts/run_devkit.sh tuner top-down -c 0-3 -d 10 -L 3

# 每秒子报告，跟踪指标变化
bash scripts/run_devkit.sh tuner top-down -d 30 -i 1 -L 0

# 采集 workload
bash scripts/run_devkit.sh tuner top-down -d 10 -- ./my_app --arg1
```
