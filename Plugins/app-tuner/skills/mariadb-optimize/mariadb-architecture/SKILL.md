---
name: mariadb-architecture
description: >
  MariaDB 10.11.16 源码架构与运行视图（只读知识库）：Server 层与存储引擎 handler 分层、
  InnoDB/Aria/MyISAM/MyRocks/ColumnStore 的定位、连接与线程模型（one-thread-per-connection
  与 pool-of-threads）、SQL 解析→优化器→执行器→handler→InnoDB 的查询链路、redo/binlog
  两阶段提交与组提交、MVCC/undo/purge、行锁/MDL 与 rw_lock/srw_lock/sux_lock、buffer pool/
  doublewrite/adaptive hash、change buffer（10.11 默认 none、10.9 起弃用、11.0 移除）、内存与文件布局、参数地图、关键源码路径。
  用于源码阅读、流程追踪、热点定位前的架构核对与优化决策；不执行构建、部署、压测或改码，
  这些动作分别交给 mariadb-build-deployment / mariadb-test-verification /
  mariadb-hotspot-analysis / mariadb-performance-optimization。
  触发词：MariaDB 架构、MariaDB 源码、MariaDB 查询流程、MariaDB 事务、MariaDB 锁、
  InnoDB 结构、redo log、binlog、buffer pool、handler、线程模型、参数地图。
license: MulanPSL-2.0
compatibility: MariaDB 10.11.16（stable）源码树；10.3.x 鲲鹏案例作为历史证据；aarch64/x86_64 Linux
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: relational_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---
# MariaDB 10.11.16 架构与源码视图

> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起构建、部署、压测、改码或系统变更。本文的数值与瓶颈结论来自特定实测环境，
> 只用于建立假设，实际结论必须由目标环境证据确认。

## 何时使用

- 读源码前需要模块地图：某个函数属于 Server 层、InnoDB、Aria 还是复制模块。
- 追一条 SQL/事务链路：从客户端连接、解析、优化到 InnoDB 页与 redo/binlog。
- 分析热点前核对线程模型、锁层次、缓存边界与刷盘路径。
- 评估优化影响面：改 buffer pool、redo、双写、线程池、隔离级别会牵动哪些模块。
- 解释部署与运行现象：文件布局、端口/socket、崩溃恢复、主从延迟、purge 滞后。

**不适用**：编译命令、集群安装、压测执行、火焰图采集、补丁实施。
按 `references/` 索引进入对应专项，不要在本 skill 内执行业务动作。

## 架构速览

```
Client (mysql/mariadb CLI, JDBC, sysbench, TPCCRunner)
  │  MySQL 协议（TCP 3306 / unix socket）
  ▼
Server 层
  ├─ 连接与线程：one-thread-per-connection 或 pool-of-threads（threadpool）
  ├─ THD / 会话状态 / 权限 / 元数据锁（MDL）
  ├─ Parser（sql_yacc.yy）→ 优化器（JOIN/range/filesort）→ 执行器
  ├─ 事务与日志协调：binlog + InnoDB redo 的两阶段提交、组提交
  └─ 复制：dump 线程 / IO 线程 / SQL 线程、GTID、Galera(wsrep)
  │  handler API（ha_*：open/read/write/scan/index/commit）
  ▼
存储引擎
  ├─ InnoDB（默认）：buffer pool、B+tree、redo/undo、MVCC、行锁、doublewrite
  ├─ Aria：崩溃安全 MyISAM 替代（内部临时表、系统表）
  ├─ MyISAM / MEMORY / CSV / ARCHIVE / BLACKHOLE
  ├─ MyRocks（LSM，替代 InnoDB 的写放大场景）
  ├─ ColumnStore（列存，分析型）
  └─ Spider / Connect / FederatedX（外部访问与分片）
```

| 层 | 角色 | 状态 | 扩展方式 |
| --- | --- | --- | --- |
| Server 层 | 连接、解析、优化、执行、事务协调、复制 | 无状态（会话在内存） | 多实例 + 代理/负载均衡 |
| InnoDB | 行存事务引擎，绝大多数 OLTP 数据 | 有状态（页/日志/undo） | 垂直扩容、读写分离、分库分表 |
| Aria | 崩溃安全非事务引擎，内部临时表与系统表 | 有状态 | 通常不单独扩容 |
| MyRocks | LSM 存储，写密集/压缩比优先 | 有状态 | 独立实例或按表选择 |
| ColumnStore | 列存 + 列式执行，分析型 | 有状态（独立服务） | 增加节点/分片 |

