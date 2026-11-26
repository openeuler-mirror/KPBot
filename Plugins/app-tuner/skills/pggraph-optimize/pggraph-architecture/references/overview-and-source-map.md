# pgGraph 总览与源码地图

## 1. 定位与边界

pgGraph 是 PostgreSQL 的扩展，不是独立图数据库：

- **源表 = 真相**：节点来自普通表，边来自外键或纯边表；PG 的约束、索引、ACL、RLS、
  MVCC、WAL、备份全部照常生效。
- **图 = 派生状态**：`graph.build()` 从源表编译出 CSR；图工件可丢弃、可重建，
  不替代 PG 的持久化与恢复。
- **SQL 是唯一公共 API**：遍历、搜索、路径、GQL/Cypher 都通过 `graph.*` 函数调用；
  GQL/Cypher 只是文本兼容面，最终落到同一执行模型。

与图数据库/Apache AGE 的差异：不搬数据、不引入 vertex/edge 表、不要求新查询语言；
优势是“对已知拓扑反复做有界深度、带路径/过滤/租户/分页的遍历”，而不是通用图数据库。

## 2. 运行时组成

```
PostgreSQL postmaster
  ├─ backend 1 ── Engine(thread_local) ── mmap .pggraph（共享 page cache）
  ├─ backend 2 ── Engine(thread_local) ── 各自的 reverse CSR / FilterIndex（私有堆）
  ├─ ...
  └─ background worker（异步 build/maintenance，携带 database/user/graph 上下文）
```

| 组件 | 归属 | 共享方式 | 影响 |
|------|------|----------|------|
| 前向 CSR / NodeStore / Resolution 主段 | `.pggraph` 文件 | 只读 mmap，OS page cache 跨 backend 共享 | 启动快，物理页可共享 |
| 反向 CSR | 每个 Engine | 从正向 CSR 重建到堆 | 每 backend 私有内存、加载 CPU |
| FilterIndex / edge_type_registry | 每个 Engine | bincode 反序列化到堆 | 每 backend 私有内存 |
| BFS/DFS 状态 | 每次查询 | 后端本地分配 | 查询级内存，短生命周期 |
| `edge_buffer` / `TxGraphDelta` | 每个后端/事务 | 不共享 | 覆盖层语义、读己写 |
| catalog 表 | PostgreSQL 表 | 正常 MVCC/共享缓冲 | 注册元数据、跨 backend 可见 |
| 投影段（mutable_overlay） | 文件/表 | manifest 重载 | 跨 backend 可见的增量 |

## 3. 核心数据结构

### 3.1 NodeStore（SoA）

| 数组 | 类型 | 作用 |
|------|------|------|
| `is_active` | packed bits | 墓碑标志；删除不立即搬移 |
| `table_oids` | `u32` | 每个节点的源表 OID |
| `primary_keys` | `Vec<String>`（owned）或 `pk_offsets + pk_bytes`（mmap） | 源主键 |

节点索引 `node_idx` 从 0 递增，是图算法唯一使用的 ID。

### 3.2 EdgeStore（CSR）

| 数组 | 类型 | 不变量 |
|------|------|--------|
| `edge_offsets` | `u32`，长度 `node_count+1` | 单调非递减；节点 i 的邻居是 `targets[offsets[i]..offsets[i+1]]` |
| `targets` | `u32` | 目标 `node_idx` 连续排列 |
| `type_ids` | `u8` | 平行于 targets，边标签 ID |
| `schema_reversed` | `u8` | 0=注册正向，1=双向反向拷贝 |
| `weights` | `u32`（可选） | 平行于 targets，加权路径用 |

反向 CSR 是从正向 CSR 派生的独立结构，入向遍历保持 O(degree)，避免扫全图。

### 3.3 ResolutionIndex

| 模式 | 使用时机 | 查找 |
|------|----------|------|
| `Builder` | 节点摄入期 | 紧凑 append |
| `Finalized` | `finalize_resolution()` 后 | 排序字节数组二分 |
| `MmapBacked` | 加载工件后 | mmap 段二分 |
| `resolution_delta` | 建图后插入 | 先查 delta，再查主索引 |

