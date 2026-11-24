---
name: nebula-architecture
description: >
  NebulaGraph 3.8.0 源码架构与运行视图（只读知识库）：三服务（metad / graphd / storaged）
  职责、standalone 与分布式进程模型、查询/写入全链路、Thrift 服务与端口、MetaClient 缓存、
  Raft 与 TOSS 事务、RocksDB Key/值编码、索引查找转换、线程池与 folly Future 模型、
  配置参数地图、关键源码路径。用于源码阅读、流程追踪、热点定位前的架构核对和优化决策；
  不直接执行构建、部署、压测或改码，这些动作分别交给 nebula-build-deployment /
  nebula-test-verification / nebula-hotspot-analysis / nebula-performance-optimization。
  触发词：Nebula 架构、Nebula 源码、Nebula 查询流程、Nebula 写入流程、Nebula Key 编码、
  Nebula 端口、Nebula 线程模型、Nebula Raft、Nebula 配置参数、Nebula 代码路径。
license: MulanPSL-2.0
compatibility: NebulaGraph 3.8.0（tag v3.8.0 = fa928930ab34…）；鲲鹏 950 / aarch64、openEuler 24.03 验证，x86_64 Linux 通用
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: graph_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---

# NebulaGraph 3.8.0 架构与源码视图

> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起构建、部署、压测、改码或系统变更。本文的百分比与瓶颈结论来自特定实测环境，
> 只用于建立假设，实际结论必须由目标环境证据确认。

## 何时使用

- 读源码前需要模块地图：某个函数属于 graphd、storaged 还是 kvstore。
- 追一条查询/写入链路：从 nGQL 到 RocksDB，或从客户端到 metad 心跳。
- 分析热点前核对线程模型、RPC 边界和 Key 访问模式。
- 评估优化影响面：改线程数、Key 编码、Raft、RocksDB 参数会牵动哪些模块。
- 解释部署现象：端口偏移、MetaClient 缓存、standalone 与分布式差异。

**不适用**：具体编译命令、集群安装、压测执行、火焰图采集、代码补丁实施。
按 `references/` 索引进入对应专项，不要在本 skill 内执行业务动作。

## 架构速览

```
Client (nebula-console / SDK / k6)
  │ GraphService (Thrift)
  ▼
graphd ── MetaService ──▶ metad ── Raft ── metad peers
  │                          ▲
  │ GraphStorageService      │ MetaService (heartbeat / leader report)
  ▼                          │
storaged ────────────────────┘
  ├─ RocksDB（每个 Space 在每条 data_path 下各一个 RocksEngine，Block Cache 全进程共享）
  ├─ Raft（每个 Partition 一个复制组）
  └─ WAL / 索引 / TOSS 跨分区事务
```

| 服务 | 角色 | 状态 | 扩展方式 |
|------|------|------|----------|
| graphd | 解析、校验、优化、调度、结果组装 | 无状态 | 增加进程/实例 |
| metad | Space/Schema/分区/用户/作业/心跳 | Raft 有状态 | 多副本 Raft |
| storaged | 图数据、索引、Raft、RocksDB | 有状态 | 增加分区副本/节点 |

核心设计：存储计算分离；分区即 Raft 组；出边邻接表按 `(partId, srcId, edgeType)` 连续存放；
Key 的字节字典序承载图语义（前缀扫描即图遍历）。

## 关键事实（先记住这些）

1. **standalone 与分布式由编译期决定**，不是运行时开关：
   `ENABLE_STANDALONE_VERSION=ON` 生成一个 `nebula-standalone`（三服务同进程、不同线程；
   客户端→graphd、graphd/storaged→metad（meta_port）仍走 loopback Thrift RPC，而 graphd→storaged
   在 standalone 构建下由 `StorageClient` 直接投递到进程内 `GraphStorageLocalServer`
   （经 ThreadManager 派发，无端口与序列化，见 `src/clients/storage/StorageClient.h`
   的 `BUILD_STANDALONE` 分支与 `src/storage/GraphStorageLocalServer.cpp`））；
   `OFF` 生成 `nebula-metad / nebula-graphd / nebula-storaged`。
2. **graphd 默认不直接读 metad**：每个 graphd/storaged 进程内 MetaClient 维护
   SpaceInfoCache（schema、partHosts、索引），查询命中本地缓存；约每 10s 心跳刷新。
   只读压测里 metad CPU 接近 0 是正常现象。
3. **查询按 VID 哈希路由分区**：`partId = hash(VID) % numParts + 1`（分区号从 1 起；8 字节
   VID 直接取整数值，其他长度走 MurmurHash2），再查 MetaClient 缓存的 leader 地址；
   LOOKUP 因不知道 VID，会广播到所有分区并行扫描。
