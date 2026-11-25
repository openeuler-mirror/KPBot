# GUC、Catalog 与一致性语义

## 1. GUC 分组

以下为 pgGraph 0.1.8 常见 GUC。**默认值以目标实例 `SHOW <name>` 和生成的
`graph--*.sql`/源码为准**；文档中的示例值不是通用规格。

### 构建与工件

| GUC | 作用 | 备注 |
|-----|------|------|
| `graph.memory_limit_mb` | 构建 OOM 预检上限 | 文档实测 LDBC SF1 约需 830MB，默认 2048；按数据集留余量 |
| `graph.build_batch_size` | SPI 游标分批大小 | 影响构建内存与往返；过大增加峰值 |
| `graph.oom_action` | 超限行为 | `error` 或降级只读，按版本核对 |
| `graph.persist_on_build` | 构建后是否写 `.pggraph` | 关闭则只保留内存图，重启需重建 |
| `graph.auto_load` | 查询时是否自动 mmap 工件 | 关闭则需显式 `load_graph` |
| `graph.data_dir` | 工件根目录（相对/绝对） | 默认通常为 `graph`，多图按 graph_id 分目录 |

### 运行模式与一致性

| GUC | 作用 | 备注 |
|-----|------|------|
| `graph.mutable_enabled` | 是否允许 GQL 写/可变覆盖层 | 写路径需要 `mutable_overlay` |
| `graph.edge_buffer_size` | 后端边增删缓冲上限 | 超限转 read_only，需 vacuum/maintenance |
| `graph.query_freshness` | pending sync 策略 | 三档：`apply_pending_sync`（**默认**，读前自动回放 pending sync）、`off`、`error_on_pending`（读到陈旧拓扑直接失败） |
| `graph.enabled` | 扩展总开关 | 关闭后 `graph.*` 调用失败/退化，按版本核对 |

注：`graph.gql_explain` / `graph.cypher_explain` **不是 GUC**，是 SQL 调试函数
（`graph.gql_explain(query text) RETURNS text`，sql_facade/gql.rs；只做解析/绑定/计划
展示、不执行）——不要对它们 SET/SHOW，登记见 lifecycle-and-flows 的 GQL/Cypher 链路。

### 租户与安全

| GUC | 作用 | 备注 |
|-----|------|------|
| `graph.enforce_tenant_scope` | 租户表查询必须带 tenant | 开启后遗漏 tenant 直接报错 |
| `graph.tenant_setting` | 会话级租户 GUC 名 | 如 `app.tenant_id`，配合 `SET` 使用 |

### PostgreSQL 侧（非 pgGraph GUC，但直接影响基准）

| 参数 | 作用 | 注意 |
|------|------|------|
| `shared_buffers` | PG 共享缓冲 | 与 mmap 工件是两套内存，不要混算 |
| `work_mem` | 排序/哈希内存 | 影响 SQL JOIN 基线与 hydrate 计划 |
| `max_connections` | 后端上限 | 每 backend 一份 Engine 私有内存，连接池比盲目加连接重要 |
| `jit` / `jit_above_cost` | JIT 编译 | 图查询主要是 C/Rust 函数调用；实测关闭约 1% 收益，需按版本复测 |
| `synchronous_commit` | WAL 同步 | 只读基准可关；生产写入不得为跑分牺牲持久性 |
| `effective_cache_size` | 优化器估算 | 影响 JOIN 基线计划，A/B 必须一致 |

## 2. Catalog 表

| 表 | 关键列 | 说明 |
|----|--------|------|
| `graph._registered_tables` | `table_name TEXT PRIMARY KEY, id_column, columns, tenant_column`（`graph_id` 为后加 UUID 列；以表名记录、构建期才解析成 OID——`table_oid` 只出现在 `graph._sync_log`） | 节点表注册 |
| `graph._registered_edges` | `graph_id, from_table, from_column, to_table, to_column, label, bidirectional, weight_column, label_column` | 边注册 |
| `graph._registered_filter_columns` | `table_name, column_name, column_type`（`graph_id` 同上；无 table_oid 列） | 遍历过滤列 |
| `graph._sync_log` | 源表 DML 的增量流 | 触发器写入，`apply_sync` 消费 |
| `graph._sync_buffer` | 待回放缓冲 | 版本/模式不同可能不存在 |
| `graph._build_jobs` | 异步构建任务 | `build_async_graph` |
| `graph._maintenance_jobs` | 维护任务 | `maintenance` |
| `graph._projection_generations` | 投影代次/manifest | `mutable_overlay` 增量段与 GC |

`graph.registered_tables()` / `graph.registered_edges()` 是面向用户的视图/函数，
排查时优先用它们；直接改 catalog 表会破坏一致性。

## 3. 过滤列类型与编码

