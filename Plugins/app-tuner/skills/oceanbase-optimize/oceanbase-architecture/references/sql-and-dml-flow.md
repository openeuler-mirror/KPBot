# SQL 与 DML 执行链路

源码根 `${OB_SRC}`。本文给“从协议到存储”的可核对链路；函数名与文件路径用于定位，
不作为稳定 API 承诺。

## 1. SELECT 链路

以 `SELECT c1,c2 FROM t1 WHERE c1>10 ORDER BY c2 LIMIT 100` 为例。

```
① 协议层   ObMPQuery::process()             src/observer/mysql/obmp_query.h
            ObMPQuery::do_process()
② SQL 入口 ObSql::stmt_query()              src/sql/ob_sql.h
            （或 stmt_prepare()/stmt_execute()）
③ Parse    ObParser::parse()                src/sql/parser/
            ob_sql_parser_mysql_mode.y → StmtNode AST
④ Resolve  ObSelectResolver::resolve()      src/sql/resolver/dml/
            表名→table_id、列名→column_id、类型检查
⑤ Rewrite  ObTransformRule::transform()     src/sql/rewrite/
            视图展开、子查询改写、谓词下推、常量折叠
⑥ Optimize ObOptimizer::optimize()          src/sql/optimizer/ob_optimizer.h
             （ObSql::optimize_stmt() 是 SQL 层静态入口，见 src/sql/ob_sql.h）
            候选计划 → 代价估算（IO/CPU/网络）→ 选最优
⑦ Codegen  ObCodeGenerator::generate()      src/sql/code_generator/
            逻辑计划 → ObPhysicalPlan / Spec 链
⑧ Execute  ObExecutor::execute_plan()       src/sql/executor/
            ObExecContext（DAS_CTX / GET_PHY_PLAN_CTX / GET_MY_SESSION）
            算子 open → get_next_row → close
⑨ Scan     ObTableScanOp::get_next_row()    src/sql/engine/table/
            本地：MemTable/SSTable；远程：DAS RPC
⑩ 存储     ObMemtable::scan() / ObSSTable::scan()
            MVCC 可见性 + 列投影 + 微块解码 + 下推过滤
⑪ 返回     ResultSet → MySQL 协议编码 → 客户端
```

### 1.1 计划缓存（软解析）

```
ObSql::stmt_query()
  → ObPlanCache::get_plan()      src/sql/plan_cache/
      命中：直接执行 ObPhysicalPlan，跳过 ③–⑦
      未命中：完整编译并尝试入缓存
  → 淘汰：plan_cache_evict_interval（默认 5s）
```

计划缓存命中率下降会直接抬高 CPU。sysbench `--db-ps-mode=disable` 时每条 SQL 文本相同，
仍可命中计划缓存；但绑定变量/字面量差异、schema 变更会导致重新编译。
**计划缓存按租户隔离**：每个租户持有独立的计划缓存（容量/淘汰参数也是租户级，可在
`SHOW PARAMETERS LIKE '%plan_cache%'` 核对）——同文本 SQL 在不同租户不共享计划，
评估命中率时须按目标租户取数。

### 1.2 分布式查询与 DAS

```
ObTableScanOp
  ├─ 本地分区：直接访问本地 Tablet
  └─ 远程分区：DAS（Data Access Service，src/sql/das/）
       构造 DAS task → RPC 到目标 observer → 远端扫描 → 本地归并
```

- 归并迭代器：`ObDASMergeIter`（多路 rowkey 归并，热点函数常见）。
- 跨机/跨 Zone 的 DAS 会把“本地扫描”变成“网络 + 远端 CPU”，评估时要同时看两侧。

### 1.3 PX 并行

```
ObPxPool（每租户/资源组）
  Coordinator → 分片 → 多 Worker 扫描 → DTL 传输 → 汇总
```

PX 适合大表扫描/聚合；小查询开 PX 反而增加调度与 DTL 开销。并行度由
`px_workers_per_cpu_quota` 与租户 CPU 决定。

## 2. DML 链路（INSERT / UPDATE / DELETE）

