# pgGraph 生命周期与核心流程

本文件把每条链路拆成“入口 → 中间状态 → 数据结构 → 出口”，用于源码追踪和热点归因。

## 1. 构建：graph.build()

```
SQL: SELECT * FROM graph.build([mode])
  → sql_build.rs（graph/src/sql_build.rs，execute_build/execute_build_with_mode）
  → builder.rs build_graph()
     ① 读 catalog（注册表/边/过滤列）
     ② OOM 预检：pg_class.reltuples 估行数；~140B/节点 + ~5B/边
        超 graph.memory_limit_mb → graph.oom_action（error / readonly）
     ③ 阶段 1 加载节点
        对每个注册表：SPI 游标分批 SELECT pk[, filter][, tenant]
        每行：node_store.add_node(oid, pk) → node_idx
              resolution_insert(oid, pk, node_idx)
              filter 值编码 → FilterIndex
              tenant 值 → RoaringBitmap
        批量写 pg_temp.graph_build_nodes(table_oid, pk, node_idx)
        建索引 (table_oid, pk)、(pk, table_oid)
        finalize_resolution(): Builder → 排序数组
     ④ 阶段 2 解析边端点
        对每条注册边：SPI 游标 SELECT from_pk, to_pk[, weight][, label]
        UNNEST 批量 JOIN graph_build_nodes 把主键翻译成 node_idx
        双向边写两条：schema_reversed=false / true
        写 pg_temp.graph_build_edges
     ⑤ 阶段 3 流式建 CSR
        游标 SELECT ... ORDER BY source, target, type_id, schema_reversed
        SortedEdgeStoreBuilder.try_push() → offsets/targets/type_ids/weights
        派生反向 CSR 到堆
        engine.replace_edge_stores()
     ⑥ persist_on_build=true：写 .pggraph.tmp → fsync → rename
        → 立即 load_graph_file() 重载校验；失败即报 build 错误（fail-closed）
     ⑦ finish_build() → 返回 nodes_loaded / edges_loaded / build_time_ms /
        memory_used_mb / sync_mode / projection_mode
```

关键点：

- 边必须按 `(source, target, type_id, schema_reversed)` 排序才能建 CSR；排序在 PG 临时表完成，
  不全量进 Rust 堆。
- 双向边会让 `edges_loaded` 变成原始边数的 2 倍；报告里要说明口径。
- 构建期间会安装同步触发器（`sync_mode=trigger`），`NOTICE: trigger ... does not exist`
  之类消息通常是跳过不存在对象的正常提示。
- 构建中断可能留下 build 锁；先查 `pg_stat_activity` 再终止残留 backend，不要直接删文件。

## 2. 遍历：graph.traverse()

```
SQL 调用
  → 检查 graph.enabled / ACL / 参数
  → ensure_current_graph(): 必要时 maybe_auto_load() mmap .pggraph
  → resolve(seed_table, seed_id): resolution_delta 优先，再主索引二分
  → edge_types: String → u8 集合
  → apply_sync(): pending _sync_log → edge_buffer
  → reduce edge_buffer → overlay_inserts / overlay_deletes
  → 构造 BfsConfig{seed, max_depth, max_nodes, max_frontier,
                   edge_type_filter, filter_ops, tenant, overlay, direction}
  → 选邻居源：out=forward CSR，in=reverse CSR，any=forward（双向注册已保证可达）
  → bfs::execute[_with_neighbors]()
       frontier = [seed]
       while frontier:
         current = pop_front()
         for neighbor in OverlayNeighbors(current):
           candidate_allowed?  visited / edge_type / active / tx_deleted / tenant / filter
           visited.insert; depth/parent/parent_edge_type.set; push
           熔断：max_nodes / max_frontier
  → to_traversal_results(): node_idx → (table_oid, pk)、path、edge_path（String 分配）
  → 排序 (depth, node_table, node_id)
  → 分页
  → hydrate=true：SPI 回查源表 → JSONB
  → 返回 TraverseRow
```

