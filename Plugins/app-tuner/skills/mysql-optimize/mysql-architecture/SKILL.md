---
name: mysql-architecture
description: >
  MySQL 8.0（8.0.25/8.0.28）源码架构与运行视图（只读知识库）：Server 层与 InnoDB 分层、
  连接线程与后台线程模型、查询执行与写入提交全链路、MVCC/Read View、redo/binlog 两阶段与
  group commit、Buffer Pool/LRU/刷脏、锁与等待、排序与 collation 路径、redo/undo/doublewrite、
  配置参数地图、关键源码路径与线程名。用于源码阅读、流程追踪、热点定位前的架构核对和
  优化影响面评估；不执行构建、部署、压测、profiling 或改码，这些动作分别交给
  mysql-build-deployment / mysql-test-verification / mysql-hotspot-analysis /
  mysql-performance-optimization。触发词：MySQL 架构、MySQL 源码、MySQL 查询流程、
  MySQL 写入流程、InnoDB 结构、MVCC、Read View、redo、binlog、Buffer Pool、线程模型、
  MySQL 参数、MySQL 代码路径。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25 / 8.0.28；Percona Server 5.7/8.0 路径大体兼容；鲲鹏 920/920G、aarch64、openEuler 22.03/24.03 实测，x86_64 Linux 通用
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: relational_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---

# MySQL 8.0 架构与源码视图

>
> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起构建、部署、压测、profiling 或系统变更。本文的线程数、百分比与参数值来自特定
> 实测环境，只用于建立假设；实际结论必须由目标环境证据确认。

## 何时使用

- 读源码前需要模块地图：某个函数属于 Server 层、handler 还是 InnoDB。
- 追一条查询/写入链路：从客户端 SQL 到 Buffer Pool，或从 commit 到 redo/binlog 落盘。
- 分析热点前核对线程模型、锁边界、刷脏路径与排序路径。
- 评估优化影响面：改 buffer pool、redo、binlog、AHI、线程数、隔离级别会牵动哪些模块。
- 解释压测现象：prepare 语句、只读/读写差异、CPU 打不满、P95 长尾、写场景 IO 等待。

**不适用**：具体编译命令、初始化、容器/虚机搭建、MTR/sysbench 执行、perf 采集、补丁实施。
按 `references/` 索引进入对应专项，不要在本 skill 内执行业务动作。

## 架构速览

```
Client (mysql / JDBC / sysbench)
  │  TCP 3306 / unix socket
  ▼
Connection layer ── one thread per connection（handle_connection）
  ▼
SQL layer: parser → optimizer → executor → handler API
  │
  ▼
InnoDB handler (ha_innobase)
  ├─ Transaction/MVCC: trx_sys、Read View、undo、锁系统
  ├─ B+Tree: btr_cur_search_to_nth_level → page_cur_search → row_search_mvcc
  ├─ Buffer Pool: page hash、LRU、flush list、page cleaner
  ├─ Redo: log buffer → log_writer → log_flusher → ib_logfile*
  ├─ Doublewrite（可选）
  └─ Change Buffer / AHI / Purge
  ▼
Data files: ibdata1、*.ibd、undo_*、redo、binlog、relay log
```

| 层 | 组成 | 状态 | 性能敏感点 |
| --- | --- | --- | --- |
| 连接层 | 监听、认证、线程创建 | 每连接一线程 | 连接风暴、线程创建、nofile |
| SQL 层 | parser/optimizer/executor、临时表、filesort | 无状态 | 解析、执行计划、排序/collation |
| InnoDB | 事务、锁、Buffer Pool、redo/undo、B+Tree | 有状态 | 锁竞争、刷脏、日志 fsync、页查找 |
| 复制/日志 | binlog、relay log、group commit | 有状态 | binlog 组提交、sync_binlog、网络 |

## 关键事实（先记住这些）