```
①–⑦ 与 SELECT 相同（解析/改写/优化/代码生成）
⑧ ObTableInsertOp::inner_open()              src/sql/engine/dml/ob_table_insert_op.h
     inner_open_with_das()
⑨ 行处理 ObDMLService::process_insert_row()  src/sql/engine/dml/ob_dml_service.h
     类型转换、默认值、唯一键冲突检查、外键检查
⑩ 提交   ObDMLService::insert_row()          src/sql/engine/dml/ob_dml_service.h
            （两个重载；远程 DAS 走同文件的 write_row_to_das_op()）
     本地：直接写 MemTable；远程：DAS RPC
⑪ MemTable ObMemtable::set()                 src/storage/memtable/ob_memtable.h
     a. 分配事务回调（MemtableCallback，TX_CALLBACK_CTX）
     b. 写 B-tree 索引 + MVCC 行链（MEMSTORE_CTX，ObFifoArena）
     c. ObRedoLogGenerator 生成 redo
⑫ Redo   ObTxLSLogWriter → PALF 提交         src/storage/tx/ob_tx_ls_log_writer.h
     多数派确认 → 提交可见
⑬ 事务   ObTransService::commit_tx()         src/storage/tx/ob_trans_service.h
     ObPartTransCtx（2PC、ELR、standby 协调）
⑭ 回调   ObIEndTransCallback / ObEndTransAsyncCallback
                                          src/sql/ob_end_trans_callback.h
     释放锁、通知客户端
⑮ 清理   arena.reuse()（解析树/候选计划/Spec/行缓冲）
```

### 2.1 UPDATE / DELETE 差异

- **UPDATE**：读旧行 → 构造新行 → 写“删除标记（新版本）+ 新行”。额外成本是读旧行，
可能触发 SSTable IO 与 `MvccValueIterator::lock_for_read_inner_` 锁等待。
- **DELETE**：读旧行 → 只写删除标记，内存成本低于 INSERT，但同样走 redo/2PC。

### 2.2 写入延迟构成

```
客户端 → sql_nio 收包 → worker 解析/执行 → MemTable 写
      → redo 序列化 → PALF 复制/落盘 → 多数派
      → ApplySrv 推进可见性 → 回调 → 返回
```

insert 场景常见瓶颈顺序：**ApplySrv/序列化 > redo IO/转储 > worker 解析**。只加 worker
通常无效；先看 `T<tenant>_ApplySrv` CPU 与 redo 盘延迟。

## 3. 执行上下文与算子

`ObExecContext`（`src/sql/engine/ob_exec_context.h`）保存：

| 成员 | 用途 |
| --- | --- |
| `DAS_CTX` | 远程分区访问（RPC、并行） |
| `GET_PHY_PLAN_CTX` | 物理计划参数（并行度、超时） |
| `GET_MY_SESSION` | 会话变量、字符集、collation |
| `ObOperatorKit` | 每算子 spec + operator + input |

排序/哈希算子的工作区受 `_sort_area_size` / `_hash_area_size` 控制，超限会 dump 到磁盘。
sysbench read_only 的 `ORDER BY`/`DISTINCT` 会放大排序与字符比较（`ob_strnncollsp_utf8mb4`、
`ObSortOpImpl::Compare`），这类热点属于 SQL 语义与 collation 问题，不是“OS 参数”。

## 4. 参数对链路的影响

| 参数 | 影响点 | 观测 |
| --- | --- | --- |
| `_sort_area_size` / `_hash_area_size` | 排序/哈希是否落盘 | 算子 dump、临时空间、延迟 |
| `query_memory_limit_percentage` | 查询总内存上限 | 查询失败/降级 |
| `large_query_threshold` | 大查询判定与降级 | 大查询排队 |
| 租户 `min_cpu`/`max_cpu` / `cpu_quota_concurrency` | worker 数（4.x 走 min_worker_cnt 公式，见 memory-and-thread-model.md §4） | 排队时延、CPU 利用率；`workers_per_cpu_quota` 在 4.x 不驱动 worker 数 |
| `net_thread_count` | sql_nio 数 | 收包瓶颈、单线程 <数值>% |
| `enable_sql_audit` | 审计开销 | 高 QPS 下 CPU 抬升 |
| `trace_log_slow_query_watermark` | 慢查询追踪 | 日志量与开销 |
| `px_workers_per_cpu_quota` | 并行度 | 大查询吞吐/小查询开销 |

## 5. 核对清单

- 端口与租户：`mysql_port`/`rpc_port`/`proxy_port` 与实际监听一致。
- 计划是否命中：`GV$OB_PLAN_CACHE_PLAN_STAT`、执行计划是否反复编译。
- 是否远程：`EXPLAIN`/`GV$OB_SQL_AUDIT` 的 DAS 次数与远程等待。
- 是否落盘：排序/哈希算子 dump、临时空间增长。
- 写入是否卡在 ApplySrv/redo：线程 CPU + redo/IO 延迟 + 转储频率。