| `column_type` | 编码 | 支持的谓词 |
|---------------|------|------------|
| `numeric` | i64 | gt/gte/lt/lte/eq/between |
| `boolean` | bool + 稀疏 bitmap | eq |
| `text` | intern 字典 token | eq/contains/prefix 等，按实现 |
| `date` | 距 2000-01-01 天数 | 比较/区间 |
| `timestamptz` | 距 epoch 微秒 | 比较/区间 |
| `uuid` | u128 | eq/比较 |

注意：搜索列（`add_table(columns:=...)`）和过滤列（`add_filter_column`）是两套，
不要以为注册了搜索列就能在 `traverse(filter:=...)` 里用。

## 4. 一致性与可见性

| 场景 | 语义 |
|------|------|
| 基础 CSR | 构建时快照，不可变 |
| 源表 DML | 触发器写 `_sync_log`；下次图查询前 `apply_sync` 回放 |
| 同一事务内 GQL 写 | `TxGraphDelta` 提供读己写，事务结束清空 |
| `csr_readonly` 的 overlay | 只在本 backend 有效，直到 vacuum/maintenance 重建 |
| `mutable_overlay` | 提交的边变更发布持久投影段，其他 backend reload manifest 可见 |
| hydrate | 通过 SPI 读源表，受 PG MVCC/RLS/ACL 约束；可能看到比图更新的源行。注意：**RLS 只覆盖回表读属性**；图拓扑读的是派生 CSR 快照（节点标识/邻接），不经源表逐行策略——源表 RLS 不能隔离拓扑可见性，租户/行级隔离须用 pgGraph 的 tenant 过滤或独立工件 |
| `query_freshness=error_on_pending` | 有未回放变更时读图直接报错，避免陈旧拓扑 |
| 加权路径 | 要求合并后的加权 CSR；pending 覆盖层时报 `PG018` |

**重要**：图是派生状态，遍历看到的是“工件 + 覆盖层”的拓扑；hydrate 看到的是源表行。
两者在同步延迟窗口内可能不一致。做正确性对账时必须固定事务边界和 `apply_sync` 状态。

## 5. 多图

- 同一数据库可有多个命名图，按 `graph_id` 隔离。
- 默认图：名称 `default`、命名空间 `public`。
- 常用函数：`build_graph`、`build_async_graph`、`build_status_for_graph`、`load_graph`、
  `unload_graph`、`set_current_graph`、`select_graph`。
- `add_table_to_graph` / `add_edge_to_graph` 可注册到指定图而不改会话选择。
- 多图会放大工件数量与每 backend 私有内存；压测时必须记录当前图。

## 6. 错误码与安全限制

| 码/现象 | 含义 | 处理 |
|---------|------|------|
| `PG018` | `UnsupportedOperation`（**通用**不支持操作，safety.rs） | 通用 UnsupportedOperation，按触发场景分流处置：加权路径遇 pending 覆盖层/加权 CSR 未就绪 → `graph.vacuum()`/`maintenance()`；GQL 写未开 `mutable_overlay` → `SET graph.mutable_enabled=on` 或改回 `csr_readonly` 模式；GQL DELETE 无注册边表 → 注册边表；readonly 模式 build → 换可写模式；子事务内 GQL 写 → 移出子事务；pgq/discovery 不支持的操作 → 按该入口语义——勿单义化为加权路径专属（mutable 场景按 vacuum 处置不对症） |
| GQL row cap exceeded (10000) | GQL 结果超过执行行数上限 | 收窄查询/加 LIMIT；记录口径；不要当环境故障 |
| NodeNotFound | seed 主键不在图里 | 核对注册、build、sync、表 OID |
| NotBuilt | 图未构建/未加载 | `build` 或 `load_graph` |
| ACL/权限错误 | 调用者无源表权限 | 授权或换用户；pgGraph 不绕过 RLS |
| `Address already in use` | PG 端口/其他服务冲突 | 换端口或停冲突进程（部署问题，不是图问题） |
| 工件校验失败 | magic/version/CRC/section 不合法 | 重新 build；不要手改工件 |

## 7. 排查顺序

1. 确认模式、路径、限制——注意 psql 里 `SHOW graph.*` 会语法错（SHOW 只接受单参数），
   用 `SELECT name, setting, unit FROM pg_settings WHERE name LIKE 'graph.%' ORDER BY name;`
   （或逐项 `SHOW graph.data_dir`）。
2. `graph.status()` + 一次 1-hop traverse，区分“元数据为 0”与“真的不可用”。
3. `graph.registered_tables()` / `registered_edges()` 核对注册。
4. 查 `_sync_log` 与 `graph.query_freshness`，确认是否读到陈旧拓扑。
5. 看 `.pggraph` 是否存在、大小、修改时间；必要时重新 build。
6. 再进入热点/性能分析，不要先改参数。