1. **连接即线程**：默认 `thread_handling=one-thread-per-connection`，每个业务连接对应一个
`handle_connection` 线程；高并发下线程数≈连接数。Percona 的 thread pool 是另一条路径。
2. **后台线程有两层名字，SQL 识别只用 `threads.NAME`**：`performance_schema.threads.NAME`
   由 PSI 注册键名 `m_name` 生成（`thread/<category>/<m_name>`），8.0.25/8.0.28 的 InnoDB
   线程是 `thread/innodb/log_writer_thread`、`log_flusher_thread`、`log_checkpointer_thread`、
   `page_flush_thread`（page cleaner / `page_cleaner` 线程组）、`srv_purge_thread`、`dict_stats_thread`、
   `buf_dump_thread`、`srv_master_thread` 等，**不存在** `thread/innodb/ib_*`。`ib_*` 是
   `m_os_name` 写出的 OS 线程名（`my_thread_self_setname`，15 字符截断），只用于
   `ps -T`/`top -H`，8.0.27 起才有——8.0.25/8.0.26 的 InnoDB 线程 `comm` 仍是 `mysqld`；
   8.0.28 的 flusher/checkpointer OS 名分别是 `ib_log_flush`/`ib_log_checkpt`。Server 层
   连接线程是 `thread/sql/one_connection`（`handle_connection`）。绑核时用 NAME 查出的
   `THREAD_OS_ID`（TID），读写场景绑到独立核常能降低尾延迟，但核少时反而负收益。
3. **Point 查询分 AHI 命中与回退两条路径**：AHI 开启且热键命中时，InnoDB 由自适应哈希索引
直接定位记录，跳过从根到叶的逐层查找；未命中（AHI 关闭/冷键/条件不满足）才走
`row_search_mvcc` → `btr_cur_search_to_nth_level` → `page_cur_search_with_match`
→ Buffer Pool 取页 → `rec_get_offsets` → 行解码的完整 B+Tree 路径。回退路径每层都有
可观测热点（比较、offset 计算、页 latch、Read View）；热点归因先用 AHI 命中状态
区分分支，不要把完整 B+Tree 路径写成点查必经路径。
4. **读一致性由 Read View 决定**：RC 每条语句、RR 首次读建立快照；`MVCC::view_open`
在 `trx_sys->mutex` 下维护视图列表。高并发只读/RC 场景，视图开关与 `trx_sys` 竞争
会成为热点（见源码优化 Read View version）。
5. **redo 是 WAL，binlog 是逻辑日志**：事务提交先写 redo（`log_writer`）并按
`innodb_flush_log_at_trx_commit` 决定 fsync；binlog 组提交分 flush/sync/commit 三阶段，
由 `Commit_stage_manager` 排队；`sync_binlog` 决定 binlog fsync 频率。
6. **Doublewrite 是页原子性的兜底**：数据页先写 doublewrite 再写数据文件；NVMe 原子写
（如 ES3000 系列 + 支持的文件系统/页大小）可在确认后关闭 doublewrite 减少一次写。
7. **Buffer Pool 是进程级共享**：`innodb_buffer_pool_size` 按实例/分块分配，
`innodb_buffer_pool_instances` 降低单 mutex 竞争；按 `Innodb_buffer_pool_read_requests` 与
`Innodb_buffer_pool_reads` 计算的命中率接近 95% 后继续加大收益递减（超过 99% 几乎无增益）。
8. **AHI 是场景化杠杆**：高 CPU、读多、命中率高、线程密度大时，AHI 的 latch/维护成本
可能超过收益；低 CPU 或随机读为主时仍可能正收益。必须用 A/B 而非默认开关判断。
9. **排序与 collation 是只读长尾常客**：`ORDER BY`/`DISTINCT`/`GROUP BY` 触发的
`filesort` 会走 `my_strnxfrm_*`（生成 sort key）与 `my_hash_sort_*`（去重/哈希）；
utf8/utf8mb4 的逐字符处理在 aarch64 上是常见热点。
10. **参数分“仅初始化/在线/需重启/需重编译”**：`innodb_page_size` 仅初始化可设，只能在
新建实例初始化时确定，变更需新建实例并迁移数据，不能对已有实例改后重启；动态变量可
`SET GLOBAL`；buffer pool chunk、log file、doublewrite、线程池等多数需重启；补丁与
`-march` 必须重编译。

## 源码地图（8.0.x）

