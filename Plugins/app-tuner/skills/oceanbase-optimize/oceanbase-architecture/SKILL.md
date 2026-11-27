---
name: oceanbase-architecture
description: >
  OceanBase CE 4.3.5 源码架构与运行视图（只读知识库）：obproxy 无状态转发、observer 单进程
  多线程 + OMT 多租户、资源单元/租户/Zone、Paxos/PALF 与三副本、LS 与分区、LSM-Tree
  （MemTable + mini/minor/major SSTable）与转储合并、SQL 解析/改写/优化/执行全链路、
  DML → MemTable → Redo → 2PC 链路、五层内存与分配器、TG 线程组与 sql_nio/worker/
  ApplySrv/ReplaySrv、锁与事务、参数地图、关键源码路径。用于源码阅读、流程追踪、
  热点定位前的架构核对和优化决策；不直接执行构建、部署、压测或改码，这些动作分别交给
  oceanbase-build-deployment / oceanbase-test-verification / oceanbase-hotspot-analysis /
  oceanbase-performance-optimization。
  触发词：OceanBase 架构、OB 源码、OB SQL 流程、OB 事务流程、OB 内存、OB 线程、
  OB LSM-Tree、OB 转储合并、OB PALF、OB 参数、OB 代码路径、observer、obproxy。
license: MulanPSL-2.0
compatibility: OceanBase CE 4.3.5（源码树 4.3.5_CE_BP5_HF2）；鲲鹏 950/920B/7270Z、aarch64、openEuler 24.03 / AntOS 6.6 与 x86_64 通用
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: relational_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---
# OceanBase 4.3.5 架构与源码视图

> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起构建、部署、压测、改码或系统变更。本文百分比与瓶颈结论来自特定实测环境
> （OB 4.2.5.5 / 4.3.5 源码，鲲鹏 950/920B、AMD 9T34），只用于建立假设，实际结论必须
> 由目标环境证据确认。

## 何时使用

- 读源码前需要模块地图：某个类属于 observer、sql、storage 还是 logservice。
- 追一条查询/写入/转储链路：从 MySQL 协议到 MemTable/SSTable，或从提交到 Paxos 多数派。
- 分析热点前核对线程模型、队列、租户资源与内存边界。
- 评估优化影响面：改线程数、绑核、冻结阈值、排序内存、编译参数会牵动哪些模块。
- 解释部署现象：端口映射、租户 CPU/内存、副本与 primary_zone、跨 NUMA 访存。

**不适用**：具体编译命令、集群安装、压测执行、火焰图采集、代码补丁实施。
按 `references/` 索引进入对应专项，不要在本 skill 内执行业务动作。

## 架构速览

```
Client (sysbench / obclient / 业务)
  │ MySQL 协议
  ▼
obproxy（无状态转发，可选）
  │ MySQL 协议
  ▼
observer（单进程、多线程、多租户）
  ├─ OMT：每租户 Worker + QQ/RQ 六档优先级队列 + 10 级嵌套层级队列 + 资源组队列
  ├─ SQL：parser → resolver → rewrite → optimizer → codegen → executor（DAS/PX）
  ├─ 存储：LS → Tablet → MemTable + mini/minor/major SSTable（LSM-Tree）
  ├─ 日志：PALF（Paxos）redo → 多数派确认 → Apply/Replay → 提交可见
  ├─ 事务：2PC、MVCC、TxTable、锁与 ELR
  └─ 资源：租户 Unit（CPU/内存/IOPS）、cgroup、KV Cache、MemStore
        │
        ▼
  三副本 observer（Zone1/2/3，Paxos 组）
```

| 角色 | 职责 | 状态 | 扩展方式 |
| --- | --- | --- | --- |
| obproxy | 协议转发、路由、连接管理 | 无状态 | 增加进程/实例 |
| observer | SQL、事务、存储、日志、RootService（每集群一个主） | 有状态 | 加节点/加副本，租户 Unit 分布 |
| 租户 | 资源与数据隔离单元（Unit × 副本） | 逻辑实体 | 调 CPU/内存/IOPS、加副本 |

