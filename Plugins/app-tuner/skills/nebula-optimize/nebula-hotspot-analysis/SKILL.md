---
name: nebula-hotspot-analysis
description: >
  NebulaGraph 3.8.0 热点分析与 profiling：按场景（Go1/Go2/Go3、FindShortestPath、Insert）
  采集 perf 火焰图、on/off-CPU、Topdown 微架构指标、HTTP /stats 时间分解，结合线程池
  （graphd netio/worker，storaged IOThreadPool/executor-pri3/reader-pool）与 folly Future 模型做归因；
  覆盖 futex 队列争用、RPC 序列化、内存分配、Value 字符串、BFS 容器、RocksDB 扫描、
  schema 拷贝、nGQL 解析、WAL fsync/compaction 等热点，并给出瓶颈迁移与优化方向。
  用于回答“慢在哪、为什么、证据是什么”；不修改代码/配置，不执行部署与压测
  （分别交给 nebula-build-deployment / nebula-test-verification / nebula-performance-optimization）。
  触发词：Nebula 热点、Nebula profiling、Nebula 火焰图、perf、topdown、futex、
  RocksDB 热点、graphd 慢、storaged 慢、P99 高、CPU 利用率低、瓶颈定位。
license: MulanPSL-2.0
compatibility: NebulaGraph 3.8.0；perf/FlameGraph、devkit topdown（如可用）、HTTP /stats；鲲鹏 950 / aarch64、openEuler 24.03 实测，x86 可复用方法
metadata:
  skill_type: tool_assisted
  category: diagnosis
  domain: graph_database
  version: "1.0.0"
---

# NebulaGraph 热点分析

## 简介

目标不是“贴一张火焰图”，而是给出一条可复核的因果链：

```
场景/负载 → 瓶颈进程/线程 → 热点函数/阶段 → 机制（锁/RPC/内存/IO/算法）→ 证据 → 优化方向
```

本 skill 覆盖三层证据：

1. **系统/线程层**：CPU 利用率、线程池分布、futex、上下文切换、IO。
2. **函数层**：perf/flamegraph 的 self/children 热点、调用路径聚合。
3. **微架构/阶段层**：Topdown（Front-End/Back-End/Memory Bound）、IPC、cache miss、
   HTTP `/stats` 的 RPC/处理/排队时间分解。

## 何时触发

- QPS 上不去、CPU 利用率异常（过高/过低）、P99 长尾、场景切换后性能变化。
- 要确认瓶颈在 graphd、storaged、metad 还是 I/O/客户端。
- 优化前后要解释“收益来自哪里”，或负收益要定位原因。
- 需要为某个场景建立热点基线（Go1/2/3、FSP、Insert）。
- 怀疑 futex、RPC 序列化、内存分配、RocksDB、schema 拷贝、BFS、nGQL 解析。

**不适用**：部署/编译 → `nebula-build-deployment`；压测口径与 A/B 判定 →
`nebula-test-verification`；改代码/调参 → `nebula-performance-optimization`；
OS/BIOS/网卡/绑核等系统层 → `kpbot-app-tuner` 的对应 subskill。

> 交接说明：上述三个姊妹 skill 与本 skill 同属 nebula-optimize 领域分组，由同一系列
> PR 交付（源码位于 `Plugins/app-tuner/skills/nebula-optimize/`，`install.sh` 会把五个
> 成员平铺安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 5 个
> PR 的 `nebula-optimize/DOMAIN.md` 合入后生效；在此之前 install.sh 对缺 DOMAIN.md 的
> 领域分组按设计整组跳过，入口暂不可用属预期窗口期）。若当前环境找不到对应
> 入口（未安装/未合入），不要凭空继续部署、压测或改码：把本 skill 已产出的证据包
> （perf.data/folded、pidstat/mpstat、/stats 采样、窗口与结论）连同待办（部署需求、
> 压测计划、候选改动清单）交回维护者，由其安装对应 skill 或人工继续。

## 采集前先固定场景

