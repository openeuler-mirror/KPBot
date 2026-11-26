# 连接、查询、事务与复制链路

本文件把一条 SQL 从客户端到磁盘、以及一次提交如何落 redo/binlog 的路径拆开，
用于源码追踪和热点归因前的链路核对。

## 1. 连接生命周期

```
connect(TCP 3306 / socket)
  → vio 读握手 → 认证插件 → 创建 THD（sql/sql_class.cc）
  → 分配线程：one-thread-per-connection（默认）或 pool-of-threads
  → handle_one_connection / threadpool worker 进入命令循环
  → COM_QUERY / COM_STMT_EXECUTE → dispatch_command（sql/sql_parse.cc）
  → 结果集写回 → 等待下一条命令
  → KILL/断开 → THD 销毁、回滚未提交事务、释放 MDL/行锁
```

要点：

- **每连接一线程**意味着 `max_connections` 直接对应线程数与栈内存；高并发短查询时
线程创建/销毁与调度开销显著，可改用线程池。
- **线程池**限制并发 worker，长查询会占住 worker；`thread_pool_stall_limit` 单位是
毫秒（ms），表示停滞检查的间隔：值越大检查越稀疏，越晚把长查询判为 stalled
并补充 worker，短请求排队时反而可能更晚拿到 worker（“慢查询 + 高并发”场景
不能盲目调大）。调整后用排队时间与停滞观测结果验证：对比
`INFORMATION_SCHEMA.THREAD_POOL_QUEUES` / `THREAD_POOL_STATS` 的队列与 stall
计数、请求排队耗时；若排队变长、stall 变少说明短请求被延后，应回退。
- **连接池在客户端**，服务端看到的连接数与并发度取决于客户端池配置；压测必须记录。

## 2. 查询执行链路（SELECT）

```
SQL 文本
  → 词法/语法（sql_yacc.yy）→ LEX/语法树
  → prepare：解析表、列、权限、生成 TABLE_LIST
  → optimize（sql/sql_select.cc、sql/opt_*.cc）
      ├─ 条件重写、等值传播、常量折叠
      ├─ 访问路径：全表 / range / index / ref / eq_ref
      ├─ JOIN 顺序与 semijoin/antijoin 策略
      └─ 排序/分组：索引有序 or filesort / 临时表
  → execute（sql/sql_select.cc，10.11 无 sql/sql_executor.cc）
      ├─ 打开表（table_cache、MDL）
      ├─ handler::index_init/index_read 或 rnd_next
      ├─ 条件下推（引擎支持时）
      └─ 组装结果、LIMIT
  → 发送结果（协议层）
```

InnoDB 读路径：

```
handler::index_read / rnd_next
  → 定位 B+tree（btr0cur.cc）
  → 页在 buffer pool？（buf0buf.cc：hash 查找 + LRU）
      命中 → 直接读
      未命中 → 从磁盘读页（可能触发预读 buf0rea.cc）
  → 记录解析（row0sel.cc / row0row.cc）
  → 可见性判断：read view + undo（trx0trx.cc、row0vers.cc）
  → 返回行
```

常见性能含义：

| 现象 | 链路位置 | 证据 |
| --- | --- | --- |
| 逻辑读高但 QPS 低 | 执行器/handler 迭代次数多 | `Handler_read_*`、`Innodb_rows_read` |
| 物理读高 | buffer pool 未命中 | `Innodb_buffer_pool_reads`、`innodb_buffer_pool_read_requests` |
| 临时表/落盘 | filesort/临时表 | `Created_tmp_disk_tables`、`Sort_merge_passes` |
| 打开表慢 | table cache/MDL | `Table_open_cache_misses`、`wait/xxx/sql/mdl` |
| 回表多 | 二级索引选择性差 | 执行计划 Extra 是否 `Using index`（覆盖索引）、慢日志 `Rows_examined` 与发送行数倍差、`Innodb_rows_read` 与 Handler 计数差（`Handler_read_rnd_next` 仅计全表扫描，不作回表证据） |

## 3. 写入与提交链路（DML）