核心设计：**Server 层与存储引擎通过 handler API 解耦**；InnoDB 以页为单位管理缓存与
持久化，redo 保证崩溃恢复，undo + read view 实现 MVCC，binlog 负责复制与时间点恢复。

## 关键事实（先记住这些）

1. **默认存储引擎是 InnoDB**（10.11 的 `default_storage_engine`）。`Aria` 承担内部临时表
与部分系统表；`MyISAM` 仍存在但不具备事务与崩溃安全。选型看事务/写放大/分析需求，
不是“越新越好”。
2. **连接模型二选一**：`thread_handling=one-thread-per-connection`（默认，每连接一线程）
或 `pool-of-threads`（线程池，限制并发线程数）。线程池对高并发短查询更稳，
但长事务/大查询会占住 worker。`thread_pool_stall_limit` 单位是毫秒，值越大停滞检测
间隔越长、补充 worker 越晚，短请求排队时可能更晚获得 worker；调整前后要对比
排队时间与停滞观测（`THREAD_POOL_QUEUES` / `THREAD_POOL_STATS`），不要盲目调大。
3. **InnoDB 的持久化是 redo + 双写 + 数据页三层**：事务提交先写 redo（受
`innodb_flush_log_at_trx_commit` 控制），脏页由 page cleaner 刷盘，写数据页前先写
doublewrite，避免部分页写。**redo 不是 binlog**：redo 用于崩溃恢复，binlog 用于复制/PITR。
4. **提交是 redo 与 binlog 的两阶段提交**：先 prepare redo，再写 binlog，最后 commit redo；
崩溃恢复按 binlog 是否完整决定前滚/回滚。组提交（`binlog_commit_wait_count/usec`）
把多个事务的 fsync 合并，是写吞吐的关键。
5. **MVCC 由 undo + read view 实现**：一致性读不锁行，读取可见版本；purge 线程清理
不再可见的 undo。长事务/大查询会阻塞 purge，导致 undo 膨胀与历史版本链变长。
6. **锁分两层**：Server 层 MDL（元数据锁，DDL 与 DML 互斥）与 InnoDB 行锁/表意向锁；
latch（`rw_lock`/`srw_lock`/`sux_lock`）保护内存结构，不可与事务锁混为一谈。
10.6 起 InnoDB 的 `rw_lock_t` 被 `std::atomic<uint32_t>` 的 `rw_lock` 类取代，
10.3 时代的 `lock_word`/`s_lock_word` 字段在 10.11 已不存在。
7. **buffer pool 是页缓存，不是查询缓存**：命中则免 IO；未命中从磁盘读页。它与 OS page
cache 可能重复缓存，`innodb_flush_method=O_DIRECT`（10.11 默认）用于绕过 page cache。
8. **change buffer 在 10.11 默认关闭**：它缓存非唯一二级索引的写、减少随机读；
`innodb_change_buffering` 在 10.11.16 仍存在且设置仍生效（默认 `none`，10.9 起标记弃用并告警，
11.0 才移除）。不要把随机 IO/CPU 热点归因到它之前，先 `SHOW VARIABLES LIKE 'innodb_change_buffering'`
确认值为 `none`。adaptive hash index 加速等值查找但会增加 latch 与内存开销。
9. **文件布局固定但可分离**：`ibdata1`（系统表空间/undo 可分离）、`ib_logfile0`
（10.8 起单文件、可在线 resize；数据目录中的 `ib_logfile1` 属 ≤10.7 升级遗留，无 MySQL 8.0 的 `#innodb_redo/`）、`*.ibd`（每表空间）、
`aria_log*`、`*-bin.00000*`（binlog，默认 hostname 或 log-basename 加 `-bin`）、`*-relay-bin.*`（relay log）、`#sql-*` 临时表；双写区在系统表空间中（无
`innodb_doublewrite_dir`）。redo 与 binlog 放在不同盘可降低写竞争。

## 源码地图（10.11.16）