核心设计：单机 Shared-Nothing + Paxos 多副本；一台机器一个 observer 进程承载多个租户；
LS（Log Stream）是日志复制与事务提交的基本单位；表按分区分布到 LS；读路径按 LSM-Tree
多层合并，写路径先写 MemTable 与 redo，再异步转储。

## 关键事实（先记住这些）

1. **端口以 `install.cfg` 为准，不要背默认值**：社区常见默认是 2881（observer MySQL）、
2882（RPC）、2883（obproxy）；本系列实测部署使用 `mysql_port=2882`、`rpc_port=2881`、
`proxy_port=2828`。连错端口会表现为“obclient 能连但租户/库不对”或直接拒绝连接。
2. **observer 是单进程多线程**：一个进程内拉起上千线程（worker、sql_nio、ApplySrv、
ReplaySrv、PX、IO、DAG、Timer 等），用 OMT 按租户调度。进程级绑核/绑内存会影响所有
租户；租户级隔离依赖 Unit 配额与 cgroup。
3. **写入不是直接落盘**：DML 先写 MemTable 并生成 redo，经 PALF 复制到多数派后提交；
MemTable 超过 `freeze_trigger_percentage`（默认 20%）触发冻结转储，后台做 mini→minor→major
合并。写入尾延迟由 redo 同步、转储、合并与 IO 共同决定。
4. **读路径是 LSM-Tree 多层合并**：MemTable + 多个 SSTable 版本按 rowkey 归并，读取要走
Bloom Filter、索引块、微块解码与 MVCC 可见性判断。只读场景的 Microbench 热点通常落在
排序/比较、微块解码、DAS 归并、内存分配与 L3 访存，而不是“SQL 解析”。
5. **内存是五层结构**：`AChunk`（2MB，mmap 对齐）→ `ABlock`（8KB）→ `AObject`（48B 头 + 16B 尾，`AOBJECT_META_SIZE=64`）→
租户 × Context × NUMA 的 `ObTenantCtxAllocator` → 业务分配器（Arena/FifoArena/MemStore）。
MemStore 用 `ObFifoArena`，KV Cache 用固定 2MB memblock，二者回收机制完全不同。
6. **内存超限先洗缓存再报错**：租户分配失败会触发 KV Cache 洗出（plan cache、block cache、
row cache、bloom filter 等），仍不足才返回错误；因此“内存打满”不必然 OOM，但会带来
缓存命中率下降与性能抖动。
7. **线程池是性能归因的关键**：`sql_nio`（网络收发与协议解析）→ 租户请求队列 → `worker`
（SQL 执行）；写入还有 `ApplySrv`（提交回调/可见性）与 `ReplaySrv`（日志回放）。950 上
insert 的 ApplySrv 可接近 <数值>%，此时加 worker 无效。
8. **obproxy 是可观测但不是必需环节**：去掉 obproxy 可让 sysbench 直连 observer；实测
920B 去掉后只写提升可达 58%、只读 20%，AMD 则可能下降。评估“obproxy 是否瓶颈”必须
做 A/B，不能只看火焰图。
9. **跨 NUMA/跨 P 访存是本系列最大的单点风险**：observer 启动时线程分布在全机 NUMA，
内存就近分配；启动后再 `taskset` 只迁移 CPU 不迁移内存，形成跨 P 访存。950 的
`numa_distance` 默认值与 920B 不同，首次选核更容易跨 P；insert 对跨 P 最敏感。
10. **950 与 920B 的微架构画像不同**：950 前端 Bound 更低、L3 为 cluster 内 private，
L3 Bound 更高（约 <数值>% vs <数值>%）；930/920B 的 L3 行为不能直接套到 950。线性度
32U/16U：AMD 约 1.93 > 920B 1.87 > 950 1.64（实测基线）。

