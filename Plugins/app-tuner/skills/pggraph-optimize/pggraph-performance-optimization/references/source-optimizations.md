# 源码优化：已实测与候选

> 所有补丁都应在 `${PGGraph_SRC}` 工作树进行，`${PGGraph_SRC_BAK}` 只读。
> 先备份、单变量、正确性门禁、同口径 A/B；收益数字来自源资料特定环境，目标环境需复测。

## 1. UnionFind 迭代化 + path halving

**文件**：`graph/src/connected_components.rs`

**问题**：`UnionFind::find` 递归路径压缩，在 component_stats 中约占 30% CPU（~810K 次），
每次递归有函数调用/栈帧开销。

**改法**：

```rust
// before: 递归 + 完整路径压缩
fn find(&mut self, x: u32) -> u32 {
    if self.parent[x as usize] != x {
        self.parent[x as usize] = self.find(self.parent[x as usize]);
    }
    self.parent[x as usize]
}

// after: 迭代 + path halving
fn find(&mut self, mut x: u32) -> u32 {
    while self.parent[x as usize] != x {
        let parent = self.parent[x as usize];
        let grandparent = self.parent[parent as usize];
        self.parent[x as usize] = grandparent;
        x = grandparent;
    }
    x
}
```

**实测**：component_stats 811.46→659.93ms（-18.6%）；TOTAL -7.1%。
**适用**：所有 connected_components 调用。
**风险**：算法等价；注意 path halving 与完整压缩的树形差异，不影响连通性结果。

## 2. `node_deleted` 只读快速路径

**文件**：`graph/src/projection/tx_delta.rs`、`graph/src/connected_components.rs`

**问题**：`node_deleted` 在 component_stats 中被调用约 460K 次（每节点 + 每边目标），
每次都 `thread_local + RefCell::borrow()`；只读查询没有事务删除，仍付开销。

**改法**：

- 增加 thread_local `HAS_DELETED_NODES: Cell<bool>`。
- `node_deleted` 先检查标志，false 直接返回，跳过 RefCell。
- 批量算法（如 components）先检查一次 `has_deleted_nodes()`，避免每节点重复访问。

**实测**：component_stats 811.46→652.29ms（-19.6%）；TOTAL -11.3%。
**适用**：所有图查询（BFS/DFS/CC）。
**风险**：只读路径行为不变；必须保证写路径正确设置标志（事务删除/回滚）。

## 3. component_sizes：HashMap → Vec

**文件**：`graph/src/connected_components.rs`、`sql_facade/components.rs`、`sql_facade/runtime.rs`

**问题**：finalize 循环用 `HashMap<u32,u32>` 统计分量大小，每节点 `entry().or_insert()`。

**改法**：根节点是 `node_idx`（0..node_count），直接用 `Vec<u32>` 下标计数。

**实测**：component_stats 811.46→756.35ms（-6.8%）；TOTAL -1.0%。
**适用**：connected_components + SQL 层。
**风险**：内存 O(n)（4B×node_count：100K/500K 小图系列约 400KB；LDBC SF1 的 3.18M
节点约 12.7MB——本文件 §1/§2 的调用次数（~810K/~460K）与 811.46→756.35ms 实测
均来自 100K/500K 小图系列，勿按 SF1 估算内存余量）；注意 `u32::MAX` 墓碑/未分配节点的处理。

## 4. traverse 排序键：String → node_idx

**文件**：`graph/src/bfs.rs`、`graph/src/sql_traversal.rs`

**问题**：traverse 后处理按 `(depth, node_table, node_id String)` 排序；2-hop 返回
1000+ 行时 String memcmp 占约 15% CPU。

**改法**：

- `TraversalResult` 增加内部 `node_idx: u32`。
- `to_traversal_results` 填充 `node_idx`。
- SQL 层排序用 `node_idx` 替代 `node_id` String；输出仍用原主键字符串。
- `sort_traverse_candidates_for_many` 同步修改。

