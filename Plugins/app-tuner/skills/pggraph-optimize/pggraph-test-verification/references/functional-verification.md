# 正确性验证：从冒烟到独立 Oracle

## 1. 验证层级

```
L0 构建/扩展：产物存在、版本正确、pg_extension 可见
L1 单测：cargo pgrx test / cargo test / heavy tests
L2 冒烟：最小表 → 注册 → build → traverse/search → reset
L3 独立 Oracle：SQL JOIN / 递归 CTE / 图不变量 / 已知答案
L4 结果一致性：同查询多次/多版本 checksum
L5 回归：固定数据集 + 固定查询集的 row_count/checksum/延迟
```

L4 不能替代 L3：checksum 只证明“两次结果相同”，不证明“结果正确”。

## 2. 单测与冒烟

```bash
cd "${PGGraph_SRC}/graph"
cargo pgrx test "${PG_FEATURE}"       # pgrx SQL 测试（discovery/traversal/gql/...）
cargo test --no-default-features --features "${PG_FEATURE}"
```

Heavy 测试（需要完整环境）：

```bash
cd "${PGGraph_SRC}/graph"
PG_VERSION_FEATURE="${PG_FEATURE}" ./tests/heavy/run_release_gate.sh
./tests/heavy/run_pg_matrix.sh
DBNAME=test_db PG_VERSION_FEATURE="${PG_FEATURE}" ./tests/heavy/fresh_install_smoke.sh
```

最小冒烟 SQL：

```sql
CREATE SCHEMA IF NOT EXISTS pggraph_smoke;
CREATE TABLE pggraph_smoke.node (
  id text PRIMARY KEY,
  name text NOT NULL,
  parent_id text REFERENCES pggraph_smoke.node(id)
);
INSERT INTO pggraph_smoke.node VALUES
  ('root','Root',NULL), ('child','Child','root');

SELECT graph.add_table('pggraph_smoke.node'::regclass, 'id', ARRAY['name']);
SELECT graph.add_edge('pggraph_smoke.node'::regclass, 'parent_id',
                      'pggraph_smoke.node'::regclass, 'id', 'parent', false);
SELECT * FROM graph.build();
SELECT * FROM graph.traverse('pggraph_smoke.node'::regclass, 'child', 1,
                                direction := 'out', include_start := false);
-- include_start 默认返回深度 0 的起点；对账邻居时显式关掉。双向注册（bidirectional=true）
-- 会补反向边，SQL 对照须对源、目标两侧的邻居取并集，不能只匹配正向列。
SELECT * FROM graph.search('name', 'Child', table_filter := 'pggraph_smoke.node'::regclass);
```

清理：

```sql
SELECT graph.reset();
DROP SCHEMA pggraph_smoke CASCADE;
```

注意：`graph.reset()` 会清当前图的 Engine/工件，不删源表；多图环境要确认当前图。

## 3. 独立 Oracle：SQL JOIN

以 LDBC 1-hop `knows` 为例，图查询与纯 SQL 必须返回同一邻居集合：

```sql
-- 选一个高出入度 seed
SELECT src_person_id
FROM ldbc.person_knows_person
GROUP BY src_person_id
ORDER BY count(*) DESC
LIMIT 1;
```

图侧：

```sql
SELECT node_id
FROM graph.traverse(
  'ldbc.persons'::regclass, :'seed', 1,
  edge_types := ARRAY['knows'],
  direction := 'out',
  include_start := false,      -- 与 SQL 对照覆盖相同邻居：起点不计入
  hydrate := false,
  max_rows := 100000
)
ORDER BY node_id;
-- 如 traverse 返回 node_table 列，可再按表过滤；knows 边已限定为 persons。
```

SQL 侧（knows 以 bidirectional=true 注册，图内有补出的反向边——须按源、目标
两侧邻居取**并集**，只读正向列时 seed 仅出现在反向边目标端的邻居会漏掉）：

