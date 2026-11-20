# 与 java-perf / py-spy 分工（kptools-arthas）

> 本仓库 Java/Python 进程性能分析工具的选型依据。原则：**采样型工具回答"热点在哪"，
> Arthas 回答"为什么慢"**；两者互补，不互替。

## 1. 问题 → 工具决策表

| 问题 | 工具 | 理由 |
|------|------|------|
| Java 进程 CPU/内存分配/锁的**热点全景** | kptools-devkit java-perf | 采样火焰图，一次采集看全貌（aarch64） |
| 某方法**耗时落在哪一段子调用** | kptools-arthas（trace） | 逐段耗时分解，采样做不到 |
| **入参/返回值**是什么导致慢 | kptools-arthas（watch） | 采样看不到数据 |
| **谁持锁**导致大量线程阻塞 | kptools-arthas（thread -b） | 采样的 LOCK 事件只给热点不给因果 |
| 接口 QPS/RT/失败率的**运行时统计** | kptools-arthas（monitor） | 量化劣化幅度与抖动 |
| Python 进程热点 | kptools-py-spy | 语言路由 |
| Java 进程 JNI/native 部分（GC/JIT 线程）热点 | kptools-devkit tuner hotspot | native 符号视角 |

**推荐顺序**（Java 性能分析）：先 java-perf 采样定位热点方法（在哪）→ 再 arthas trace/watch 深挖该热点（为什么）→ 证据移交后续优化。没有 java-perf（x86_64 环境 / 未装 devkit）时，arthas `thread`/`profiler` 可承担热点定位。

## 2. 能力对照

| 维度 | kptools-devkit java-perf | kptools-arthas | kptools-py-spy |
|------|--------------------------|----------------|----------------|
| 机制 | 采样（attach 运行中 JVM） | attach + 命令式观测（只读性能分析） | 采样（ptrace） |
| 核心输出 | CPU/ALLOC/LOCK/CYCLES 火焰图（HTML） | trace/watch/thread/monitor/tt 等纯文本 | 火焰图（SVG/raw） |
| 回答的问题 | 热点在哪 | 为什么慢（耗时段/参数/持锁者） | 热点在哪 |
| 机器可解析 | 否（HTML 无契约，禁依赖内部结构） | **是**（纯文本） | 是（raw/speedscope） |
| 架构 | 仅 aarch64 | x86_64 + aarch64 | 两者 |
| 观测开销 | 采样开销 | 增强类命令有开销（须 -n 限次） | 采样开销（短暂暂停） |
| 权限 | 同 uid attach | 同 uid attach | ptrace（yama） |
| 交互模式 | 非交互 | 批处理 `-b -c` / telnet | 非交互 |

## 3. 参数语义对照（防混用，四工具）

| 语义 | tuner hotspot | java-perf | py-spy | arthas |
|------|--------------|-----------|--------|--------|
| 采样密度 | `-f 400`（Hz，**↑ 更密**） | `-i 5`（ms，**↓ 更密**） | `-r 200`（Hz，↑ 更密） | 非采样型；`-n` 为**采集次数上限**（trace/watch/stack/tt） |
| `-n` 含义 | 不存在 | 不存在 | 不存在 | 次数限制（增强开销护栏） |
| `-i` 含义 | 子报告间隔（秒） | 采样间隔（毫秒） | 不存在（用 -r） | dashboard 刷新间隔（ms） |
| `-c` 含义 | 不存在 | 不存在 | 不存在 | collect.sh 命令串；arthas-boot 中为单条命令 |
| 窗口控制 | `-d <sec>` | `-d <sec>`（1-300） | `-d <sec>` | monitor `-c <sec>` 统计窗口；trace/watch 用 `-n` 控制总量 |
| 火焰图 | 需显式 `-g` | 默认输出 | 默认输出 | `profiler stop --file <abs>.html` |
| 输出格式 | HTML + callstack log | 仅 HTML（绝对路径 .html） | SVG/speedscope/raw | 纯文本（可 grep/解析） |

## 4. aarch64 / 鲲鹏平台建议

- arthas 批处理性能分析（dashboard/thread/trace/watch/shutdown）与 `profiler` CPU 事件已在 aarch64 + JDK 17 + arthas 4.3.5 实测通过
- 火焰图全景仍建议 java-perf 优先（ALLOC 事件、与 devkit 工具链联动）；arthas profiler 作为无 devkit 环境的替代路径
- java-perf 的 `CACHE_MISSES` 在鲲鹏不可用（java-perf-guide 已标注）：LLC 归因用 tuner miss，行为分析用 arthas trace
- 生产增强命令开销护栏在鲲鹏多核环境同样适用：`-n` 限次、单方法、短窗口
