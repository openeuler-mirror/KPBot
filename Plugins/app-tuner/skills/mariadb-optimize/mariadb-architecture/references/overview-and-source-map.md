# 模块地图、数据模型与引擎接口

本文件回答“每个模块负责什么、表数据长什么样、handler API 如何把 Server 层接到引擎、
10.11 与 10.3 的差异在哪里”。所有路径以 10.11.16 源码树为准。

## 1. Server 层职责

| 子模块 | 目录/文件 | 职责 | 典型热点 |
| --- | --- | --- | --- |
| 连接与协议 | `sql/sql_connect.cc`、`vio/` | 握手、认证、连接线程创建 | 连接风暴、TLS 握手、认证插件 |
| 会话 | `sql/sql_class.{h,cc}` | THD、会话变量、诊断区、kill | THD 初始化/销毁、内存分配 |
| 线程调度 | `sql/threadpool*.cc`、`sql/thread_cache.h` | 线程池或线程缓存 | worker 排队、stall、futex |
| 解析 | `sql/sql_yacc.yy`、`sql/sql_lex.cc` | 语法树、LEX | 复杂 SQL 解析 |
| 优化 | `sql/sql_select.cc`、`sql/opt_*.cc` | JOIN 顺序、range、索引选择 | range 估算、统计信息失真 |
| 执行 | `sql/sql_select.cc`、`sql/sql_union.cc`、`sql/filesort.cc` | 迭代执行、排序、临时表（执行器在 sql_select.cc，10.11 无 sql_executor.cc） | filesort、临时表落盘 |
| 表管理 | `sql/table.cc`、`sql/table_cache.cc` | 表定义缓存、打开表、MDL | `table_open_cache` 不足、MDL 等待 |
| 事务协调 | `sql/transaction.cc`、`sql/log.cc`、`sql/log_event.cc` | 2PC、组提交、binlog（10.11 无 sql/binlog.cc） | fsync、组提交队列 |
| 复制 | `sql/rpl_*.cc`、`sql/log_event*.cc` | dump/IO/SQL 线程、GTID | 单线程回放、大事务 |
| 监控 | `storage/perfschema/`、`sql/sql_show.cc` | performance_schema、SHOW | 采集开销 |

## 2. 存储引擎与选型

| 引擎 | 事务 | 崩溃安全 | 典型用途 | 关键代价 |
| --- | --- | --- | --- | --- |
| InnoDB | 是 | 是 | OLTP 主数据 | redo/双写/undo、页竞争 |
| Aria | 部分（crash-safe） | 是 | 内部临时表、系统表 | 非 MVCC 行锁粒度粗 |
| MyISAM | 否 | 否 | 只读/历史遗留 | 表锁、崩溃需 repair |
| MEMORY | 否 | 否 | 小热表/临时 | 重启丢失、全表锁 |
| MyRocks | 是 | 是 | 写密集、高压缩 | compaction、读放大 |
| ColumnStore | 是 | 是 | 分析/列存 | 独立服务、写入路径不同 |
| Spider | 视后端 | 视后端 | 分片/联邦 | 网络与两阶段提交 |
| Connect/FederatedX | 视后端 | 视后端 | 外部数据源 | 远程访问延迟 |

选型原则：

- 需要事务与崩溃恢复 → InnoDB（默认）。
- 写入极高且能接受 LSM 读放大 → MyRocks（需 `rocksdb-perf-diagnostics` 配合）。
- 分析型大扫描、列裁剪收益大 → ColumnStore（独立部署，不混 OLTP 实例）。
- 临时表/内部排序溢出 → Aria（由 `tmp_table_size`/`max_heap_table_size` 决定何时落盘，
  落盘引擎为 Aria，可用 `aria_used_for_temp_tables` 核对；`internal_tmp_disk_storage_engine`
  是 MySQL 参数，10.11 无此选项）。

## 3. 数据模型与物理组织（InnoDB）

- **表空间**：系统表空间 `ibdata1`（可含 undo）、每表文件表空间 `*.ibd`、
通用表空间、临时表空间 `ibtmp1`。
- **页**：默认 16KB（`innodb_page_size` 建库时确定，不可在线改）。
- **聚簇索引**：主键即数据组织；无显式主键时用唯一非空索引，否则内部 6 字节 row id。
- **二级索引**：叶子存索引列 + 主键，回表按主键再查聚簇索引。
- **记录格式**：`COMPACT`/`DYNAMIC`（10.11 默认 `DYNAMIC`），大字段溢出到
off-page；`innodb_default_row_format` 影响溢出与页密度。
- **隐藏列**：`DB_TRX_ID`、`DB_ROLL_PTR`、`DB_ROW_ID`（无主键时）支撑 MVCC 与回滚。

