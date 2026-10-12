---
name: kptools-devkit
description: >
  使用华为鲲鹏 DevKit 采集 CPU core/uncore 频率、topdown 微架构指标
  和 NUMA 访存分析。当用户需要 topdown 分析、微架构瓶颈定位、CPU 频率采集、
  core 频率、uncore 频率、流水线瓶颈分析、NUMA 访存分析、NUMA 跨节点访问、
  内存亲和性分析、功耗监控、CPU 功耗、Socket 功耗、温度监控、热点函数、
  火焰图、调用栈、Cache Miss、TLB Miss、远端访问、长延迟 Load、内存带宽、
  Cache 命中率、DDR 带宽、Java 火焰图、Java 热点、Java 性能分析、JVM profiling、
  Java 内存分配、Java 锁竞争、GC 分配分析、鲲鹏 DevKit、devkit 命令时触发。
license: Proprietary
compatibility: aarch64, requires devkit 26.1.RC1
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.1.0"
---

# kptools-devkit — 鲲鹏 DevKit 性能采集

## 工作流程

1. 检查 devkit 是否已部署：`bash scripts/discover_devkit.sh`
2. 如未安装，执行部署：`bash scripts/deploy_devkit.sh`（Java 采集需附带可选包：`bash scripts/deploy_devkit.sh --with java-perf`）
3. 采集数据：`bash scripts/run_devkit.sh tuner <task> -d <sec> [options]`（Java：`bash scripts/run_devkit.sh java-perf hotspot -p <PID> ...`）

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

> 支持 C/C++ 源码关联（`-s` + `--dwarf`）。Java 进程热点请使用 java-perf（见下文"Java 热点分析"小节）。

### Java 热点分析（java-perf）

> 需先部署可选包：`bash scripts/deploy_devkit.sh --with java-perf`（默认最小安装不含）。

```bash
# Java CPU 热点火焰图（attach 运行中的 JVM，免重启），输出到统一证据目录
mkdir -p ./devkit-evidence/$(date +%Y%m%d-%H%M%S)
bash scripts/run_devkit.sh java-perf hotspot -p <PID> -e CPU -d 20 -o $(pwd)/devkit-evidence/<ts>/flame.html

# 内存分配热点（GC 压力定位）
bash scripts/run_devkit.sh java-perf hotspot -p <PID> -e ALLOC -d 20 -o <abs_path>.html

# PMU cycles 视角（需 perf 权限）
bash scripts/run_devkit.sh java-perf hotspot -p <PID> -e CYCLES -d 20 -o <abs_path>.html

# 锁竞争分析（需真实竞争窗口，低竞争会 No data collected）
bash scripts/run_devkit.sh java-perf hotspot -p <PID> -e LOCK -d 30 -o <abs_path>.html
```

输出：自包含交互式 HTML 火焰图（Java 方法名 + 行号）。**必须显式 `-o` 指定绝对路径 `.html` 文件**——默认输出目录（安装目录 data/）在 root 安装 + 非 root 采集场景不可写，相对路径报错，无后缀静默失败。

> `-e CACHE_MISSES` 鲲鹏平台不可用（实测 No data collected），LLC 归因请用 tuner miss。
>
> **火焰图路由**：采集前先判语言——`/proc/PID/cmdline` 含 python → py-spy；exe 为 java 或 `jcmd -l` 含该 PID → java-perf；其余 → tuner hotspot。无语言前缀请求不得直接假设语言。

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

### java-perf hotspot

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标 Java 进程（attach 模式，免重启） | - |
| `-e <event>` | `CPU` \| `ALLOC` \| `CYCLES` \| `LOCK`（CACHE_MISSES 鲲鹏不可用，不承诺） | CPU |
| `-d <sec>` | 采集时长（秒），范围 1-300 | 20 |
| `-i <milli>` | 采样间隔（毫秒），范围 1-1000。**注意方向与 tuner `-f` 相反：↓ 更密** | 10 |
| `-o <path>` | 输出文件（**绝对路径 + .html 后缀**，run_devkit.sh 前置校验） | 安装目录 data/（不可写风险） |

## 约束

- 架构：仅 aarch64
- turbostat、top-down、hotspot 和 memory 无 OS/内核版本限制，无虚拟机/容器限制
- numafast 和 miss 需 ARM SPE 硬件支持，不支持虚拟机和容器环境
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`
- `--bmc` 为交互式参数，需手动输入 BMC 凭据，不适用于自动化场景
- java-perf：需 `deploy_devkit.sh --with java-perf` 部署可选包；采集用户须与目标 JVM 同 uid；JDK 8 由 wrapper 自动适配（注入 tools.jar）；同一时刻仅允许一个 java-perf 采集任务

## 错误处理

- `discover_devkit.sh` 显示 NOT_INSTALLED → 执行 `deploy_devkit.sh` 安装
- `discover_devkit.sh` 显示 TASK_UNAVAILABLE → devkit 已安装但子任务不可用，检查 devkit-tuner 插件
- `discover_devkit.sh` 显示 `java-perf: OPTIONAL_ABSENT` → 可选插件未装，执行 `deploy_devkit.sh --with java-perf`（不影响 tuner 功能）
- `discover_devkit.sh` 显示 `java-perf: JAVA_PERF_PERM` → root 安装 + 非 root 运行，用 `--install-dir ~/.local/share/devkit` 重装到用户目录
- `run_devkit.sh java-perf` 报 `-o must be an absolute path` / `must end with .html` / `target directory ... not writable`（退出码 2）→ 按提示修正 `-o` 参数
- 采集返回 `LIBPERF` 或 `PmuOpen failed` → PMU 事件不可用，检查 `perf_event_paranoid` 和 `kptr_restrict`
- 采集返回 `SPE function needs to be enabled` → ARM SPE 不可用，numafast 和 miss 无法使用
- memory 返回退出码 0 但输出含 `print failed` 或 `N/A` → PMU 限制导致数据不完整
- 采集返回非零退出码且输出含 `error:` → 环境限制或参数错误，查看输出中的错误信息
- java-perf 采集返回 `You do not have permission to perform this operation` → 采集用户与目标 JVM uid 不同，切换同 uid 用户执行
- java-perf 采集返回 `Another task is running` → 已有 java-perf 任务进行中，等待完成或结束后重试
- java-perf 输出 `No data collected` → 采样窗口内无匹配事件（如 LOCK 低竞争），延长 `-d` 或确认负载触发目标事件

## 参考资料

- [turbostat 频率与功耗采集指南](references/turbostat-guide.md)
- [top-down 微架构分析指南](references/topdown-guide.md)
- [numafast NUMA 访存分析指南](references/numafast-guide.md)
- [hotspot 热点函数分析指南](references/hotspot-guide.md)
- [memory 内存带宽与命中率指南](references/memory-guide.md)
- [miss Cache Miss 分析指南](references/miss-guide.md)
- [java-perf Java 热点分析指南](references/java-perf-guide.md)
