# OceanBase 总览、部署形态与源码地图

源码根：`${OB_SRC}`（4.3.5_CE_BP5_HF2）。下文路径均相对 `${OB_SRC}`。

## 1. 部署形态

| 形态 | 组成 | 适用 | 注意 |
| --- | --- | --- | --- |
| 单节点单副本 | 1 observer + 可选 obproxy | 功能验证、单机调优基线 | `zone_num=1`、`locality=F`；无多数派，写入语义与生产不同 |
| 单机三副本 | 1 台内 3 个 observer 进程/端口，或 3 台各 1 个 | 架构验证、Paxos 行为 | 单机三副本仍受同机 NUMA/IO 竞争影响 |
| 三机三副本 | 3 observer（Zone1/2/3）+ obproxy + 压测机 | 本系列主测试形态 | observer 按 LS 呈现一主两备（同一节点可兼有 Leader/Follower）；压测机常同时跑 sysbench 与 obproxy |
| 多实例混部 | 单机多 observer（32U×2+64U×1） | 容器化生产模拟 | 需 VF/中断/NUMA 隔离，否则实例间互相拖累 |

### 1.1 测试拓扑（实测基线）

```
压测机：sysbench + obproxy
   │  MySQL 2828/2883
   ▼
observer A/B/C（Zone1/2/3，各 32U/64U/128U，三副本）
```

主备角色按 **LS（日志流）** 划分，不按机器固定：同一台 OBServer 可同时承载不同 LS 的
Leader 与 Follower。归因前先在 sys 租户按 `LS_ID`、`SVR_IP`、`ROLE` 核对分布：

```
SELECT TENANT_ID, LS_ID, SVR_IP, SVR_PORT, ROLE FROM oceanbase.GV$OB_LOG_STAT
 WHERE TENANT_ID = <业务租户ID> ORDER BY LS_ID, ROLE, SVR_IP;
```

只有确认某节点上相关 LS 的角色后，才能把其 CPU/网卡流量归为“复制/回放（Follower）”
或“查询/写入（Leader）”：写场景 Follower 也有 CPU 与网卡流量（redo 复制与回放），读场景
Follower 流量通常更低。仅按机器贴“主/备”标签会误判负载。

## 2. 进程、端口与目录

| 进程 | 默认端口（社区） | 本系列实测 `install.cfg` | 说明 |
| --- | --- | --- | --- |
| observer MySQL | 2881 | `mysql_port=2882` | MySQL 协议，obclient/业务直连 |
| observer RPC | 2882 | `rpc_port=2881` | observer 间 RPC、DAS |
| obproxy | 2883 | `proxy_port=2828` | 客户端入口，转发到 observer |
| observer HTTP（部分版本） | 无固定 | — | 视版本与配置 |

> 端口不是“记住默认值”而是“以配置文件为准”。部署后用 `ss -lntp` 与 `SHOW PARAMETERS`
> 双向核对；客户端连错端口时，常见现象是能建立连接但 `-uroot@tenant` 报租户不存在。

目录约定（实测）：

```
${OB_BASE}/data/1/obm/<cluster>.z1.obs0/   # workdir / data_path / slog_path
${OB_BASE}/log/obm/                       # clog_path
```

`data_path`、`clog_path`、`slog_path`、`workdir` 可以分盘；生产建议 data/clog 分盘，
XFS + `noatime,nodiratime,pquota`。

## 3. 租户、资源单元、Zone

```
Cluster
 ├─ Zone1 / Zone2 / Zone3          # 故障域，通常 1 Zone = 1 机器
 ├─ Tenant（mysql / oracle 模式）
 │    ├─ Resource Unit（CPU、Memory、IOPS、log_disk）
 │    └─ Unit Num / 副本分布（locality）
 └─ sys 租户（ID=1）；500 是 server tenant（非 sys）
     每个用户租户另有一个 meta tenant（ID 与用户租户差 1）
```

关键点：

- **Unit 决定租户可用资源**：`min_cpu/max_cpu`、`memory_size`、`max_iops/min_iops`。
租户 CPU 配得小于 observer 可用核数时，observer 跑不满是预期行为。
- **primary_zone 影响各 LS 的主副本分布**：三节点测试常把租户主副本固定在压测机同机，减少跨机 RPC；
`ALTER TENANT <t> primary_zone='z1;z2;z3'`（三节点）或 `'z1'`（单节点）。主副本是 LS 级角色，
实际分布仍按上文 `GV$OB_LOG_STAT` 的 `LS_ID`/`SVR_IP`/`ROLE` 核对。
- **locality**：`FFF` 三副本、`F` 单副本；单节点环境必须改成单副本，否则建租户会卡住。
- **大规格要同步放大资源**：32U→64U→128U 时，`tenant cpu_num`、`memory_size`、
`net_thread_count` 要一起调，否则出现“机器有核但租户不用”的假瓶颈。

## 4. 数据模型与分区

- 表按 **分区（partition）** 切分，分区归属 **LS（Log Stream）**；LS 是日志复制与事务
提交单位，一个 LS 对应一个 Paxos 组。
- 分区副本分布由租户 `locality` 与 Unit 决定；主副本负责强一致读写，备副本回放 redo。
- 表是 **LSM-Tree**：MemTable（可写）+ SSTable（只读，多层）。不是 B+ 树，随机更新会
转为“追加 + 合并”。
- 全局索引是独立的表/分区对象；本地（局部）索引与主表绑定在同一个 partition/Tablet 内存储，
  两者路由不同；DAS 负责把远程分区的访问变成 RPC。

