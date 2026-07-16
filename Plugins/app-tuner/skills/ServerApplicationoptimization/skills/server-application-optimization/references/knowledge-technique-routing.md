# Knowledge Technique Routing

本文件把知识库 `/Users/a111/knowledge/20_Knowledge/Performance/Server-Performance-Optimization` 中的技术层和案例索引映射到当前子 skill。使用时只按命中的瓶颈读取相关知识库文件；不要把历史案例当作当前结论。

## Routing Map

| 知识库层级/技术 | 触发信号 | 子 skill |
|---|---|---|
| L1 BIOS/OS: Performance Mode、Cache Mode、DIE Interleaving、Hardware Prefetch | BIOS 未高性能、跨 NUMA 访存、预取/缓存模式影响 PMU | `bios-optimization`、`os-optimization` |
| L1 BIOS/OS: NUMA Balancing、Affinity Optimization、Scheduler Parameter Tuning、Transparent Huge Pages | NUMA miss、高迁移、调度抖动、THP 影响延迟 | `cpu-affinity-optimization`、`os-optimization` |
| L1 BIOS/OS: Network Parameter Tuning | softirq、IRQ、RPS/XPS、drops、retrans、远程压测网络疑点 | `network-optimization` |
| L1 BIOS/OS: Linux Kernel Parameters | sysctl、内核参数、补丁缺失、容器/VM 能力限制 | `os-optimization` |
| L2 Application Config: MySQL/PostgreSQL/Flink/Async/Tokenizer/Prefix Cache | 应用内部状态能解释瓶颈 | `application-config-optimization`，按需 `database-workload-analysis` |
| L3 Performance Libraries: libc string、BiSheng Stringlibs、jemalloc、tcmalloc、KQMalloc | 热点落在 malloc/free、memcpy/memset/string、allocator 或高性能库可替换 | `performance-library-selection`，按需 `compiler-optimization` |
| L4 Compiler: Architecture Flags、CRC and LSE、GCC/LLVM/BiSheng、PGO、LTO、Function Alignment、Loop Vectorization、MySQL Patch | 编译选项、代码生成、profile-guided、平台编译器或源码补丁相关 | `compiler-optimization` |
| L5 Source Code: Zero Copy and Batching、Data Structure Alignment/Slimming、Operator Fusion、Scheduling、Deep Copy to Shallow Copy | 热点需要源码/算法/数据结构配套，不是单纯系统参数 | `other-optimization`，按需 `application-config-optimization` 或 `compiler-optimization` |
| L6 Microarchitecture: Software Prefetch、HHA DDR、L2/L3 Cache Isolation | PMU 指向 cache、DDR、frontend/backend、微架构资源隔离 | `cpu-affinity-optimization`、`os-optimization`、`bios-optimization` |
| Foundations: PMU、NUMA、CPU Affinity | 证据采集、瓶颈归因和拓扑解释 | 主流程环境诊断、`cpu-affinity-optimization`、相关子 skill |
| Cases: Optimization Case Source Index | 用户确认可参考历史案例时 | 只作为候选启发，不替代本轮证据 |

## Usage Rules

- 知识库案例只能提供候选方向、预期收益范围和验证指标；当前报告收益必须来自本轮 A/B。
- 若用户未确认使用历史记录，不能根据历史案例继续调优。
- 每个子 skill 输出候选动作时，应引用命中的知识库技术名、当前证据和本轮验证计划。
- 若当前环境缺少 perf/PMU、root 权限、容器/VM 映射或系统命令，应先标记降级，再决定是否还能使用对应知识库方法。

## Evidence To Technique Hints

| 证据 | 优先查阅 |
|---|---|
| Frontend Bound、I-cache/ITLB、branch miss | L4 Function Alignment、PGO、LTO、LLVM Compiler |
| 原子/锁热点、LL/SC 重试、mutex 扩展差 | L4 CRC and LSE Instructions、Architecture Flags |
| CRC/checksum 软件热点 | L4 CRC and LSE Instructions、Architecture Flags、MySQL Patch Optimization |
| malloc/free、内存分配锁 | L3 jemalloc、tcmalloc、KQMalloc |
| memcpy/memset/string 热点 | L3 libc Memory String Functions、BiSheng Stringlibs |
| softirq/NAPI/netfilter/TCP 热点 | L1 Network Parameter Tuning |
| NUMA miss、远端内存、线程迁移 | Foundations NUMA、CPU Affinity、L1 NUMA Balancing |
| cache miss 或 DDR bound | L6 HHA DDR Tuning、L2/L3 Cache Isolation、Software Prefetch |
| 高层应用锁、连接池、buffer/cache | L2 Application Config 对应应用文件 |