```
INSERT/UPDATE/DELETE
  → 解析/优化/执行
  → InnoDB：定位聚簇索引页 → 生成 undo → 修改页（脏页）
  → 写 redo（log buffer → log file）
  → 行锁/间隙锁维护（lock0lock.cc）
  → COMMIT：
      ① redo prepare（InnoDB）
      ② 写 binlog（server 层，受 sync_binlog 影响）
      ③ redo commit（InnoDB）
  → 后台：page cleaner 刷脏页、purge 清理 undo
  （change buffer：10.11 默认 `innodb_change_buffering=none`，10.9 起弃用但设置仍生效并告警，11.0 移除）
```

关键参数与边界：

| 参数 | 作用 | 风险 |
| --- | --- | --- |
| `innodb_flush_log_at_trx_commit` | 提交时 redo 刷盘策略（1/2/0） | 非 1 会丢最近事务 |
| `sync_binlog`（10.11 默认 0，OS 安排刷盘） | binlog 刷盘策略 | 非 1 崩溃可能丢 binlog；用 `SELECT @@GLOBAL.sync_binlog` 核对实例配置 |
| `innodb_doublewrite` | 部分页写保护 | 关闭提升写性能但降低崩溃安全 |
| `innodb_io_capacity` | 后台 IO 速率上限 | 过低导致刷脏跟不上、checkpoint 压力 |
| `binlog_commit_wait_count`（默认 0） | 组提交等待的事务数 | 提高并发时合并 fsync；>0 后凑不满 count 个事务会等到 usec 时限 |
| `binlog_commit_wait_usec`（默认 100000 微秒 ≈ 100ms） | 等待时限，仅 count > 0 时生效 | 只提高 count、保留默认 usec 时，低并发提交可能额外等待约 100ms |

**2PC 的崩溃语义**：恢复时若 binlog 中事务完整，则 redo 前滚；若不完整，则回滚。
因此 redo 与 binlog 的持久化级别必须一起看，不能只调一个。

## 4. 复制链路

```
主库：事务提交 → binlog（log.cc）
  → dump 线程读 binlog → 发送
从库：IO 线程写 relay log
  → SQL 线程（或并行 worker）回放
  → 更新从库数据
```

要点：

- **异步复制**默认；半同步需插件，牺牲延迟换可靠性。
- **GTID** 简化位点管理；并行复制按组提交/库维度并行，主库组提交质量影响从库并行度。
- **大事务**会造成从库长事务、延迟尖刺与 purge 滞后；拆分事务比调参数更有效。
- **Galera** 是同步多主（写集认证），提交延迟受最慢节点与网络影响，与异步复制模型不同。

## 5. 一次查询的证据采集点

| 层 | 命令/表 | 看什么 |
| --- | --- | --- |
| 会话 | `SHOW FULL PROCESSLIST` | 状态（Sending data/Updating/statistics） |
| 语句 | `performance_schema.events_statements_summary_by_digest` | 总耗时、扫描行、临时表 |
| 等待 | `performance_schema.events_waits_summary_global_by_event_name` | 锁/IO/网络等待占比 |
| 引擎 | `SHOW ENGINE INNODB STATUS` | 锁等待、死锁、历史列表长度 |
| 计数器 | `SHOW GLOBAL STATUS LIKE 'Innodb_%'` | 逻辑读/物理读/行操作 |
| 优化器 | `EXPLAIN`（DML 只读检查仅用 EXPLAIN） | 访问路径、估算行数、是否 filesort |

> 注意：MariaDB 的 `ANALYZE` 会**实际执行原语句并对行计时**；对 `UPDATE`/`DELETE`
> 用 `ANALYZE` 会真的修改/删除匹配行，不能当只读检查。DML 的只读执行计划检查
> 必须只用 `EXPLAIN`（`EXPLAIN UPDATE/DELETE` 不执行语句）。
> 另外 `performance_schema` 在 MariaDB 默认可能未开启（`performance_schema=OFF`），
> 依赖它之前先 `SHOW VARIABLES LIKE 'performance_schema'`，并评估开启开销。

## 6. 输出要求

描述链路时必须区分：**用户态计算 / 等锁 / 等 IO / 等网络 / 等日志刷盘**，
并给出对应证据；不要把“慢”笼统归因到“数据库”。

