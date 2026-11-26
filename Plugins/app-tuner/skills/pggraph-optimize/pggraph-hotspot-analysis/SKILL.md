---
name: pggraph-hotspot-analysis
description: >
  pgGraph 0.1.8 热点分析与 profiling：按查询场景（component_stats、1-hop/2-hop traverse、
  search、shortest_path、GQL、build/load、并发吞吐）采集 perf/flamegraph、on/off-CPU、
  Topdown/PMU、`pg_stat_activity`、`pg_stat_statements`、`EXPLAIN (ANALYZE, BUFFERS)`、
  backend 进程的 `proc` `smaps_rollup` 与 NUMA/CCL 指标；结合“每 backend 一个 thread_local Engine、
  前向 CSR mmap 共享、反向 CSR/FilterIndex 私有堆、SPI 串行、Python 客户端 GIL”做归因；
  覆盖 UnionFind、node_deleted、String 分配/排序、CSR 随机访问、hydrate/SPI、加载/CRC、
  缓存争用与客户端瓶颈。用于回答“慢在哪、为什么、证据是什么”；不修改代码/配置，
  不执行部署与压测（分别交给 pggraph-build-deployment / pggraph-test-verification /
  pggraph-performance-optimization）。触发词：pgGraph 热点、pgGraph profiling、pgGraph 火焰图、
  perf、topdown、pg_stat_activity、component_stats 慢、traverse 慢、CPU 利用率低、QPS 上不去。
license: MulanPSL-2.0
compatibility: pgGraph 0.1.8；PostgreSQL 18.3；perf/FlameGraph、devkit topdown（如可用）、pg_stat_statements；aarch64/鲲鹏与 x86_64 方法通用
metadata:
  skill_type: tool_assisted
  category: diagnosis
  domain: graph_database
  version: "1.0.0"
---

# pgGraph 热点分析

## 简介

目标不是“贴一张火焰图”，而是给出一条可复核的因果链：

```
场景/负载 → 瓶颈进程/线程 → 热点函数/阶段 → 机制（计算/访存/锁/SPI/客户端）→ 证据 → 优化方向
```

pgGraph 的特殊性：

- 每个 PostgreSQL backend 一个 `thread_local Engine`，没有跨 backend 的共享 Rust 堆。
- 前向 CSR/NodeStore/Resolution 主段是 mmap 共享；反向 CSR/FilterIndex 是每 backend 私有堆。
- 单个 backend 内 SPI 串行；多 backend 并发的瓶颈可能不在图算法，而在缓存/客户端/连接数。
- 同一组现象可能有两种完全不同的根因（客户端 GIL vs 服务端缓存争用），必须用证据判别。

## 何时触发

- 已有**可采信基线**（同口径协议见 `pggraph-test-verification`）前提下的归因诉求：QPS 上不去、CPU 利用率异常（过高/过低）、P99 长尾、场景切换后性能变化——尚未建立可采信基线（数字口径/预热/轮数未核）时，先回 `pggraph-test-verification` 建基线。
- 要确认瓶颈在 pgGraph Rust 代码、PostgreSQL/SPI、客户端还是系统层。
- 优化前后要解释“收益来自哪里”，或负收益要定位原因。
- 需要为某个场景建立热点基线（component/traverse/search/path/GQL/build）。
- 高并发下 CPU 不饱和、backend 大量空闲、IPC 下降、LLC miss 升高。

**不适用**：部署/编译 → `pggraph-build-deployment`；压测口径与 A/B 判定 →
`pggraph-test-verification`；改代码/调参 → `pggraph-performance-optimization`；
OS/BIOS/网卡/绑核等系统层 → `kpbot-app-tuner` 的对应 subskill。

> 交接说明：上述姊妹 skill 与本 skill 同属 pggraph-optimize 领域分组，由同一系列 PR 交付
> （源码位于 `Plugins/app-tuner/skills/pggraph-optimize/`，`install.sh` 会把五个成员平铺
> 安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 1 个 PR 交付的
> `pggraph-optimize/DOMAIN.md`（install.sh 依赖它识别分组）合入后生效；此前 install.sh
> 对缺 DOMAIN.md 的领域分组按设计整组跳过，已合入成员暂不安装，属预期窗口期）。若当前环境找不到对应入口
> （未安装/未合入），不要凭空继续部署、压测或改码：把本 skill 已产出的证据包
> （perf.data/report、pg_stat 快照、window.txt 与结论）连同待办交回维护者，由其安装
> 对应 skill 或人工继续。

## 采集前先固定场景

没有固定场景的热点没有意义。记录：