## 4. handler API：Server 与引擎的契约

`sql/handler.h` 定义抽象基类，引擎实现 `ha_*`：

| 接口 | 作用 | InnoDB 实现位置 |
| --- | --- | --- |
| `open()` / `close()` | 打开/关闭表，绑定表定义 | `ha_innobase::open` |
| `info()` | 行数、删除标记、扫描成本 | `ha_innobase::info` |
| `rnd_init/rnd_next` | 全表/全索引扫描 | `ha_innobase::rnd_*` |
| `index_init/index_read` | 索引范围/等值读 | `ha_innobase::index_*` |
| `write_row/update_row/delete_row` | DML | `ha_innobase::*_row` |
| `external_lock` | 语句级加锁/事务边界 | `ha_innobase::external_lock` |
| `start_stmt/commit/rollback` | 事务钩子 | 事务桥接 |
| `truncate/delete_all_rows` | DDL/清空 | `ha_innobase::truncate` |
| `analyze` | 统计信息 | `ha_innobase::analyze` |

要点：

- 优化器通过 `handler::records`/`scan_time`/`read_time` 等估算成本；统计信息不准会选错
索引，表现为“执行计划对但很慢”。
- `external_lock` 是 MDL 与引擎锁的衔接点；DDL 等待常表现为 MDL，而不是 InnoDB 行锁。
- 引擎层不做 SQL 语义（表达式、排序、连接），这些在 Server 层完成；下推（ICP/条件下推）
是例外，需看具体引擎支持。

## 5. 10.11 与 10.3/10.4 的关键差异（移植优化前必读）

| 主题 | 10.3/10.4 | 10.11.16 | 影响 |
| --- | --- | --- | --- |
| InnoDB latch | `rw_lock_t` + `lock_word`/`s_lock_word`（`sync0rw.h`） | `rw_lock` 类，`std::atomic<uint32_t> lock`（`include/rw_lock.h`）；`srw_lock`/`sux_lock` 模板 | 老的“拆分 lock_word/顺序锁”补丁不能直接套用 |
| 目录结构 | `storage/innobase/` 单层 | `buf/ log/ trx/ lock/ sync/ row/ btr/ page/ ...` 子目录 | 源码定位与补丁路径变化 |
| redo | `ib_logfile0/1` 固定文件（≤10.7） | `ib_logfile0` 单文件（10.8 起）+ 在线 resize（`innodb_log_file_size` 动态）；数据目录中的 `ib_logfile1` 属 ≤10.7 升级遗留，无 MySQL 8.0 的 `#innodb_redo/` 目录 | 调 redo 不必重启，但需观察 resize 过程 |
| 默认 flush | 依版本/平台 | `innodb_flush_method` 默认 `O_DIRECT` | 与 MySQL 默认不同，双重缓存假设要改 |
| 默认 redo 大小 | 依版本 | `innodb_log_file_size` 默认 96MB | 写密集必须先评估 redo 容量 |
| 统计/优化器 | 旧代价模型 | 持续演进，`optimizer_switch` 选项不同 | 升级后执行计划可能变化 |

> 结论：涉及 InnoDB latch/锁的源码优化，必须在 10.11 重新抓热点、重新设计；
> 10.3 的实测收益只能作为“方向性证据”，不能作为“现成结论”。

## 6. 源码定位速查

```
SRC_ROOT=${SRC_ROOT:-/opt/src/mariadb-10.11.16}

# 参数声明（默认值/范围/是否可动态修改）
grep -n "MYSQL_SYSVAR" "${SRC_ROOT}/storage/innobase/handler/ha_innodb.cc" | head
grep -n "MYSQL_SYSVAR" "${SRC_ROOT}/sql/sys_vars.cc" | head

# 线程模型
grep -n "thread_handling\|thread_pool_size\|pool-of-threads" "${SRC_ROOT}/sql/mysqld.cc"
grep -rn "thread_pool_stall_limit" "${SRC_ROOT}/sql/"

# 2PC / 组提交
grep -n "group_commit\|binlog_commit_wait" "${SRC_ROOT}/sql/log.cc"

# InnoDB 关键结构
grep -n "class rw_lock\|class srw_lock\|class sux_lock" \
  "${SRC_ROOT}/storage/innobase/include/"{rw_lock,srw_lock,sux_lock}.h
```

## 7. 输出要求

回答“某功能在哪实现”时，给出：模块 → 文件 → 函数 → 上游调用者 → 下游依赖，
并标注是否跨线程/跨进程/跨磁盘边界；不要只给一个文件名。
