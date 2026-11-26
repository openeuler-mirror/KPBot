# 场景热点与源码位置

> 下列百分比/延迟来自源资料的特定环境（PostgreSQL 18.3、pgGraph 0.1.8、aarch64、
> 特定 LDBC 图），仅用于建立假设；目标环境必须重新采集。数据集规模在源资料中有
> 3.18M/34.5M 与 100K/500K 两种口径，不可混用。

## 1. component_stats（全图连通分量）

调用链：

```
SQL → component_stats_wrapper → with_panic_boundary
  → Engine::connected_components（单行聚合：num_components/largest/isolated/total_active）
  → compute_components_with_neighbors
  → UnionFind::union → UnionFind::find
  → component_sizes 计数 / active_count
  （to_component_rows 的逐节点 primary_key().to_string() 只在
   graph.connected_components() 等逐行 API 中出现，不在本链）
```

已知热点（1c1t 实测，供定位参考）：

| 位置 | 占比/现象 | 机制 |
|------|-----------|------|
| `UnionFind::find` | ~30% CPU；~810K 次调用 | 递归路径压缩，函数调用/栈帧 + parent 数组读写 |
| 主循环 active/neighbor | ~18% | 读 active 位图、CSR、遍历所有边 |
| `tx_delta::node_deleted` | ~0.9%（但 ~460K 次） | thread_local + RefCell + Option 检查 |
| `component_sizes` HashMap | ~3.4% | 每节点 `entry().or_insert()` 哈希 |
| `to_component_rows`（仅逐行 API，不在本链） | 逐行 API 的结果行 String 分配 | 每节点主键 `to_string()`；component_stats 为单行聚合、无逐节点输出 |
| 结果输出 | component_stats 恒 1 行；逐行 API 1 行/分量或 1 行/节点 | 行数/JSON 开销只在逐行 API 出现 |

优化方向（见 performance skill）：

- UnionFind 迭代化 + path halving（实测 component -18.6%）。
- `node_deleted` 只读快速路径（component -19.6%）。
- component_sizes 从 HashMap 改 Vec（component -6.8%）。
- 逐行 API（connected_components 等）避免结果行不必要的 String/JSON；component_stats 单行聚合无此开销（优化方向勿张冠李戴）。

注意：component_stats 是全图 O(V+E)，大图上它天然比有界遍历慢；不要用它的绝对值
判断“图引擎整体慢”。

## 2. graph.traverse（1-hop / 2-hop）

调用链：

```
SQL → execute_traverse_candidates
  → ENGINE.with → traverse_with_filter_ops
  → resolve(seed) → BfsConfig
  → bfs::execute_with_neighbors
       candidate_allowed: visited / edge_type / active / tx_delta / tenant / filter
       visited.insert; depth/parent/edge_type.set; frontier.push
  → to_traversal_results: reconstruct_path + primary_key().to_string()
  → sort_by(depth, node_table, node_id String)
  → paginate + hydrate(SPI) + JSON + regclass_text
```

热点分层：

| 阶段 | 典型热点 | 触发条件 |
|------|----------|----------|
| BFS 热循环 | CSR 随机读、`candidate_allowed`、RoaringBitmap、active 位图 | 高度数节点、2-hop 扇出 |
| 结果转换 | `primary_key().to_string()`、路径 String、edge_type clone | 返回节点多、路径长 |
| 排序 | `sort_by` 的 String memcmp | 2-hop 返回 1000+ 行 |
| hydrate | SPI 规划/执行、palloc、读源表页、JSON | `hydrate=true`；2-hop 大量节点 |
| 格式化 | JSON 序列化、`regclass_text` 系统目录查找、String clone | 每行固定开销，短查询占比高 |

实测（1c1t，论坛/人物 2-hop）：

- String 排序约 15% CPU；`to_traversal_results` + String clone + JSON 合计显著。
- hydrate/SPI 约 11%，其中计划构建/palloc 约 7%。
- `candidate_allowed`/visited/状态写入各占几个百分点。
- 把排序键从 `node_id` String 改为 `node_idx` 后，2-hop 查询 -20~29%。

判读：

- 1-hop 很快、2-hop 慢：先看结果行数、排序、路径/String、hydrate，而不是 BFS 循环。
- 高度数节点：看 CSR 扇出、visited 去重、max_rows 截断。
- `hydrate=false` 明显更快：瓶颈在 SPI/JSON/属性回查。

## 3. graph.search

调用链：

```
validate_search_request → 对每个注册表生成 SQL 谓词
  → 执行（源表扫描/索引）
  → SearchValueMatcher Rust 二次核验
  → 去重/排序/分页 → hydrate(可选)
```

热点：

- 源表 `ILIKE '%...%'` 无索引 → 全表扫描；这是 SQL 问题，不是 CSR 问题。
- 返回候选过多 → Rust 二次核验/去重/排序成本。
- `hydrate=true` → 回查源表/JSON。
- `score` 固定 1.0，没有向量相似度；不要按“向量检索”优化。

方向：表达式索引/`pg_trgm` GIN、限制 `max_rows`、先 `hydrate=false` 过滤再 hydrate。