- 查询名与完整 SQL（seed、limit、max_rows、hydrate、direction、edge_types）；
- 数据集与规模（`metadata.json`/`graph.status()`/`build_output`）；
- build 模式（csr_readonly/mutable_overlay）、当前图、是否有 pending sync；
- 连接数/客户端模式（threading/`--mp`/异步）、绑核；
- PostgreSQL 配置（shared_buffers/work_mem/jit/max_connections）；
- pgGraph 版本/commit、`.so` SHA256、Rust/pgrx；
- 工件大小、每 backend 私有内存（如已测）。

## 证据分层

| 层 | 工具 | 回答 |
|----|------|------|
| 服务端总览 | `pg_stat_activity`、`pg_stat_statements` | 哪些 backend 活跃/等待/ClientRead；哪条 SQL 耗时 |
| 进程/线程 | `pidstat`、`mpstat`、`/proc/<pid>/stat` | 服务端/客户端各用多少 CPU；是否单核打满 |
| 函数 | `perf record -g`、FlameGraph | Rust/PG 热点函数、调用栈 |
| 微架构 | `perf stat`/Topdown/PMU | IPC、backend stall、L1/L2/L3、DRAM、TLB |
| 内存 | `/proc/<pid>/smaps`、`numastat` | 共享 mmap vs 私有堆；是否跨 NUMA |
| SQL 内部 | `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` | SQL 包装层/hydrate 的计划与页访问 |
| Rust 符号 | `perf` + debuginfo/`graph.so` | 函数名是否可读；release 优化是否内联 |

## 参考索引

| 文件 | 何时读 |
|------|--------|
| `references/profiling-playbook.md` | 采集命令、perf/FlameGraph、pg_stat_activity/stat_statements、smaps、符号与采样 |
| `references/scenario-hotspots.md` | component/traverse/search/path/GQL/build/load 的已知热点与源码位置 |
| `references/thread-process-model.md` | backend/Engine/SPI/后台 worker/连接池/线程本地状态 |
| `references/topdown-and-contention.md` | PMU/Topdown、缓存争用、GIL、NUMA/CCL、两种根因的判别 |
| `scripts/collect_profile.sh` | dry-run/采集包装：记录环境、perf、pg_stat_activity、smaps |

## 快速判别表

| 现象 | 更像 | 下一步 |
|------|------|--------|
| 客户端 Python CPU ~1 核，backend 大量 `ClientRead`，QPS 平台，min 延迟不增 | 客户端 GIL/提交速率 | `--mp`/异步/多进程复测 |
| 客户端 CPU 不高，backend 高活跃，LLC miss/后端 stall 高，每 backend 私有内存大 | 服务端缓存/私有内存争用 | 降并发/连接池；查反向 CSR/FilterIndex；考虑共享化 |
| 单查询 CPU 高，flamegraph 集中在 `UnionFind::find`/排序/String 分配 | 算法/后处理 | 看 `scenario-hotspots`，做单变量优化 |
| `hydrate=true` 明显慢于 `hydrate=false` | SPI/回查/JSON | 关闭 hydrate、批量/缓存、减少列 |
| 首次查询慢、后续快 | 加载/CRC/反向 CSR/预热 | 预热/连接池/加载优化 |
| 单核高、NUMA 后利用率低但吞吐不涨 | SMT/客户端/排队 | 容量扫描 + 客户端 CPU + 每核利用率 |
| `pg_stat_activity` 有锁等待 | PG 锁/缓冲区 | 查 wait_event；不是 pgGraph 算法 |

## 输出契约

热点结论至少给出：

1. **场景与口径**：查询、数据、连接、绑核、轮次。
2. **瓶颈位置**：服务端 pgGraph / PostgreSQL / 客户端 / 系统；具体函数/阶段。
3. **证据**：perf/火焰图/计数器/等待事件/内存数据；区分实测与推断。
4. **机制**：为什么这是瓶颈（计算/访存/锁/SPI/排队/缓存）。
5. **反证**：排除了哪些假设，用什么证据。
6. **优化方向**：指向 `pggraph-performance-optimization` 的候选，不在此处改码。

## 维护约定

- 百分比、延迟、内存、核数等标“实测基线”，不写成通用规格。
- 数据集规模口径必须记录；不同文档的 3.18M/100K 节点数字不可混用。
- 路径/端口/用户变量化；不写个人 home、挂载盘、私网 IP。
- 两种冲突根因（GIL vs 缓存）同时保留在 `topdown-and-contention.md`，用判别表而非拍脑袋。

## 完整性校验

用例位于姊妹 skill `pggraph-architecture/cases/skill-integrity/`（随系列首 PR 交付）：

```bash
# 源码树中，且 pggraph-architecture 已合入（系列首 PR）时可执行：
bash ../pggraph-architecture/cases/skill-integrity/run.sh
# 缺失的姊妹 skill 记为 pending 而非失败；退出码 0 = 已合入成员全部通过
```

若环境中不存在该脚本（架构 skill 未合入/安装平铺布局），跳过本节，不要把
"No such file" 误报为校验失败。
