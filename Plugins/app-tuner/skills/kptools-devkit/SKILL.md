---
name: kptools-devkit
description: >
  使用华为鲲鹏 DevKit 采集 CPU core/uncore 频率、topdown 微架构指标
  和 NUMA 访存分析。当用户需要 topdown 分析、微架构瓶颈定位、CPU 频率采集、
  core 频率、uncore 频率、流水线瓶颈分析、NUMA 访存分析、NUMA 跨节点访问、
  内存亲和性分析、功耗监控、CPU 功耗、Socket 功耗、温度监控、热点函数、
  火焰图、调用栈、Cache Miss、TLB Miss、远端访问、长延迟 Load、内存带宽、
  Cache 命中率、DDR 带宽、鲲鹏 DevKit、devkit 命令时触发。
license: Proprietary
compatibility: aarch64, requires devkit 26.1.RC1
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.0.0"
---

# kptools-devkit — 鲲鹏 DevKit 性能采集

## 工作流程

1. 检查 devkit 是否已部署：`bash scripts/discover_devkit.sh`
2. 如未安装，执行部署：`bash scripts/deploy_devkit.sh`
3. 采集数据：`bash scripts/run_devkit.sh tuner <task> -d <sec> [options]`

> 采集时必须传 `-d` 参数指定采集时长，否则命令会持续运行直到手动中断。
>
> deploy_devkit.sh 下载 RPM 并解压到 /opt/devkit/（root）或
> ~/.local/share/devkit/（user），无需 sudo。

## 脚本工具

- `scripts/deploy_devkit.sh` — 下载 RPM + sha256 校验 + rpm2cpio 解压
- `scripts/discover_devkit.sh` — 检查 devkit 安装状态和子任务可用性
- `scripts/run_devkit.sh` — CLI 包装器，设置 LD_LIBRARY_PATH 后透传命令

## 采集命令

### core/uncore 频率与功耗采集（turbostat）

```bash
# 采集 30 秒，每秒一个子报告
bash scripts/run_devkit.sh tuner turbostat -d 30 -i 1

# 指定 CPU 核
bash scripts/run_devkit.sh tuner turbostat -d 30 -i 1 -c 0-3

# 带 BMC 带外数据（需 BMC IP/用户名/密码）
bash scripts/run_devkit.sh tuner turbostat -d 30 --bmc
```

输出包含：
- Per NUMA Frequency Table（CPU 频率 + Uncore 频率，按 NUMA 分组）
- CPU Core Frequency Table（每核频率）
- Uncore(L3 Cache) Frequency Table（每个 L3 Cache 设备频率）
- CPU Socket Power and Temperature Table（Socket 功耗 + Die 温度）
- Server Status（--bmc：整机功耗/CPU 功耗/内存功耗/进出风口温度）

### topdown 微架构分析（top-down）

```bash
# 系统级 topdown（全量 7 级）
bash scripts/run_devkit.sh tuner top-down -d 30

# 指定进程
bash scripts/run_devkit.sh tuner top-down -p <PID> -d 30

# 仅 level1（Backend/Bad Spec/Frontend/Retiring）
bash scripts/run_devkit.sh tuner top-down -d 30 -L 1

# 指定 CPU 核 + level3（Memory Bound 细分）
bash scripts/run_devkit.sh tuner top-down -c 0-3 -d 30 -L 3
```

输出包含：
- Top-down metrics（Bad Speculation / Frontend Bound / Retiring / Backend Bound，百分比）
- IPC、Cycles、Instructions
- PMU 事件原始计数
- 瓶颈优化建议

### NUMA 访存分析（numafast）

```bash
# 采集 30 秒，每 5 秒一个子报告
bash scripts/run_devkit.sh tuner numafast -d 30 -i 5

# 指定进程采集
bash scripts/run_devkit.sh tuner numafast -p <PID> -d 30

# 生成 TAR 结果包
bash scripts/run_devkit.sh tuner numafast -d 30 --package
```

输出包含：
- NUMA 访问流量矩阵（SRC→DST 的带宽/NUMA 距离/访问占比）
- NUMA score（0=最差，1=最优）
- 节点详情（RMA 远端访问/LMA 本地访问/内存利用率/CPU 占用）
- Top N 进程/线程（访存得分/远端访问比例/线程迁移次数）