条目约 16B：`table_oid:u32 + pk_hash:u64 + node_idx:u32`；哈希用 xxh3；
delta 命中后还要用 `node_store.is_active` 和主键比较核验，防哈希碰撞/墓碑。

### 3.4 FilterIndex

按 `node_idx` 索引的平坦数组，类型编码：

| 逻辑类型 | 编码 |
|----------|------|
| numeric | `i64` |
| boolean | `bool` + 稀疏 bitmap |
| text | intern 字典 token（相同字符串只存一次） |
| date | 距 2000-01-01 的天数 |
| timestamptz | 距 epoch 的微秒 |
| uuid | `u128` |

遍历谓词 `graph.gt/lt/eq/between/all` 编译成 `FilterOp`，在 `candidate_allowed` 里
直接比较编码值，不回源表。

### 3.5 其他

- `edge_type_registry: Vec<String>`：标签 → `u8`，0 保留，最多 254 个用户标签。
- `tenant_membership: HashMap<String, RoaringBitmap>`：租户 → 允许节点集合。
- `edge_buffer: Vec<EdgeMutation>`：建图后的边增删覆盖层。
- `projection_mode`：`csr_readonly` 或 `mutable_overlay`。
- `_mmap: Option<Mmap>`：拥有 mmap 生命周期，所有 mmap-backed store 依赖它。

## 4. 注册模型

| catalog 表 | 内容 |
|------------|------|
| `graph._registered_tables` | `table_name` 主键（以表名注册、构建期才解析 OID，`table_oid` 只出现在 `graph._sync_log`）、`id_column` 主键列、可搜索列、租户列 |
| `graph._registered_edges` | 源表+源列、目标表+目标列、标签、是否双向、权重列、动态标签列 |
| `graph._registered_filter_columns` | 过滤列名 + 类型 |

注册只写元数据，不碰源表数据。`graph.add_table()` 要求 `id_column` 是主键或
唯一 NOT NULL 索引（`catalog/validate.rs`）。

两类边：

- **FK 式**：`from_table` 是已注册节点表，`from_column` 是节点表上的外键列。
- **边表式**：`from_table` 是纯边表，`from_column`/`to_column` 都在边表上。

## 5. 多图

同一数据库支持多个隔离命名图：catalog、Engine 槽、构建/维护 job、持久化路径、
同步日志、投影代次、发现、reset 全部按 `graph_id` 键控。默认图名 `default`、
命名空间 `public`。查询前用 `graph.set_current_graph()` 选择。

## 6. 版本核对清单

升级或换源码树时先核对：

```bash
# 扩展与源码版本
grep -E '^version|^pgrx|^roaring|^bincode' "${PGGraph_SRC}/graph/Cargo.toml"
grep -E 'rust-version|channel' "${PGGraph_SRC}/graph/rust-toolchain.toml"

# PostgreSQL 大版本与 pgrx feature 必须一致
"${PGSQL_PREFIX}/bin/pg_config" --version
grep -o 'pg1[4-8]' "${PGGraph_SRC}/graph/Cargo.toml" | sort -u

# 工件格式版本（加载时会校验）
# .pggraph header 的 version 字段 = 3
```

差异优先看 SQL facade、生成的 `graph--*.sql`、`graph/src/pg_tests/`；
不要只按旧文档的字段名推断。

## 7. 源码阅读顺序建议

1. `sql_facade/admin.rs` → `catalog.rs`：注册与校验。
2. `builder.rs`：源表 → NodeStore → spool → CSR。
3. `engine.rs` + `bfs.rs` + `path_finder.rs` + `connected_components.rs`：查询热路径。
4. `persistence.rs` + `node_store.rs` + `edge_store.rs`：工件、mmap、反向 CSR。
5. `sync.rs` + `projection/`：源表变更如何进入覆盖层/投影段。
6. `sql_search.rs` + `gql/` + `query/`：搜索与文本查询。
7. `sql_facade/traversal.rs` + `sql_hydration.rs`：结果排序、分页、hydrate、JSON。