```sql
SELECT DISTINCT node_id FROM (
  SELECT dst_person_id AS node_id
  FROM ldbc.person_knows_person
  WHERE src_person_id = :'seed'
  UNION
  SELECT src_person_id AS node_id
  FROM ldbc.person_knows_person
  WHERE dst_person_id = :'seed'
) neighbors
ORDER BY node_id;
```

对账：

```bash
psql ... -At -c "<graph sql>" > /tmp/graph_ids.txt
psql ... -At -c "<sql sql>"   > /tmp/sql_ids.txt
diff -u /tmp/sql_ids.txt /tmp/graph_ids.txt
```

要求：

- 同一 seed、同一方向、同一 edge type、同一表过滤。
- `max_rows` 足够大，避免截断造成假阳性差异。
- 双向注册时，图内既有正向也有补出的反向边：SQL 对照须对源、目标两侧邻居取**并集**；
  `direction='out'` 对应并集后的出向视角，要测入向需用 SQL 的反向条件。
- 结果排序后再 diff；图默认按 `(depth, table, node_id)` 排序，SQL 要显式 `ORDER BY`。

## 4. 图不变量

### 4.1 边数

```sql
-- 边数口径：graph.status() 统计**全部已注册关系**。仅注册 knows（双向）时才等于
-- count(person_knows_person) × 2；注册了其他关系表时，须按每张表的有效行数与各自的
-- bidirectional 设置（true ×2 / false ×1）逐张加总后再比较，否则正确的图也会验收失败。
SELECT (SELECT edge_count FROM graph.status()) AS graph_edges,
       (SELECT count(*) FROM ldbc.person_knows_person) * 2 AS expected_knows_only;
```

注意 `graph.status()` 的口径；若 status 为 0，用 `report.json.build_output.edges_loaded`
或 `graph.traverse` 结果间接核对。

### 4.2 双向对称性

```sql
SELECT count(*) AS asymmetric
FROM ldbc.person_knows_person a
WHERE NOT EXISTS (
  SELECT 1 FROM ldbc.person_knows_person b
  WHERE b.src_person_id = a.dst_person_id
    AND b.dst_person_id = a.src_person_id
);
```

LDBC 的 knows 原始数据不一定对称；注册为双向后，图内应可双向到达。要区分“源表对称”
与“图注册双向”，不要用源表不对称直接判图错误。

### 4.3 节点总数（total_active_nodes）

-- 口径：component_stats() 的 total_active_nodes 是节点总数；num_components 才是
-- 连通分量数（有边时远小于节点总数）——本条不变量是“节点总数=8 表行数之和”。
```sql
SELECT (SELECT total_active_nodes FROM graph.component_stats()) AS node_total,
       (SELECT count(*) FROM ldbc.persons)
     + (SELECT count(*) FROM ldbc.posts)
     + (SELECT count(*) FROM ldbc.comments)
     + (SELECT count(*) FROM ldbc.forums)
     + (SELECT count(*) FROM ldbc.tags)
     + (SELECT count(*) FROM ldbc.tagclasses)
     + (SELECT count(*) FROM ldbc.places)
     + (SELECT count(*) FROM ldbc.organisations) AS table_total;
```

若存在墓碑/事务删除，分量总数可能小于表总数；要在无并发写的稳定状态测量。

### 4.4 最短路径

用递归 CTE 作为独立实现：

```sql
WITH RECURSIVE bfs AS (
  SELECT dst_person_id AS target, 1 AS depth
  FROM ldbc.person_knows_person WHERE src_person_id = :'src'
  UNION ALL
  SELECT e.dst_person_id, b.depth + 1
  FROM ldbc.person_knows_person e
  JOIN bfs b ON e.src_person_id = b.target
  WHERE b.depth < :'max_depth'
)
SELECT min(depth) AS sql_shortest FROM bfs WHERE target = :'dst';
```

与 `graph.shortest_path(...)` 的路径长度对比；同时检查路径边是否真实存在、
方向是否正确、是否有重复节点。加权路径要另算权重和，不能只比跳数。