**实测**（A+B+C 基线上）：forum_neighborhood -29.4%、person_content -25.2%、
friend_traversal -14.0%、status -18.2%；TOTAL 1405→972ms（A+B+C+H，-30.8%），
吞吐 5.93→8.45 q/s（+42.5%）。代号 A/B/C/H 的定义见 SKILL.md 速查表脚注；本行属默认
轮次系列（与 compiler-and-rust 的 40 次迭代系列口径不同，见该表说明）。
**适用**：所有 traverse，尤其是 2-hop/大结果。
**风险**：排序顺序必须与旧逻辑等价（同一节点集合、同一 depth/table 下）。注意
`node_idx`（分配序）与 `node_id`（字符串字典序）**不保证同序**——同一 depth/table 下若
主键 b 比 a 先注册（idx 0 < 1），按 idx 排序的首行会从 a 变成 b，分页边界随之漂移：
消费方依赖结果顺序/分页时须保留原排序键（或先证明该表 idx 与字符串序一致并验证
有序结果与分页边界不变）。同时保证 `node_idx` 与 `node_id` 一一对应，
墓碑/覆盖层节点处理一致。

## 5. hydrate / String / SPI / JSON

问题：`hydrate=true` 时每行/每节点：

- `primary_key().to_string()`、路径 String、edge_type clone。
- 按表分组、`sort/dedup`、SPI 查询、palloc/计划构建、`to_jsonb`、JSON 序列化。
- 每行 `regclass_text` 系统目录查找、String clone、结果向量释放。

候选（按风险从低到高）：

1. **关闭不必要的 hydrate**：计数/内部组合/大遍历用 `hydrate=false`；这是零代码优化。
2. **减少返回列/路径**：只要坐标就不要 hydrate；只要 ID 就不要完整节点。
3. **批量/缓存 hydrate**：按表批量 `= ANY($1)` 已是现状；可加计划缓存/结果缓存（注意
   一致性与内存）。
4. **缓存 `regclass_text`**：按 table_oid 缓存表名，减少系统目录查找。
5. **String/arena**：路径/节点 ID 用 arena/共享切片，减少分配/克隆；改动面大，
   需要生命周期设计。
6. **排序后分页再格式化**：先 `skip/take` 再 hydrate/JSON，避免为未返回行付成本
   （注意排序需要足够信息）。

**状态**：1/2 是已可用策略（无需改码）；3-6 是候选，源资料未给出完整 A/B 数字。
**风险**：hydrate 结果受 MVCC/RLS/ACL 影响；缓存可能读到陈旧行；arena 改动易引入
生命周期/别名 bug。

## 6. 反向 CSR 持久化 / mmap 共享（候选，未实现）

**问题**：每 backend 加载时从正向 CSR 重建反向 CSR（`EdgeStore::reversed`），
是私有内存和加载 CPU 的主要来源之一。源资料测量某环境每 backend 私有约 265.7MB，
其中反向 CSR 占大头；42 个活跃 backend ≈ 11.2GB。

**候选**：

- 在 `.pggraph` 增加反向 CSR section（edge_offsets/targets/type_ids/schema_reversed——对齐上游 EdgeBacking::Owned 字段名），加载时直接
  mmap，不再每 backend 重建。
- FilterIndex 也可考虑直接 mmap（当前 bincode→heap）。
- 提供受控的 `trust_checksum`/跳过 CRC 选项，减少加载扫描；必须显式开关并记录风险。

**预期**（源资料，未验证）：私有内存 265.7MB→<5MB/backend；LLC miss、stall、IPC 改善。
**风险**：

- 工件格式变更 → 旧工件不兼容，需要版本/迁移/重建。
- mmap 布局/对齐/生命周期复杂；错误会导致越界/崩溃。
- 跳过 CRC 会放过损坏工件；只能用于可信/可重建场景。
- 跨 backend 共享的只读页需要确保不被原地修改。

**验证**：先做格式/加载单测，再做小图功能对账，最后同口径并发 A/B；不要直接上生产。

## 7. 其他候选

| 候选 | 思路 | 风险/状态 |
|------|------|-----------|
| 结果行按需列 | 只返回请求的列，减少 JSON/String | 改 SQL 接口/兼容性 |
| `max_rows`/`LIMIT` 下推 | 减少 BFS/后处理 | 改变语义/口径，需注明 |
| 连接池/预热 | 复用 backend，移出加载成本 | 配置层，见 config |
| `target-cpu=native` | SVE 自动向量化 | -2.5%，可移植性风险 |
| 预取/密集位图/NEON | 源资料负结果 | 不要重复 |

## 8. 源码改动流程

```
① 从备份复制工作树（如需要）
② 记录基线 .so/commit/口径
③ 单个改动 + 单元/冒烟
④ 正确性：SQL JOIN/不变量/checksum
⑤ 同口径 A/B（≥3 轮）
⑥ 保留/回滚；更新文档与负结果
```

不要在一个补丁里混合“算法 + 格式化 + 编译选项”；否则无法归因。