## 源码地图（4.3.5）

| 模块 | 路径 | 关键内容 |
| --- | --- | --- |
| 服务入口 | `src/observer/ob_server.h` | `ObServer::THE_ONE`、init/start 序列 |
| 多租户 | `src/observer/omt/` | `ObMultiTenant`、`ObTenant`、`ObThWorker`、PX Pool |
| MySQL 协议 | `src/observer/mysql/` | `ObMPQuery`、结果集编码 |
| SQL 引擎 | `src/sql/` | `parser/`、`resolver/`、`rewrite/`、`optimizer/`、`code_generator/`、`engine/`、`executor/`、`plan_cache/`、`das/`、`dtl/` |
| 存储 | `src/storage/` | `memtable/`、`blocksstable/`、`compaction/`、`tx/`、`tablelock/`、`access/` |
| 日志 | `src/logservice/` | `palf/`、`applyservice/`、`replayservice/`、`archiveservice/` |
| 集群管理 | `src/rootserver/` | DDL、负载均衡、Unit/Zone、容灾 |
| 共享设施 | `src/share/` | `parameter/`、`allocator/`、`io/`、`schema/`、`ob_cgroup_ctrl.h` |
| 基础库 | `deps/oblib/src/lib/` | `allocator/`、`alloc/`、`lock/`、`thread/`、`coro/`、`hash/`、`resource/` |
| 第三方 | `deps/init/*.deps`、`deps/3rd` | 依赖清单与预编译包（见 build-deployment） |
| 参数种子 | `src/share/parameter/ob_parameter_seed.ipp` | 全部参数定义、默认值、范围 |

## 参考索引（渐进披露）

| 文件 | 何时读 |
| --- | --- |
| `references/overview-and-source-map.md` | 需要模块职责、部署形态、进程/端口、租户/Unit/Zone、LS/分区、数据模型与源码定位时 |
| `references/memory-and-thread-model.md` | 追内存分配与回收、KV Cache、MemStore、租户内存限制、TG 线程组、请求分发与线程交互时 |
| `references/sql-and-dml-flow.md` | 追 SELECT/DML 全链路、计划缓存、DAS 远程访问、PX 并行、算子与执行上下文时 |
| `references/storage-and-transaction.md` | 分析 LSM-Tree、冻结与转储合并、微块编解码、PALF、2PC、MVCC、TxTable 与锁时 |
| `references/parameters-and-config-map.md` | 查参数默认值/范围/生效级别，评估线程、内存、冻结、限流、排序内存的影响面时 |

## 输出契约

回答架构问题时给出四段，避免只给结论：

1. **链路**：客户端 → obproxy/协议 → observer 线程/队列 → 模块/函数 → 存储结构（标出 RPC 与
进程边界）。
2. **证据**：源码路径与函数名，必要时给 `文件:函数`；文档结论标注“源码/实测/经验/官方”。
3. **影响面**：改动会触及哪些租户、线程池、队列、缓存、持久化格式或副本协议。
4. **验证入口**：指向对应 skill（部署/测试/热点/优化），不在此处执行。

## 维护约定

- 以 4.3.5 源码为真源；版本差异先核对 `src/share/parameter/ob_parameter_seed.ipp`、
`cmake/Env.cmake`、`deps/init/*.deps`。
- 百分比、线程数、端口等易漂移信息标“实测基线”，不写成通用规格。
- 路径一律用 `${OB_SRC}`、`${OB_HOME}` 等变量，不写个人环境绝对路径。
- 实测数据必须带机型与版本；OB 4.2.5.5 的数字不能直接当作 4.3.5 的结论。

## 完整性校验

五个 OceanBase skill 的结构/链接/锚点/evals/敏感信息检查：

```
bash cases/skill-integrity/run.sh
# 产物 cases/skill-integrity/results/result.json；退出码 0 = 通过
```