4. **N 跳遍历 = N 个串行轮次**（每跳按当前顶点集合重新路由；一轮内顶点按目标 storaged
   主机分组、并行发出多个 RPC），这是 Go2/Go3 延迟和
   RPC 开销的根源，也是热点从 graphd 迁移到 storaged 的原因。
5. **写入走 Raft**：编码 Key-Value → WriteBatch → appendAsync 写 WAL → 复制到 Follower →
   多数派确认 → commit 到 RocksDB。WAL fsync 与 compaction 决定写入尾延迟。
6. **RocksEngine 按 Space × data_path 计数**：一个 storaged 配多盘（`--data_path=/disk1,/disk2`）
   时，同一 Space 在每条 data_path 下各有一个 RocksEngine 实例；`USE space` 只改 session
   上下文，不开关 RocksDB；Block Cache 是进程级 `static`，所有 Space 与引擎共享同一份容量。
7. **storaged 每个实例占连续 3 个端口**：`port-1`（StorageAdminService）、`--port`
   （GraphStorageService）、`port+1`（RaftexService）；多实例规划必须留 ≥3 的间隔，
   否则会出现 `Address already in use`。metad 的 Raft 端口同样按相邻规则分配。
8. **graphd 的 Thrift 优先级队列是轻查询场景的常见瓶颈**：worker 数越多，单队列 futex
   争用越强；增加 graphd 实例数（减少每个队列的 worker 数）通常比继续加线程有效。

## 源码地图（3.8.0）

| 模块 | 路径 | 关键内容 |
|------|------|----------|
| 守护进程入口 | `src/daemons/` | `GraphDaemon.cpp`、`MetaDaemon.cpp`、`StorageDaemon.cpp`、`StandAloneDaemon.cpp` |
| graphd | `src/graph/` | `service/`、`validator/`、`planner/`、`optimizer/`、`scheduler/`、`executor/`、`context/`、`visitor/` |
| storaged | `src/storage/` | `query/`、`mutate/`、`index/`、`transaction/`、`admin/`、`exec/` |
| metad | `src/meta/` | `processors/`（schema/parts/user/job/config/zone/session） |
| KV/Raft | `src/kvstore/` | `raftex/`、`wal/`、`listener/`、`RocksEngine`、`NebulaStore` |
| 解析 | `src/parser/` | flex/bison 词法与语法 |
| 编解码 | `src/codec/` | `RowWriterV2` / `RowReaderV2` |
| 公共类型 | `src/common/` | `datatypes/Value`、`expression/`、`memory/`、`utils/NebulaKeyUtils` |
| Thrift IDL | `src/interface/` | GraphService、MetaService、GraphStorageService 等 |
| 客户端 | `src/clients/` | `MetaClient`、`StorageClient` |

## 参考索引（渐进披露）

| 文件 | 何时读 |
|------|--------|
| `references/overview-and-source-map.md` | 需要模块职责、数据模型、分片、线程池、standalone/分布式差异与源码定位时 |
| `references/query-write-flow.md` | 追查询/写入/索引链路，理解 DAG、Future、分区路由、Raft 与 TOSS 时 |
| `references/storage-key-encoding.md` | 分析 RocksDB 访问模式、前缀扫描、索引范围、Block Cache 与 Space 关系时 |
| `references/process-ports-config.md` | 规划端口、排查心跳/leader、配置线程/缓存/WAL/压缩、理解参数影响时 |

## 输出契约

回答架构问题时给出四段，避免只给结论：

1. **链路**：客户端 → 服务/端口 → 函数/模块 → 存储结构（标出跨进程 RPC 边界）。
2. **证据**：源码路径与函数名，必要时给 `文件:函数`；文档结论标注“实测/经验/官方”。
3. **影响面**：改动会触及哪些进程、线程池、协议或持久化格式。
4. **验证入口**：指向对应 skill（部署/测试/热点/优化），不在此处执行。

## 维护约定

- 以 3.8.0 源码为真源；版本差异先核对 `src/interface/*.thrift` 与 `conf/*.default`。
- 百分比、线程数、端口等易漂移信息标“实测基线”，不写成通用规格。
- 路径一律用 `${NEBULA_HOME}`、`${SRC_ROOT}` 等变量，不写个人环境绝对路径。

## 完整性校验

五个 Nebula skill 的结构/链接/锚点/evals/敏感信息检查：

```bash
bash cases/skill-integrity/run.sh
# 产物 cases/skill-integrity/results/result.json；退出码 0 = 通过
```
