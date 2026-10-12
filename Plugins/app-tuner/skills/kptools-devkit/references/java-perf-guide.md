# java-perf — Java 热点分析指南

## 概述

devkit-java-perf 可选插件提供 Java 进程热点函数分析，生成含 Java 方法名和行号的火焰图（Thor profiler + 内嵌 async-profiler，attach 运行中的 JVM，免重启）。

> tuner hotspot 采样的是 native 符号（JVM 进程只见 C 层帧如 `_PyEval_EvalFrameDefault` 类的 interpreter/JIT 内部符号），看不到 Java 方法名。Java 进程热点分析必须用 java-perf。

## 命令

```bash
devkit java-perf hotspot [-p <PID>] [-e {CPU,ALLOC,CYCLES,CACHE_MISSES,LOCK}] [-d <sec>] [-i <milli>] [-o <path>]
```

经 SKILL 包装器调用（`-o` 有前置校验）：

```bash
bash scripts/run_devkit.sh java-perf hotspot -p <PID> -e CPU -d 20 -o <abs_path>.html
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p, --pid <PID>` | 目标 Java 进程 PID（attach 模式，免重启） | - |
| `-e, --event` | `CPU` \| `ALLOC` \| `CYCLES` \| `LOCK`（CACHE_MISSES 鲲鹏不可用，不承诺） | CPU |
| `-d, --duration <sec>` | 采集时长（秒），范围 1-300 | 20 |
| `-i, --interval <milli>` | 采样间隔（毫秒），范围 1-1000 | 10 |
| `-o, --output <path>` | 输出路径（**绝对路径 `.html` 文件**，run_devkit.sh 前置校验） | `<devkit_home>/java-perf/data/` |

## 事件类型选择

| 事件 | 用途 | 说明 |
|------|------|------|
| `CPU` | 默认热点分析 | Java 方法级 CPU 占用，最常用 |
| `ALLOC` | GC 压力/分配热点定位 | 分配火焰图，含分配点栈帧 |
| `CYCLES` | PMU cycles 视角 | 需 perf 权限（`perf_event_paranoid`） |
| `LOCK` | 锁竞争分析 | 需真实竞争窗口；低竞争输出 `No data collected`（预期行为），建议延长 `-d` |
| `CACHE_MISSES` | **鲲鹏平台不可用** | 实测始终 No data（async-profiler 事件在 aarch64 缺少映射）；LLC 归因改用 `tuner miss`（native 侧）或 perf |

## 输出解读

自包含交互式 HTML（~120KB，含搜索/排序/翻转功能），浏览器打开：

| 要素 | 含义 |
|------|------|
| 帧标签 | Java 方法名（含类名）+ 行号 |
| 帧宽度 | 采样占比（越宽占用越高） |
| 深度 | 调用栈层级 |

> **已知限制**：java-perf 无机器可解析的原始输出格式（火焰图数据内嵌于 HTML，无官方 schema）。agent 可直接读 HTML 做定性/半定量分析；禁止依赖 HTML 内部结构做自动解析（非契约接口，升级即碎）。

## 约束

| 约束 | 说明 |
|------|------|
| 架构 | 仅 aarch64 |
| JDK | 任意版本（JDK 8 由 wrapper 自动注入 tools.jar；JDK 21 实测通过） |
| attach 权限 | 采集用户须与目标 JVM 同 uid（跨 uid 报 `You do not have permission to perform this operation`） |
| 安装目录写权限 | wrapper/cli-backend 需写 `java-perf/log/` 和 `config/`——root 装到 /opt/devkit 时非 root 用户无法运行，用 `deploy_devkit.sh --install-dir ~/.local/share/devkit` 装到用户目录 |
| 输出目录写权限 | 默认输出到安装目录 `java-perf/data/`，"root 安装 + 非 root 采集"场景不可写——**必须显式 `-o` 指定用户可写的绝对路径**（run_devkit.sh 前置校验拦截） |
| 并发限制 | 同一时刻仅允许一个采集任务，冲突报 `Another task is running` |
| 参数转义 | 参数含引号被 wrapper 拒绝 |
| agent 生命周期 | thor-agent + libasyncProfiler.so 首次 attach 后常驻目标 JVM（idle 复用，不重复加载），停止采集不卸载 |
| 与 tuner 差异 | 无 `--package` TAR 打包、无 `devkit report` 集成、无源码关联（`-s`/`--dwarf`） |

## 与 tuner hotspot 的协作

- Java 进程热点 → java-perf（Java 方法名）
- Java 进程的 JNI/native 部分（如 JNI 库、GC 线程、JIT 编译线程）→ 可叠加 `tuner hotspot`（native 视角）
- JVM 内 LLC miss 归因 → `tuner miss`（java-perf CACHE_MISSES 不可用）

## 三工具参数语义对照（防混用）

| 语义 | tuner hotspot | java-perf | py-spy |
|------|--------------|-----------|--------|
| 采样密度 | `-f 400`（频率 Hz，**↑ 更密**） | `-i 5`（间隔 ms，**↓ 更密**） | `-r 200`（频率 Hz，↑ 更密） |
| 火焰图开关 | 需显式 `-g` | 默认输出 | 默认输出 |
| `-i` 参数 | 子报告间隔（秒） | 采样间隔（毫秒） | 不存在（用 `-r`） |
| 输出格式 | HTML + callstack log | 仅 HTML | SVG/speedscope/raw |

**统一证据目录**：所有火焰图采集显式 `-o` 输出到 `./devkit-evidence/{时间戳}/`，不依赖工具默认路径。

## 错误处理

| 现象 | 原因 | 处理 |
|------|------|------|
| `run_devkit.sh` 退出码 2：`-o must be an absolute path` | 相对路径（ThorCliApp 异常） | 改用绝对路径 |
| 退出码 2：`-o must end with .html` | 无后缀会静默失败（无文件无报错） | 补 `.html` 后缀 |
| 退出码 2：`target directory does not exist or is not writable` | 目录不存在/不可写 | `mkdir -p` 创建证据目录或换可写目录 |
| `The current process collected is not a Java process` | 目标非 Java 进程 | 确认 PID；C/C++ 进程改用 tuner hotspot |
| `You do not have permission to perform this operation` | 跨 uid attach | 与 JVM 同 uid 用户执行 |
| `Another task is running` | 并发采集冲突 | 等待当前任务结束 |
| `No data collected` | 低竞争（LOCK）或事件不可用（CACHE_MISSES） | 延长 `-d`；CACHE_MISSES 改用 tuner miss |

## 示例

```bash
# 部署（Java 场景一次性）
bash scripts/deploy_devkit.sh --with java-perf

# CPU 热点火焰图，输出到统一证据目录
mkdir -p ./devkit-evidence/$(date +%Y%m%d-%H%M%S)
bash scripts/run_devkit.sh java-perf hotspot -p 12345 -e CPU -d 20 -o $(pwd)/devkit-evidence/<ts>/flame.html

# GC 压力定位：分配火焰图
bash scripts/run_devkit.sh java-perf hotspot -p 12345 -e ALLOC -d 20 -o /tmp/alloc.html

# 锁竞争定位（延长窗口）
bash scripts/run_devkit.sh java-perf hotspot -p 12345 -e LOCK -d 30 -o /tmp/lock.html
```