没有固定场景的热点没有意义。记录：

- 场景与语句（Go1/2/3、FSP、Insert，是否 LIMIT）；
- VU/duration、k6 进程数与绑核；
- 部署形态、进程数、线程参数、绑核/NUMA；
- 数据集、Space/分区/副本；
- 二进制 md5/commit、third-party/jemalloc 版本。

同时记录 QPS、成功率、p50/p99、各进程 CPU，作为热点解释的对照。

## 采集速查

```bash
# 1) 进程/线程分布（按 PID 枚举，不要整机 grep——io 子串会命中内核/iou 线程，nebula 排到 200 行外）
ps -Lo pid,tid,comm,psr,pcpu -p "${PID}"

# 2) perf 采样（按进程；采样窗口覆盖稳态；perf -p 多 PID 须逗号分隔，pidof 输出空格分隔）
perf record -F 999 -g -p "$(pidof nebula-graphd | tr ' ' ',')" -- sleep 20
perf report -g graph,0.5,callee --stdio | head -80

# 3) 火焰图
perf script | FlameGraph/stackcollapse-perf.pl | FlameGraph/flamegraph.pl > graphd.svg

# 4) off-CPU（锁/IO 等待）：按 PID 累计离核时长（单位 us），勿用 sched_switch 事件次数占比
/usr/share/bcc/tools/offcputime -p "$(pidof nebula-storaged | awk '{print $1}')" 20 > storaged_offcpu.txt

# 5) Topdown（若平台支持）
# 使用 devkit 或 perf stat 的 topdown 指标，采集代表核 8–20s

# 6) HTTP 统计
curl -s http://127.0.0.1:19669/stats > graphd_stats.txt
curl -s http://127.0.0.1:19779/stats > storaged_stats.txt

# 7) CPU/IO 对照（pidstat -p 多 PID 用逗号分隔；pidof 输出空格分隔须先转换）
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "$(pidof nebula-graphd | tr ' ' ','),$(pidof nebula-storaged | tr ' ' ',')" 1 30 > pidstat.txt
```

采集纪律：

- 先预热再采样，窗口覆盖稳态；采样期间不要改配置/数据。
- 多实例时分别采 graphd 和 storaged，或选代表实例；记录采的是哪个 PID/核。
- `perf` 采样用 `--call-graph dwarf`（`-g` 只是调用栈缩写、不接收展开方式参数，写
  `-g dwarf` 会把 dwarf 当待执行命令）；dwarf 模式需要二进制带调试信息——编译加 `-g`
  且不要 strip（或保留独立的 debug 符号包）；`[unknown]` 过高时先补符号。
- 火焰图看 self 与 children 两种视图，避免把“等待”误判为“计算”。

## 场景与瓶颈速查

| 场景 | 主要瓶颈位置 | 典型热点 | 备注 |
|------|-------------|---------|------|
| Go1Step | graphd | Thrift 队列 futex、RPC 序列化、内存分配 | CPU 高、QPS 高；排队开销主导 |
| Go2Step | storaged | RocksDB 迭代/比较、GetNeighbors 组装、内存分配、schema 拷贝 | graphd 多在等 RPC |
| Go3Step | storaged | RocksDB 扫描、Value/结果组装、内存分配 | 扇出大，RPC 与扫描放大 |
| FindShortestPath | graphd | BFS 容器（hash/insert）、Value 字符串、内存分配 | 算法/容器结构主导 |
| InsertPerson | storaged + I/O | nGQL 解析、WAL fsync、RocksDB WriteThread/compaction | CPU 常不饱和，延迟受 I/O |

迁移规律：

- 轻查询：futex 队列争用 → 增加 graphd 实例/减少单队列 worker。
- 多跳：futex 下降，内存分配/RPC 等待/ RocksDB 扫描上升。
- 路径：Value 字符串 + BFS 容器。
- 写入：解析 + WAL + compaction，属 I/O/解析型。

详细百分比与场景表见 `references/scenario-hotspots.md`。