`candidate_allowed` 的六重检查顺序（`bfs.rs`）：

1. `visited.contains(neighbor)` — RoaringBitmap。
2. `edge_type_filter` — HashSet。
3. `node_store.is_active(neighbor)` — 位图。
4. `tx_delta::node_deleted(neighbor)` — thread_local + RefCell。
5. `tenant_membership.contains(tenant, neighbor)` — 租户隔离（RoaringBitmap；`graph.enforce_tenant_scope` 开启时缺 tenant 直接报错）。
6. `filter_index.check_filters(...)` — 类型化数组。

`hydrate=false` 只返回图坐标；计数/内部组合/大遍历应关闭 hydrate。
`max_rows`、`max_depth`、`max_nodes`、`max_frontier` 是熔断器，压测时必须记录。

## 3. 搜索：graph.search()

与遍历相反，search **不读图工件**，而是对源表构造 SQL：

```
validate_search_request(): 从 catalog 找注册了 property_key 的表 + ACL
  → 对每个表生成：
      SELECT pk, col::text, to_jsonb(src.*)
      FROM table src
      WHERE col IS NOT NULL AND <mode 谓词>
      ORDER BY pk LIMIT candidate_limit
  → mode 谓词：
      contains: col::text ILIKE '%' || $1 || '%'（转义特殊字符）
      exact:    lower(col::text) = lower($1)
      prefix:   col::text ILIKE $1 || '%'
      token:    按非字母数字分词，所有 token 都要出现
  → 执行 SQL，拿候选行
  → SearchValueMatcher 在 Rust 二次核验（防 ILIKE/分词误命中）
  → 去重 (node_table, node_id) → 排序 → 分页
  → 返回 node_table, node_id, match_type, score=1.0, verified, node(JSONB)
```

要点：

- `score` 当前固定 1.0，不是相似度；没有 embedding/向量索引/ANN。
- 搜索列必须在 `add_table(columns:=...)` 注册；过滤列是另一套 `add_filter_column`。
- 性能取决于源表索引（表达式索引、`pg_trgm` GIN）；图工件帮不上 search。
- 返回图坐标，可直接作为 `graph.traverse` 的 seed。

## 4. GQL / Cypher

```
graph.gql(text, params)
  → gql/lexer.rs + parser.rs → AST
  → query/semantics.rs:
      标签 → table_oid；关系类型 → edge_type u8；
      属性引用 → 注册列；$param → params JSONB；
      id(node)=$x 或无歧义主键等值 → identity_lookup
  → query/logical_plan.rs:
      NodeScan / NodeLookup / Expand / OptionalExpand / WildcardPathRead / JoinRead
  → query/physical_plan.rs: row_cap、方向、类型集合
  → query/execute.rs:
      source_idx = engine.resolve(...) 或全表扫描
      expand_targets() 用 CSR 拿邻居，按 rel_type_id 过滤
      project GqlRow；超 execution_row_cap 截断/报错
  → query/value.rs: node/rel/path → JSONB
  → hydrate=true：SPI 回查属性
  → ORDER BY / SKIP / LIMIT / DISTINCT / 聚合
```

- `graph.cypher()` 走 `cypher/parser.rs`，再 `cypher/lower.rs` 落到同一 GQL IR。
- 调试函数（非 GUC，不要 SET/SHOW）：`graph.gql_explain(query text) RETURNS text`
  （sql_facade/gql.rs）与 `graph.cypher_explain()`——只做解析/绑定/计划展示、不执行，
  用于核对改写是否走预期链路。
- GQL 写（CREATE/SET/REMOVE/DELETE/MERGE）通过 SPI 改源表，由 PG 执行约束/触发器/RLS/MVCC，
  成功后更新后端覆盖层；要求 `mutable_overlay` + `graph.mutable_enabled=on`，
  拒绝在子事务内执行。