| 模块 | 路径 | 关键内容 |
| --- | --- | --- |
| 连接/线程 | `sql/conn_handler/`、`sql/sql_connect.cc` | 监听、认证、`handle_connection` |
| 解析 | `sql/sql_parse.cc`、`sql/parse_tree*` | SQL 解析、命令分派 |
| 优化器 | `sql/sql_optimizer.cc`、`sql/sql_select.cc` | 执行计划、join 顺序、条件下推 |
| 执行器 | `sql/sql_executor.cc`、`sql/iterators/` | 迭代器执行、临时表、filesort |
| handler 接口 | `sql/handler.cc`、`sql/handler.h` | Server 层与引擎边界 |
| InnoDB handler | `storage/innobase/handler/ha_innodb.cc` | `ha_innobase::index_read/update`、external_lock |
| B+Tree | `storage/innobase/btr/btr0cur.cc` | `btr_cur_search_to_nth_level` |
| 页内查找 | `storage/innobase/page/page0cur.cc` | `page_cur_search_with_match` |
| 记录 | `storage/innobase/rem/rec.cc`、`rem0cmp*` | `rec_get_offsets`、`cmp_dtuple_rec` |
| 行读取 | `storage/innobase/row/row0sel.cc` | `row_search_mvcc` |
| 事务/MVCC | `storage/innobase/trx/`、`read/read0read.cc` | `trx_sys`、`ReadView`、`MVCC` |
| 锁 | `storage/innobase/lock/` | 行锁、表锁、死锁检测 |
| Buffer Pool | `storage/innobase/buf/` | `buf_pool`、LRU、flush list、page cleaner |
| redo | `storage/innobase/log/` | log buffer、writer/flusher、checkpoint |
| undo | `storage/innobase/trx/trx0undo.cc`、`trx0purge.cc` | undo 页、purge |
| change buffer/AHI | `storage/innobase/ibuf/`、`btr/btr0sea.cc` | 二级索引缓存、自适应哈希 |
| binlog/组提交 | `sql/binlog.cc`、`sql/rpl_commit_stage_manager.cc` | binlog 写入、flush/sync/commit 阶段 |
| 字符集/排序 | `strings/ctype-utf8.cc`、`ctype-mb.cc` | `my_strnxfrm`、`my_hash_sort`、`my_strnncollsp` |
| 复制 | `sql/rpl_*`、`sql/log_event*` | dump/applier、relay log |
| 客户端/测试 | `client/mysql.cc`、`client/mysqltest.cc` | CLI、MTR 引擎 |
| MTR | `mysql-test/` | `mysql-test-run.pl`、`t/`、`r/`、`suite/` |

## 参考索引（渐进披露）

| 文件 | 何时读 |
| --- | --- |
| `references/overview-and-source-map.md` | 需要分层职责、内存结构、线程名、版本差异与源码定位时 |
| `references/query-execution-flow.md` | 追 point/range/join/排序查询、handler 边界、页内查找与行解码时 |
| `references/innodb-storage-and-locking.md` | 分析 redo/undo/doublewrite、Buffer Pool、锁等待、MVCC 与 group commit 时 |
| `references/threads-and-config-map.md` | 规划绑核、解释 CPU 分布、建立参数影响面与在线/重启分类时 |

## 输出契约

回答架构问题时给出四段，避免只给结论：

1. **链路**：客户端 → 连接/线程 → SQL 层 → handler → InnoDB → 存储结构（标出锁/IO 边界）。
2. **证据**：源码路径与函数名，必要时给 `文件:函数`；结论标注“实测/经验/官方”。
3. **影响面**：改动会触及哪些线程、锁、缓存、日志或持久化格式。
4. **验证入口**：指向对应 skill（部署/测试/热点/优化），不在此处执行。

## 维护约定

- 以 8.0.25/8.0.28 源码为真源；版本差异先核对 `sql/sys_vars.cc`、`storage/innobase/include/`。
- 线程名、参数默认值、百分比等易漂移信息标“实测基线”，不写成通用规格。
- 路径一律用 `${MYSQL_HOME}`、`${SRC_ROOT}`、`${DATA_ROOT}` 等变量，不写个人环境绝对路径。

## 完整性校验

MySQL 域十个 skill 的结构/链接/锚点/evals/敏感信息检查：

```
bash cases/skill-integrity/run.sh
# 产物 cases/skill-integrity/results/result.json；退出码 0 = 通过
```