| 模块 | 路径 | 关键内容 |
| --- | --- | --- |
| 服务入口 | `sql/mysqld.cc` | 初始化、信号、线程调度器、关闭流程 |
| 连接与会话 | `sql/sql_connect.cc`、`sql/sql_class.h` | 握手、THD、会话变量、kill |
| 线程池 | `sql/threadpool*.cc/.h`、`sql/thread_cache.h` | pool-of-threads、worker、队列 |
| 解析 | `sql/sql_yacc.yy`、`sql/sql_lex.*` | 词法/语法、LEX |
| 优化与执行 | `sql/sql_select.cc`、`sql/opt_*.cc`、`sql/filesort.cc`、`sql/sql_union.cc` | JOIN 顺序、range、filesort、子查询（执行器在 sql_select.cc，10.11 无 sql_executor.cc） |
| 表与 handler | `sql/handler.{h,cc}`、`sql/table.cc`、`sql/table_cache.cc` | 引擎接口、表定义缓存、打开表 |
| 事务/日志协调 | `sql/transaction.cc`、`sql/log.cc`、`sql/log_event.cc` | 2PC、组提交、binlog 写入（10.11 无 sql/binlog.cc） |
| 复制 | `sql/rpl_*.cc`、`sql/log_event*.cc`、`sql/slave.cc` | dump/IO/SQL 线程、GTID、relay log |
| Galera | `wsrep-lib/`、`sql/wsrep_*.cc` | 写集复制、认证、SST/IST |
| InnoDB 入口 | `storage/innobase/handler/ha_innodb.cc` | 引擎注册、参数、事务桥接 |
| buffer pool | `storage/innobase/buf/` | `buf0buf.cc`、`buf0lru.cc`、`buf0flu.cc`、`buf0dump.cc` |
| redo/恢复 | `storage/innobase/log/`、`storage/innobase/srv/srv0start.cc` | `log0log.cc`、`log0recv.cc`、启动恢复 |
| 事务/undo | `storage/innobase/trx/` | `trx0trx.cc`、`trx0undo.cc`、`trx0purge.cc` |
| 行锁 | `storage/innobase/lock/` | `lock0lock.cc`、`lock0prdt.cc` |
| latch | `storage/innobase/sync/`、`include/rw_lock.h`、`srw_lock.h`、`sux_lock.h` | `srw_lock.cc`、futex 封装 |
| B+tree/记录 | `storage/innobase/btr/`、`row/`、`page/` | `btr0cur.cc`、`row0sel.cc`、`page0page.cc` |
| 元数据/字典 | `sql/datadict.cc`、`storage/innobase/dict/` | 数据字典、表空间 |
| Aria | `storage/maria/` | `ha_maria.cc`、pagecache、日志 |
| MyRocks | `storage/rocksdb/` | `ha_rocksdb.cc`、RocksDB 集成 |
| 性能模式 | `storage/perfschema/` | 表定义与采集 |
| 客户端/工具 | `client/`、`extra/`、`scripts/` | `mariadb`（client/）、`mariadb-backup`（extra/）、`mariadb-install-db`（scripts/） |

## 参考索引（渐进披露）

| 文件 | 何时读 |
| --- | --- |
| `references/overview-and-source-map.md` | 需要模块职责、数据模型、引擎选型、handler 接口、版本差异与源码定位时 |
| `references/query-execution-flow.md` | 追连接/解析/优化/执行/InnoDB 读写、2PC 与组提交、复制链路时 |
| `references/innodb-storage-internals.md` | 分析 buffer pool、B+tree、redo/undo、MVCC、doublewrite、purge 时（change buffer：10.11 默认 none、10.9 起弃用） |
| `references/process-threads-and-config-map.md` | 规划线程、排查锁/等待、理解文件布局与参数影响、看 performance_schema 时 |

## 输出契约

回答架构问题时给出四段，避免只给结论：

1. **链路**：客户端 → 连接/线程 → Server 模块 → handler → 引擎结构（标出磁盘/日志边界）。
2. **证据**：源码路径与函数名，必要时给 `文件:函数`；文档结论标注“实测/经验/官方”。
3. **影响面**：改动会触及哪些线程、缓存、锁、日志或持久化格式。
4. **验证入口**：指向对应 skill（部署/测试/热点/优化），不在此处执行。

## 维护约定

- 以 10.11.16 源码为真源；版本差异先核对 `VERSION`、`sql/sys_vars.cc` 与
`storage/innobase/handler/ha_innodb.cc` 的参数声明。
- 百分比、线程数、默认值等易漂移信息标“实测/源码默认”，不写成通用规格。
- 路径一律用 `${MARIADB_HOME}`、`${SRC_ROOT}`、`${DATA_ROOT}` 等变量，不写个人环境绝对路径。

## 完整性校验

五个 MariaDB skill 的结构/链接/锚点/evals/敏感信息检查：

```
bash cases/skill-integrity/run.sh
# 产物 cases/skill-integrity/results/result.json；退出码 0 = 通过
```