- 默认行数 cap 10000；LDBC 的三个 `gql_knows_*` 查询常因此失败，这是安全限制，不是环境故障。

## 5. 最短路径与连通分量

### shortest_path

- 全双向图：双向 BFS，从 source/target 同时扩散，每次扩较小 frontier，相遇即停；
  复杂度约 O(b^(d/2))。
- 含单向边：退化为单侧 BFS。
- 加权：Dijkstra + `BinaryHeap<Reverse<...>>`；要求合并后的加权 CSR，
  有 pending 覆盖层时报 `PG018`，需 `graph.vacuum()`/`graph.maintenance()`。
- 结果含 `step/node_table/node_id/edge_label/edge_weight/step_cost/total_cost`。

### component_stats / components

- 全图 O(V+E) 近似单遍：**UnionFind**（connected_components.rs）——顺序扫描边集合，对每条边调用 `uf.union(node, edge.target)` 合并节点集合；完成后按根节点聚合出各连通分量与 `component_sizes`。不是逐点 BFS/DFS。
- `component_stats()`、`components()`、`largest_component()`、`component(id)`、`isolated_nodes()`。
- 全图算法，大图建议只读副本/维护窗口；需要 graph-admin 权限。
- 热点通常是 UnionFind find/union、`node_deleted`、分量大小统计、结果行字符串化。

## 6. 同步与维护

```
源表 INSERT/UPDATE/DELETE
  → pgGraph 触发器 → graph._sync_log
  → 下次图查询前 apply_sync():
       按本图注册表 OID 过滤到 high-water mark
       INSERT → NodeStore 加节点 + resolution_delta
       UPDATE → 改 tenant/filter；PK 变更则旧墓碑 + 新插入
       DELETE → 节点墓碑 + edge-delete 覆盖层
       边变更 → edge_buffer
  → 遍历前 reduce edge_buffer → overlay inserts/deletes
  → OverlayNeighbors = CSR 切片（跳过 deletes）+ 追加 inserts
  → mutable_overlay：提交的边变更发布持久投影段，其他后端 reload manifest 可见
  → 事务内 GQL 写：TxGraphDelta 提供读己写，事务结束清空
  → edge_buffer 超 graph.edge_buffer_size：引擎转 read_only，需 vacuum/maintenance
```

维护操作：

| 操作 | 做什么 | 何时用 |
|------|--------|--------|
| `graph.build()` | 从源表全量重建 CSR | 注册变更、schema drift、格式升级、损坏 |
| `graph.vacuum()` | 重建 + 清理过期投影段 | edge_buffer 太大、加权路径 PG018 |
| `graph.maintenance()` | 重建 + 发布新投影代次 | 周期性维护（可 concurrently） |
| `graph.apply_sync()` | 只回放 pending sync，不重建 | 想读最新但不想全量重建 |
| `graph.reset()` | 清后端 Engine + 删图工件目录 | 彻底重置（源表不动） |

## 7. 失败与边界

| 现象 | 可能原因 | 核对入口 |
|------|----------|----------|
| `PG018` 加权路径失败 | pending 覆盖层，加权 CSR 未合并 | `graph.status()`、vacuum |
| GQL 行数超限 | `execution_row_cap` 10000 | 降低结果/加 LIMIT，记录口径 |
| `graph.status()` 为 0 但 traverse 可用 | status 报告内存构建元数据，不是工件状态 | 执行一次 traverse |
| 遍历结果少于 SQL JOIN | `max_rows`/`max_depth`/`max_frontier` 截断 | 放大限制后重测 |
| 重启后首次查询慢 | 每 backend 首次 mmap + 重建反向 CSR + CRC | 预热、连接池、加载优化 |
| 源表变更后读不到 | 未 `apply_sync`、事务未提交、`query_freshness` 策略 | `_sync_log`、freshness |
| 构建后新插入不可解析 | delta 未生效/事务本地 | 事务边界、apply_sync |