> numafast 需 ARM SPE 硬件支持，不支持虚拟机和容器环境。

### 热点函数分析（hotspot）

```bash
# 采集 30 秒，显示 Top 10 热点函数
bash scripts/run_devkit.sh tuner hotspot -d 30 -t 10

# 生成火焰图和调用栈
bash scripts/run_devkit.sh tuner hotspot -d 30 -t 10 -g --package

# 指定进程采集
bash scripts/run_devkit.sh tuner hotspot -p <PID> -d 30

# 查看可用采样事件
bash scripts/run_devkit.sh tuner hotspot list
```

输出包含：
- Hotspot Metrics（函数名、cycles、模块、cycles 占比）
- 调用栈日志和火焰图（`-g`）
- TAR 报告包（`--package`）

> 支持 C/C++ 源码关联（`-s` + `--dwarf`）。Java 需独立插件。

### 内存带宽与命中率（memory）

```bash
# 全量采集（Cache + DDR + HBM）
bash scripts/run_devkit.sh tuner memory -d 30 -m 1

# 仅 Cache 带宽与命中率
bash scripts/run_devkit.sh tuner memory -d 30 -m 2

# 仅 DDR 带宽（按 NUMA 每控制器）
bash scripts/run_devkit.sh tuner memory -d 30 -m 3
```

输出包含：
- Cache Miss 概览（L1D/L1I/L2D/L2I）
- DDR 读写带宽
- L1/L2/L3/TLB 带宽与命中率
- DDRC 带宽（按 NUMA 每控制器）

### Cache Miss 分析（miss）

```bash
# LLC Miss 分析，显示 Top 10 函数
bash scripts/run_devkit.sh tuner miss -d 30 -m 1 -t 10

# TLB Miss 分析
bash scripts/run_devkit.sh tuner miss -d 30 -m 2

# 远端访问分析
bash scripts/run_devkit.sh tuner miss -d 30 -m 3

# 长延迟 Load 分析（阈值 128 cycles）
bash scripts/run_devkit.sh tuner miss -d 30 -m 4 -L 128
```

输出包含：
- Miss Summary Report（函数名、模块、Miss Rate，按 Top N 排序）
- TAR 报告包（`--package`）

> miss 需 ARM SPE 硬件支持，不支持虚拟机和容器环境。支持 C/C++ 源码关联（`-s` + `--dwarf`）。

## 参数速查

### turbostat

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 不生成子报告 |
| `-c <cpu>` | CPU 核列表（如 0,1,2 或 0-2） | 全部 |
| `--bmc` | 启用 BMC 带外数据 | 关闭 |
| `-l {0-3}` | 日志级别（0=debug, 1=info, 2=warn, 3=error） | 1 |

### top-down

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标进程（逗号分隔，ALL=全系统） | 全系统 |
| `-c <cpu>` | CPU 核列表 | 全部 |
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-L {0-6}` | profile 级别 | 0 (ALL) |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 1 |
| `-D <sec>` | 延迟启动（秒） | 0 |
| `-r` | 采集范围 user/kernel/all | all |

topdown profile 级别：

| 级别 | 含义 |
|------|------|
| 0 | ALL（全部维度） |
| 1 | Level1：Backend Bound / Bad Speculation / Frontend Bound / Retiring |
| 2 | Backend Bound → Core Bound（执行单元资源瓶颈） |
| 3 | Backend Bound → Memory Bound（数据读写流水线停顿） |
| 4 | Backend Bound → Resource Bound（微操作分发资源停顿，README 标注仅鲲鹏 920 系列，950 实测可用） |
| 5 | Bad Speculation（分支预测失败浪费） |
| 6 | Frontend Bound（指令获取/解码不足） |

### numafast

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 5 |
| `-p <PID>` | 目标进程（逗号分隔） | - |
| `-n <n>` | 展示 Top N 进程数（1-100） | 30 |
| `-t <n>` | 每进程 Top N 线程数（1-10） | 5 |
| `-c <n>` | SPE 采集指令间隔 | 65536 |
| `--package` | 生成 TAR 结果包 | 关闭 |
| `-o <path>` | 报告文件名（不需 .tar 后缀） | 当前路径 |
| `-f` | 输出为文件而非 .tar（需与 `--package` 同用） | 关闭 |
| `-l {0-3}` | 日志级别 | 1 |

### hotspot

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-p <PID>` | 目标进程（逗号分隔，ALL=全系统） | 全系统 |
| `-c <cpu>` | CPU 核列表 | 全部 |
| `-t <n>` | 显示 Top N 热点函数 | 10 |
| `-f <n>` | 采样频率（Hz） | 200 |
| `-r` | 采集范围 user/kernel/all | all |
| `-e` | 采样事件（`hotspot list` 查看可用事件） | cycles |
| `-g` | 开启调用栈采集，生成火焰图 | 关闭 |
| `-s <dir>` | C/C++ 源码项目目录 | - |
| `--dwarf` | 使用 DWARF 调试信息关联源码 | 关闭 |
| `--long-name` | 显示完整函数名 | 关闭 |
| `--package` | 生成 TAR 报告包 | 关闭 |
| `-l {0-3}` | 日志级别 | 1 |

