# hotspot — 热点函数分析指南

## 概述

基于 perf 采样，识别 CPU 热点函数及其调用栈，生成火焰图，定位代码级性能瓶颈。支持 C/C++ 源码关联。

## 命令

```bash
devkit tuner hotspot [-h] [-c {n | n,m | n-m}] [-r {user,kernel,all}] [-d <sec>] [-D <sec>] [-t n] [-f n] [-l {0,1,2,3}] [-i <sec>] [-e] [-o] [-s] [-p {PID1 | PID1,PID2 | ALL}] [-g] [--package] [--long-name] [--dwarf] [-G cgroup_name] [workload...]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-c <cpu>` | CPU 核列表：`n`、`n,m`、`n-m` | 全部 |
| `-r` | 采集范围：`user`/`kernel`/`all` | all |
| `-d <sec>` | 采集时长（秒），最小值 1 | 持续直到 Ctrl+C |
| `-D <sec>` | 延迟启动采集（秒），需小于采集时长 | 0 |
| `-t <n>` | 显示 Top N 热点函数，最小值 1 | 10 |
| `-f <n>` | 采样频率（Hz），最小值 1 | 200 |
| `-l {0-3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error | 1 |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 1 |
| `-e` | 采样事件（`hotspot list` 查看可用事件） | cycles |
| `-o <file>` | 输出文件名（不需 .tar 后缀） | 当前路径 |
| `-s <dir>` | C/C++ 源码项目目录，用于源码关联 | - |
| `-p <PID>` | 目标进程 PID（逗号分隔，ALL=全系统） | 全系统 |
| `-g` | 开启调用栈采集，生成火焰图 HTML | 关闭 |
| `--package` | 生成 .tar 报告包 | 关闭 |
| `--long-name` | 显示完整函数名和模块信息 | 关闭 |
| `--dwarf` | 使用 DWARF 调试信息关联源码（C/C++） | 关闭 |
| `-G <cgroup>` | cgroup 名称（v1/v2，不支持混合模式） | - |
| `workload` | 应用程序及参数，运行并采集 | - |

## 可用采样事件

`devkit tuner hotspot list` 查看全部可用事件：

| 事件类型 | 示例 |
|---------|------|
| 默认 | cycles |
| 分支 | branch-loads, branch-load-misses |
| Cache | L1-dcache-loads, L1-dcache-load-misses, LLC-loads |
| TLB | dTLB-loads, dTLB-load-misses, iTLB-loads, iTLB-load-misses |
| 调度 | context-switches, cpu-migrations, cs |

## 输出结构

### Hotspot Metrics

函数名、cycles、模块、cycles 占比，按 Top N 排序：

```
  Function                    cycles    Module                    cycles(%)
  _PyEval_EvalFrameDefault    4,023,132,585    libpython3.11.so.1.0    8.81
  el0_svc                     3,287,092,168    [kernel]                7.20
```

### 调用栈日志（`-g`）

`callstack-时间戳.log`，树形调用链：

```
  8.77%  _PyEval_EvalFrameDefault@/usr/lib64/libpython3.11.so.1.0
         ├── 8.72%   0x15e084@/usr/lib64/libpython3.11.so.1.0
         │           ├── 0.20%   0x1c3234@/usr/lib64/libpython3.11.so.1.0
```

### 火焰图（`-g`）

`Flamegraph-时间戳.html`，可交互火焰图，浏览器打开查看。

### TAR 包（`--package`）

`.tar` 报告包，可用 `devkit report -i <file.tar>` 查看。

## 语言支持

| 语言 | 热点采集 | 火焰图 | 调用栈 | 源码关联 |
|------|---------|--------|--------|---------|
| C/C++ | 支持 | 支持 | 支持 | 支持（`-s` + `--dwarf`） |
| 内核 | 支持 | 支持 | 支持 | 不适用 |
| Python | 支持（采样 `.so`） | 支持 | 支持 | 不适用 |
| Go | 支持 | 支持 | 支持 | 不适用 |
| Java | 不支持（需 `devkit-java-perf` 独立插件） | | | |

## 结果解读

| 指标 | 异常值 | 优化方向 |
|------|--------|----------|
| 单函数 cycles 占比 > 20% | 热点集中 | 优化该函数算法或数据结构 |
| 内核函数占比高 | 系统调用开销 | 减少系统调用频率（批量 IO、缓冲） |
| 函数名显示 `***` | 函数名过长截断 | 加 `--long-name` 查看完整名 |
| 无符号信息 | 二进制未 strip | 编译时加 `-g`，采集时加 `--dwarf` |

## 约束

- 无 OS 版本限制
- 无虚拟机/容器限制
- 仅 aarch64 架构
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`

## 示例

```bash
# 采集 30 秒，显示 Top 10 热点
bash scripts/run_devkit.sh tuner hotspot -d 30 -t 10

# 生成火焰图和 TAR 包
bash scripts/run_devkit.sh tuner hotspot -d 30 -t 10 -g --package

# 指定进程采集
bash scripts/run_devkit.sh tuner hotspot -p 12345 -d 30

# 指定 CPU 核 + 采样频率 999Hz
bash scripts/run_devkit.sh tuner hotspot -d 30 -c 0-3 -f 999

# 使用 branch-load-misses 事件采集
bash scripts/run_devkit.sh tuner hotspot -d 30 -e branch-load-misses

# 关联 C/C++ 源码
bash scripts/run_devkit.sh tuner hotspot -d 30 -s /path/to/source --dwarf
```