## 5. checksum 的真实含义

hot benchmark 的 `result_checksum` 生成方式：

```sql
SELECT count(*)::bigint AS row_count,
       md5(coalesce(string_agg(row_hash, '' ORDER BY row_hash), '')) AS result_checksum
FROM (
  SELECT md5(row_to_json(benchmark_query)::text) AS row_hash
  FROM (<原始查询>) AS benchmark_query
) AS benchmark_rows;
```

性质：

- 行顺序无关：行哈希先排序再拼接。
- 行级粒度：单行变化会改变最终 checksum。
- 空结果：`coalesce` 保证返回 `md5('')`。
- 包含所有列：列顺序/类型/值变化都会影响。

能力边界：

| 能力 | 说明 |
|------|------|
| 检测优化引入的行为变化 | 前后 checksum 不同 → 行为变了 |
| 检测非确定性 | 多次运行 checksum 不一致 → 并发/随机/数据变化 |
| 不能证明正确 | 原始实现可能有 bug，错误结果也能稳定 checksum |
| 不能检测共性 bug | 前后都错，checksum 仍一致 |
| throughput 没有 checksum | 只测 wall time，不记录结果内容 |

`ok: True` 只表示查询没抛异常，不代表结果正确。

## 6. A/B checksum 与 Oracle 的组合

| SQL JOIN Oracle | A/B checksum | 结论 |
|-----------------|--------------|------|
| 一致 | 一致 | 优化未改变行为，且与独立实现一致 → 可接受 |
| 一致 | 不一致 | 优化改变了行为；用 Oracle 判断谁对 |
| 不一致 | 一致 | 原始实现可能就有 bug，优化保留了同样的 bug → 不可接受 |
| 不一致 | 不一致 | 优化引入新 bug 或修了旧 bug；逐查询排查 |

A/B 比较时要求：

- 同一份数据、同一 build 模式、同一查询 SQL/limit/order。
- 同一事务/同步状态；不要一边 pending sync 一边已回放。
- 同一 seed；LDBC 的 seed 必须固定并记录。
- 同一 `hydrate` 设置；hydrate 会回查源表，结果包含属性。
- 图查询与 SQL 的 limit 相同；否则 checksum 不可比。

## 7. 功能回归清单

- [ ] 8 个节点表、23 个边表注册数量正确。
- [ ] `graph.status()` 可调用；1-hop traverse 返回 ≥1。
- [ ] 1-hop 图结果与 SQL JOIN 邻居集合一致（至少 3 个 seed：高度数/低度数/孤立）。
- [ ] 2-hop 结果行数与 `max_rows` 截断关系符合预期。
- [ ] `graph.search` 四种 mode 各有一个正例/反例；`verified=true`。
- [ ] 最短路径与递归 CTE 长度一致；路径边存在（**CTE 须按注册口径展开**：knows 以
  bidirectional=true 注册时图内含补出的反向边——原始表只存一个方向的边，图内
  1 跳可达的对，正向 CTE 会给更长路径或查不到，把正确的图误判“不一致”。
  双向注册时把展开改为
  `JOIN bfs b ON e.src_person_id = b.target OR e.dst_person_id = b.target`
  并对目标端相应取另一侧，即并集口径，与 §3 的 1-hop 对账同款处理）。
- [ ] 节点总数（total_active_nodes）与 8 表行数之和在稳定状态下一致。（分量数校验另立：num_components ≤ 节点总数）
- [ ] GQL 查询：标量/坐标/hydrated 三种；超限时错误信息符合预期。
- [ ] 多图/租户/过滤列（如启用）各有一个正例。
- [ ] 同步：INSERT/UPDATE/DELETE 后 `apply_sync`，读己写/跨后端可见性符合模式。
- [ ] 重建：`graph.reset` + 重新 build 后结果一致。
- [ ] 工件复用：重启/重编译后 traverse 结果一致（格式未变）。