### memory

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-m {1-4}` | 采集模式（1=ALL, 2=Cache, 3=DDR, 4=HBM） | 1 (ALL) |
| `-P {100,1000}` | 采集周期（毫秒） | 1000 |
| `-c <cpu>` | CPU 核列表 | 全部 |
| `-i <sec>` | 采样间隔（秒），生成子报告 | 采集时长 |
| `-l {0-3}` | 日志级别 | 1 |

### miss

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d <sec>` | 采集时长（秒） | 持续直到 Ctrl+C |
| `-p <PID>` | 目标进程（逗号分隔，ALL=全系统） | 全系统 |
| `-c <cpu>` | CPU 核列表 | 全部 |
| `-m {1-4}` | 采集模式（1=LLC, 2=TLB, 3=Remote, 4=Long Latency） | 1 (LLC Miss) |
| `-L <n>` | 长延迟 Load 阈值（cycles），仅 `-m 4` | 64 |
| `-P <n>` | SPE 采样周期（cycles） | 8192 |
| `-t <n>` | 显示 Top N 函数 | 10 |
| `-g` | 开启调用栈采集 | 关闭 |
| `-s <dir>` | C/C++ 源码项目目录 | - |
| `--dwarf` | 使用 DWARF 调试信息关联源码 | 关闭 |
| `--long-name` | 显示完整函数名 | 关闭 |
| `--package` | 生成 TAR 报告包 | 关闭 |
| `-l {0-3}` | 日志级别 | 1 |

## 约束

- 架构：仅 aarch64
- turbostat、top-down、hotspot 和 memory 无 OS/内核版本限制，无虚拟机/容器限制
- numafast 和 miss 需 ARM SPE 硬件支持，不支持虚拟机和容器环境
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`
- `--bmc` 为交互式参数，需手动输入 BMC 凭据，不适用于自动化场景

## 错误处理

- `discover_devkit.sh` 显示 NOT_INSTALLED → 执行 `deploy_devkit.sh` 安装
- `discover_devkit.sh` 显示 TASK_UNAVAILABLE → devkit 已安装但子任务不可用，检查 devkit-tuner 插件
- 采集返回 `LIBPERF` 或 `PmuOpen failed` → PMU 事件不可用，检查 `perf_event_paranoid` 和 `kptr_restrict`
- 采集返回 `SPE function needs to be enabled` → ARM SPE 不可用，numafast 和 miss 无法使用
- memory 返回退出码 0 但输出含 `print failed` 或 `N/A` → PMU 限制导致数据不完整
- 采集返回非零退出码且输出含 `error:` → 环境限制或参数错误，查看输出中的错误信息

## 参考资料

- [turbostat 频率与功耗采集指南](references/turbostat-guide.md)
- [top-down 微架构分析指南](references/topdown-guide.md)
- [numafast NUMA 访存分析指南](references/numafast-guide.md)
- [hotspot 热点函数分析指南](references/hotspot-guide.md)
- [memory 内存带宽与命中率指南](references/memory-guide.md)
- [miss Cache Miss 分析指南](references/miss-guide.md)