## 5. 源码地图（按问题查）

| 想回答 | 首选路径 | 关键类/函数 |
| --- | --- | --- |
| 进程怎么起来 | `src/observer/ob_server.h` | `ObServer::init()`、`ObServer::start()` |
| 租户怎么调度 | `src/observer/omt/ob_multi_tenant.h`、`ob_tenant.h` | `ObMultiTenant::recv_request()`、`ObTenant::ReqQueue` |
| SQL 入口 | `src/observer/mysql/obmp_query.h`、`src/sql/ob_sql.h` | `ObMPQuery::process()`、`ObSql::stmt_query()` |
| 解析/优化 | `src/sql/parser/`、`resolver/`、`optimizer/` | `ObParser::parse()`、`ObOptimizer::optimize()` |
| 执行/算子 | `src/sql/engine/`、`executor/` | `ObTableScanOp`、`ObSortOpImpl`、`ObExecContext` |
| 远程分区 | `src/sql/das/`、`src/sql/dtl/` | DAS task/RPC、DTL channel |
| 计划缓存 | `src/sql/plan_cache/` | `ObPlanCache`、`ObPhysicalPlan` |
| 写入 MemTable | `src/storage/memtable/` | `ObMemtable::set()`、`ObMvccEngine`、`ObQueryEngine` |
| SSTable 读 | `src/storage/blocksstable/` | `ObSSTable`、`ObMicroBlockDecoder`、`ObIndexTreePrefetcher` |
| 冻结/转储 | `src/storage/tx_storage/ob_tenant_freezer.h`、`src/storage/compaction/` | `ObTenantFreezer`、merge DAG |
| 日志/复制 | `src/logservice/` | `ObLogService`、PALF、`ObLogApplyService`、`ObLogReplayService` |
| 事务 | `src/storage/tx/` | `ObTransService`、`ObPartTransCtx`、`ObTxTable` |
| 内存 | `deps/oblib/src/lib/allocator/`、`src/share/allocator/` | `ObMallocAllocator`、`ObTenantCtxAllocator`、`ObFifoArena` |
| 锁 | `deps/oblib/src/lib/lock/` | `ObLatch`、`ObLatchMutex`、`ObBucketLock`、`DRWLock` |
| 线程 | `deps/oblib/src/lib/thread/`、`src/share/ob_thread_define.h` | TG 框架、线程组定义 |
| 参数 | `src/share/parameter/ob_parameter_seed.ipp` | `DEF_INT/DEF_BOOL/DEF_STR/...` |
| 构建 | `build.sh`、`cmake/Env.cmake`、`deps/init/` | 编译模式、依赖清单 |

## 6. 版本差异核对清单

迁移 4.2.5 → 4.3.5 或 cherry-pick 补丁时，先核对：

1. `src/share/parameter/ob_parameter_seed.ipp`：参数默认值/范围/是否仍存在。
2. `src/interface/*.thrift`：RPC 协议与枚举。
3. `cmake/Env.cmake`：`-march`、LSE、LTO/AutoFDO/HOTFUNC/BOLT 开关。
4. `deps/init/oceanbase.*.deps`：三方依赖版本（protobuf、rocksdb、arrow 等）。
5. 计划缓存/执行器接口：`ObPhysicalPlan`、`ObOperator` 的虚函数是否变化。
6. 存储格式：微块编码、TxTable、PALF 日志格式；旧二进制通常打不开新格式。

## 7. 架构级实测线索与待验证假设

> 下表前 3 行的百分比是脱敏占位符，原文未附原始采样值、采样时间、机型/版本、负载与绑核条件，
> **只能当待验证假设**；在目标环境用同口径证据确认前，不得据此排除前端、锁或 IO 瓶颈。
> 后 2 行有比值/增益口径，仍需在目标机型/版本复测后才可引用。

| 事实 / 待验证假设 | 数据与口径 | 判读 |
| --- | --- | --- |
| 【待验证假设】只读瓶颈是后端内存 | 原文仅留 Backend Bound / Memory Bound / L3 Bound 的 <数值>% 占位；缺原始采样值、机型/版本、负载、绑核与 topdown 口径 | 需同口径 topdown + cache/TLB/perf 数据互证；成立后才优先减少随机访存/提升局部性，未验证时同时排查前端、锁与 IO |
| 【待验证假设】只写瓶颈含 IO/ApplySrv | 原文仅留 920B 只写 CPU、内存盘/V7 盘 off-CPU 等 IO 的 <数值>% 占位；缺采样值与盘型/负载条件 | 先分清“等 IO”还是“算不动”，不能用占位百分比下结论 |
| 【待验证假设】计划缓存/排序热点突出 | 原文仅留 sysbench read_only 排序+字符比较链 <数值>%+ 占位；缺火焰图原始占比与 collation/版本 | 需原始火焰图与查询语义互证后再动 `_sort_area_size` |
| 950 线性度差 | 32U/16U：950 1.64 vs 920B 1.87 vs AMD 1.93 | 大规格前先验证跨 cluster/跨 P 访存 |
| obproxy 影响与平台相关 | 920B 去掉 obproxy 只写 +58%，AMD -6% | 必须 A/B，不能一概而论 |

> 后 2 行来自 OB 4.2.5.5 与特定绑核策略；4.3.5 上需重测后再引用。