## 归因规则

1. **先分进程**：graphd 与 storaged 的 CPU/延迟是否同步？谁先饱和？
2. **再分线程池**：graphd `executor-pri3-*`/`graph-netio`；storaged `executor-pri3-*`（与 graphd 同名，按 PID 区分）/`reader-pool-pri3-*`/`IOThreadPool<N>`——各占多少？热点是否集中在某一池？
3. **再分机制**：
   - futex/锁：`try_dequeue`、`waitOnTask`、`ThreadManager::add`、`pthread_mutex_*`；
   - 内存：`malloc`、`sdallocx`、`tcache_*`、`Value` 构造/析构；
   - 网络/RPC：`sendmsg/recv`、序列化、Future/RequestContext；
   - 存储：RocksDB 迭代/比较/Block Cache、MemTable/SST、compaction；
   - 算法：BFS 容器、GetNeighbors 组装、路径拼接；
   - 写入：WAL fsync、Raft、WriteThread、compaction。
4. **用对照验证**：换部署/换 VU/加 LIMIT/改一个参数，看热点是否按预期迁移。
5. **区分事实与推断**：火焰图占比是事实；“因为 futex 所以慢”需要对照实验或额外证据。

## 常见误判

| 误判 | 纠正 |
|------|------|
| “graphd CPU 低，所以 graphd 不是瓶颈” | 可能在等 RPC；看延迟、连接、storaged |
| “worker 越多越好” | 轻查询高 QPS 时单队列 futex 争用会反噬 |
| “`[unknown]` 是无效数据” | 多为内核/vdso（sendmsg/recv/futex），需结合上下文拆解 |
| “self 低就不重要” | children 占比可能很高；要看调用路径聚合 |
| “CPU 100% 就是计算瓶颈” | 可能大量时间在 futex/自旋/内存分配 |
| “Go2 比 Go1 慢是因为计算多” | 很多是串行 RPC 等待与结果解码 |
| “Topdown Memory Bound 就要换内存” | 可能是 cache miss/访存模式/数据结构问题 |
| “压测数字低就是服务端慢” | 先排除客户端 CPU/解码瓶颈 |

## 输出契约

```
## 热点分析报告

### 场景与身份
- 场景/语句/是否 LIMIT、VU/时长、k6 数
- 部署形态、进程数、线程参数、绑核
- 数据集/Space/分区、二进制 md5/commit

### 性能对照
- QPS、成功率、p50/p99、各进程 CPU

### 线程/阶段分解
- graphd: netio/worker 占比；storaged: reader-pool/IOThreadPool/executor（与 graphd 同名，按 PID 区分）占比
- RPC 次数、队列、内存、IO（有数据则列）

### Top 热点
| 排名 | self% | children% | 函数 | 线程池 | 机制 | 证据 |

### 归因
- 瓶颈进程/线程 → 机制 → 为什么（对照/实验）
- 与性能数字的对应关系

### 优化方向
- 按“部署 / 配置 / 编译 / 源码 / 第三方”分类，给预期收益与验证方式
- 明确哪些是假设，哪些已被对照验证
```

## 参考资料与脚本

| 文件 | 何时读 |
|------|--------|
| `references/profiling-playbook.md` | 采集命令、符号、火焰图、off-CPU、Topdown、HTTP stats、采样窗口与注意事项 |
| `references/scenario-hotspots.md` | 五场景热点表、graphd/storaged 分类占比、瓶颈迁移、x86/ARM 差异、扩展实验 |
| `references/thread-folly-model.md` | 线程池、Future/Promise、Thrift 队列、四次序列化、DAG，解释热点为何出现 |
| `references/stats-topdown.md` | HTTP 指标、时间消耗分解、Topdown 指标、IPC/cache miss、优化优先级 |
| `scripts/collect_profile.sh` | 只读采集：默认 dry-run 打印计划；`--execute` 采 perf/火焰图/pidstat/mpstat/ss/stats |
