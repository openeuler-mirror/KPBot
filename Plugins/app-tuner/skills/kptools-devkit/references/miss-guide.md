# miss — Cache Miss 分析指南

## 概述

基于 ARM SPE（Statistical Profiling Extension），按函数粒度定位 Cache Miss、TLB Miss、远端访问和长延迟 Load 的来源，实现代码级归因。与 top-down（宏观定性）和 memory（系统级量化）构成三级分析层次：top-down 发现 Memory Bound 瓶颈类型，memory 量化系统级带宽和命中率，miss 定位到具体函数。

## 命令

```bash
devkit tuner miss [-h] [-c {n | n,m | n-m}] [-d <sec>] [-P n] [-D <sec>] [-t n] [-l {0,1,2,3}] [-m {1,2,3,4}] [-L n] [-i <sec>] [-r {user,kernel,all}] [-o] [-s] [-p {PID1 | PID1,PID2 | ALL}] [--package] [--long-name] [--dwarf] [workload...]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-c <cpu>` | CPU 核列表：`n`、`n,m`、`n-m` | 全部 |
| `-d <sec>` | 采集时长（秒），最小值 1 | 持续直到 Ctrl+C |
| `-P <n>` | SPE 采样周期（cycles），范围 1024~2^32-1 | 8192 |
| `-D <sec>` | 延迟启动采集（秒） | 0 |
| `-t <n>` | 显示 Top N 函数 | 10 |
| `-l {0-3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error | 1 |
| `-m {1-4}` | 采集模式（见下表） | 1 (LLC Miss) |
| `-L <n>` | 长延迟 Load 阈值（cycles），范围 1-4095，仅 `-m 4` | 64 |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 1 |
| `-r` | 采集范围：`user`/`kernel`/`all` | all |
| `-o <file>` | 输出文件名 | 当前路径 |
| `-s <dir>` | C/C++ 源码项目目录，用于源码关联 | - |
| `-p <PID>` | 目标进程 PID（逗号分隔，ALL=全系统） | 全系统 |
| `--package` | 生成 .tar 报告包 | 关闭 |
| `--long-name` | 显示完整函数名和模块信息 | 关闭 |
| `--dwarf` | 使用 DWARF 调试信息关联源码（C/C++） | 关闭 |
| `workload` | 应用程序及参数 | - |

## 采集模式

| 模式 | 名称 | 分析内容 |
|------|------|---------|
| 1 | LLC Miss | L3 Cache 未命中，按函数统计 Miss Rate |
| 2 | TLB Miss | TLB 未命中，按函数统计 Miss Rate |
| 3 | Remote Access | 远端内存访问（跨 NUMA），按函数统计 |
| 4 | Long Latency Load | 长延迟 Load（阈值可设 `-L`），按函数统计 |

## 输出结构

### Miss Summary Report

函数名、模块、Miss Rate（%），按 Top N 排序：

```
  Function                  Module                              LLC Miss Rate
  m_next                    [kernel]                                  17.28 %
  0x9b660                   /usr/lib64/libc.so.6                       7.41 %
  Table_cleanupRow          /usr/bin/htop                              7.41 %
```

### TAR 包（`--package`）

`.tar` 报告包，可用 `devkit report -i <file.tar>` 查看。

## 结果解读

| 指标 | 异常值 | 优化方向 |
|------|--------|----------|
| LLC Miss Rate > 10% | L3 缓存压力大 | 改善数据局部性，减小数据集，使用预取 |
| TLB Miss Rate 高 | TLB 容量不足 | 使用大页（HugePages），减小内存碎片 |
| Remote Access 高 | 跨 NUMA 访问多 | 绑核 + 内存亲和性绑定 |
| Long Latency Load 高 | 内存延迟瓶颈 | 数据布局优化，减少随机访问 |

## 约束

- 无 OS 版本限制
- **不支持虚拟机和容器环境**
- 仅 aarch64 架构
- 需 ARM SPE 硬件支持
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`
- 采集前需启用 SPE

## 示例

```bash
# LLC Miss 分析，Top 10 函数
bash scripts/run_devkit.sh tuner miss -d 30 -m 1 -t 10

# TLB Miss 分析
bash scripts/run_devkit.sh tuner miss -d 30 -m 2

# 远端访问分析
bash scripts/run_devkit.sh tuner miss -d 30 -m 3

# 长延迟 Load 分析（阈值 128 cycles）
bash scripts/run_devkit.sh tuner miss -d 30 -m 4 -L 128

# 指定进程采集
bash scripts/run_devkit.sh tuner miss -p 12345 -d 30 -m 1

# 生成 TAR 报告包
bash scripts/run_devkit.sh tuner miss -d 30 -m 1 --package

# 关联 C/C++ 源码
bash scripts/run_devkit.sh tuner miss -d 30 -m 1 -s /path/to/source --dwarf
```