## 4. shortest_path / weighted_shortest_path

- 全双向：双向 BFS，frontier 较小侧扩展；热点是 CSR 邻居扫描、visited/parent 状态。
- 含单向：单侧 BFS，深度大时节点数指数增长。
- 加权：Dijkstra + BinaryHeap；热点是堆操作和边松弛。
- 有 pending overlay 时加权路径报 `PG018`：不是性能问题，先 vacuum/maintenance。
- 路径重建/String 输出与 traverse 类似；短路径查询的行数少，后处理占比低。

## 5. GQL / Cypher

```
lexer/parser → semantics → logical plan → physical plan → execute
  → NodeLookup/NodeScan → expand_targets(CSR) → project → JSONB → hydrate(可选)
```

热点：

- 解析/绑定：固定开销，短查询占比高；复杂 MATCH/多 pattern 更明显。
- `NodeScan` 全表扫描：没有 `id(node)=` 或主键等值时可能扫全部节点。
- `Expand`/`OptionalExpand`（EXPLAIN 计划节点，单跳为 hops 1..1）：expand_targets 做 CSR 邻居 + rel_type 过滤；高度数节点扇出大。
- 结果 JSONB/`execution_row_cap`：行数大时序列化/截断。
- GQL 写：SPI 改源表 + 触发器 + 覆盖层；写放大/约束检查是主要成本。

## 6. 构建与加载

构建：

```
SPI 读源表（游标/分批）→ NodeStore/Resolution/Filter/tenant
  → 临时 spool → UNNEST JOIN 解析端点 → 排序 → CSR → 反向 CSR
  → 写 .pggraph（tmp+fsync+rename）→ 重载校验
```

热点：

- 源表扫描/COPY 后的表膨胀、缺索引、外键检查。
- 临时表排序/UNNEST JOIN（边端点解析）。
- 排序后建 CSR（O(E log E)）。
- 写工件/CRC/重载校验（I/O + CPU）。
- 反向 CSR 派生（O(V+E)）。
- OOM 预检/内存不足导致降级。

加载（每 backend 首次）：

- mmap + CRC32 全文件扫描。
- CSR/主键/bincode 边界校验。
- 反向 CSR 重建。
- FilterIndex/registry 反序列化。

高并发 warmup 时多个 backend 同时加载，会看到 CPU/IO 抖动；计时阶段应预热。

## 7. 同步/写入

- 触发器写 `_sync_log`：每次源表 DML 的固定开销；批量写比逐行写更划算。
- `apply_sync`：按注册表 OID 回放到 high-water mark；日志积压时下一次查询变慢。
- `mutable_overlay`：发布持久投影段 + manifest 重载；写放大但跨 backend 可见。
- `csr_readonly`：覆盖层只在本 backend；其他 backend 看不到，直到 vacuum/maintenance。
- `edge_buffer` 超限：引擎转 read_only，后续写失败/需维护。
- GQL 写：SPI + PG 约束/触发器/RLS/MVCC；子事务不支持。

## 8. 并发/吞吐

高并发下的热点不一定在图算法：

| 层 | 现象 | 机制 |
|----|------|------|
| 客户端 | Python 进程 ~1 核、backend 大量 ClientRead | GIL/提交速率 |
| 连接 | backend 数暴涨、每 backend 私有内存叠加 | 反向 CSR/FilterIndex 复制 |
| 缓存 | LLC miss 升高、backend stall 升高、IPC 下降 | 私有工作集超过 L3/CCL |
| NUMA | 远端访问/跨节点延迟 | 内存未绑本地节点 |
| SMT | 偶/奇逻辑核利用率差异、IPC 下降 | 两线程共享执行单元 |
| PG | LWLock/BufferPin 等待 | shared_buffers/页竞争 |
| 加载 | warmup 阶段 CPU/IO 峰值 | 每 backend mmap+CRC+反向 CSR |

见 `topdown-and-contention.md` 的判别表；不要把所有“CPU 利用率低”都归因于 L3。

## 9. 源码定位速查

| 功能 | 文件/函数 |
|------|-----------|
| 构建 | `builder.rs:build_graph`、`edge_store.rs:SortedEdgeStoreBuilder` |
| 遍历 | `bfs.rs:execute_with_neighbors`、`candidate_allowed`、`to_traversal_results` |
| 路径 | `path_finder.rs:shortest_path_with_neighbors`、`bidirectional_bfs` |
| 分量 | `connected_components.rs:compute_components_with_neighbors`、`UnionFind::find/union` |
| 搜索 | `sql_search.rs:source_table_search_statements`、`SearchValueMatcher` |
| GQL | `gql/parser.rs`、`query/semantics.rs`、`query/execute.rs` |
| 持久化 | `persistence.rs:write_graph_file/load_graph_file` |
| 同步 | `sync.rs`、`sql_sync.rs`、`projection/neighbors.rs`、`projection/tx_delta.rs` |
| 结果格式化 | `sql_traversal.rs:paginate_and_format_traverse_candidates` |
| hydrate | `sql_hydration.rs:hydrate_nodes` |
