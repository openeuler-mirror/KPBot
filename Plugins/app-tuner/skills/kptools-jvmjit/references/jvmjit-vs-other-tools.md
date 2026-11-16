# JVM JIT 采集与其他 Java/语言分析工具分工

自包含对比，路由判断不依赖其他 skill 文档。

> 注：kptools-arthas 为本仓库**在途 PR #139**（当前 master 未合入）。合入前，"免重启方法级
> 交互分析"诉求指向**外部工具 arthas**（https://github.com/alibaba/arthas），非本仓库 skill；
> 其余路由目标（kptools-devkit java-perf / kptools-py-spy / kptools-nmon）均已在本仓库存在。

## 工具路由表

| 用户诉求 | 推荐 | 理由 |
|----------|------|------|
| Java 进程方法级耗时分解、入参/返回值、锁持有者（免重启） | kptools-arthas（在途 PR #139，master 未合入；合入前用外部工具 arthas） | attach 式交互诊断，无需部署期规划 |
| Java 采样火焰图全景（CPU/ALLOC/LOCK） | kptools-devkit java-perf | 鲲平台 aarch64 一等支持 |
| **perf 下 Java 符号缺失、JIT 反汇编、指令级热点、容器内 Java 采集** | **本 Skill（kptools-jvmjit）** | 唯一给出 JIT 代码机器码级证据的路径 |
| Python 进程热点 | kptools-py-spy | 语言路由 |
| 系统整体 CPU/内存/IO 趋势 | kptools-nmon | 系统级采集 |

## 能力对比

| 维度 | kptools-jvmjit | arthas | devkit java-perf |
|------|----------------|--------|------------------|
| 接入方式 | **启动期** `-agentpath`（需重启规划） | 运行期 attach（免重启） | 运行期 attach |
| 粒度 | 方法级 + **JIT 指令级（机器码）** | 方法级/调用链 | 方法级火焰图 |
| 产物 | 符号化 perf.data + jitted ELF | 文本证据 | 火焰图 |
| 容器支持 | 容器内执行链（实测） | docker exec 后 attach | 支持 |
| 独有价值 | 向量化/内联判定、优化前后代码结构对比、`[JIT]` 裸地址修复 | 观察运行时行为（参数/锁/GC） | 全景趋势 |

## 何时选本 Skill 而不是 arthas

- 要回答"热点代码**长什么样**"（指令/向量化/内联）→ 本 Skill（arthas 只给耗时数字）
- 已有 perf.data 采自 Java 进程但符号全是 unknown → 本 Skill（前提：JVM 当时带过 agent）
- 目标在容器内且只允许 distro 工具链（不能带第三方 jar）→ 本 Skill（全链 perf 自带）

## 何时选 arthas 而不是本 Skill

- 进程正在生产运行、不能重启、要看方法耗时/参数 → arthas（本 Skill 的 agent 无法补加）
- 只需要快速定位"哪个方法慢" → arthas trace 更直接

## 相关替代物（知道即可，非本仓库 skill）

- **async-profiler**：免启动期 agent（可 attach），方法级火焰图；不产 jitted ELF 指令级视图
- **`jcmd <pid> PerfMap.dump`**（JDK 21+）：生成 `/tmp/perf-<pid>.map` 符号映射，
  让普通 perf report 显示方法名；**不含机器码**，无指令级下钻能力——轻量场景可先用它
- **JITWatch**：需要 `-XX:+PrintCompilation`/hsdis 配套做离线深挖，运维成本高
