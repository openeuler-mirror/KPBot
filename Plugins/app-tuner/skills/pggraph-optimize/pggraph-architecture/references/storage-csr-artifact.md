# CSR、持久化工件与内存归属

## 1. 为什么是 CSR

CSR（Compressed Sparse Row）把邻居存成连续数组：

```
edge_offsets = [0, 2, 3, 3]   // len = node_count + 1，单调非递减
targets      = [1, 2, 2]      // 所有目标 node_idx 连续排列
type_ids     = [1, 1, 1]      // 平行于 targets
schema_reversed = [0,0,0]
```

节点 i 的邻居 = `targets[edge_offsets[i]..edge_offsets[i+1]]`，O(1) 切片，cache 友好。
这是 pgGraph 相对递归 SQL/JOIN 的核心优势：邻居访问不重新发现关系，热循环不回 SQL。

CSR 的不变量（加载时会校验）：

- `edge_offsets[0] == 0`
- `edge_offsets` 单调非递减
- `edge_offsets[node_count] == targets.len()`
- `targets` 中每个值 < `node_count`
- `type_ids` / `schema_reversed` / `weights` 长度与 `targets` 一致
- 主键偏移单调、UTF-8 边界合法
- 各 section 偏移在文件范围内，bincode 载荷长度合法

## 2. 前向 CSR 与反向 CSR

| 结构 | 来源 | 存储 | 用途 |
|------|------|------|------|
| 前向 CSR | 构建时按 `(source,target,type,schema_reversed)` 排序生成 | `.pggraph` 段，加载后 mmap | `direction=out` / `any`（双向注册） |
| 反向 CSR | 加载/构建后从正向 CSR 派生 | 后端堆，`EdgeStore::reversed()` | `direction=in`，入向遍历 O(degree) |

关键事实：**反向 CSR 不持久化到工件，每个 backend 各自重建**。在 LDBC 规模下这是
每 backend 数百 MB 私有内存和数秒加载时间的来源。优化方向（把反向 CSR 写入工件
section 并 mmap 共享）属于候选，不是现状。

## 3. `.pggraph` 工件

路径：`$PGDATA/<graph.data_dir>/<graph_id>/main.pggraph`
（`graph.data_dir` 默认 `graph`，多图按 `graph_id` 分目录）。

### 3.1 Header（128B）

| 字段 | 大小 | 说明 |
|------|------|------|
| magic | 4B | `PGGH` |
| version | u32 | 当前 3 |
| flags | u32 | 保留/特性位 |
| node_count | u32 | 节点数 |
| edge_count | u32 | 边数（含双向展开） |
| section_offsets | 12 × u64 | 12 个 section 的偏移 |
| crc32 | u32 | header 之后字节的 CRC32 |

### 3.2 Section

| # | 内容 | 大小/编码 |
|---|------|-----------|
| 0 | active bits | `ceil(node_count/8)` |
| 1 | table_oids | `node_count × 4` |
| 2 | edge_offsets | `(node_count+1) × 4` |
| 3 | targets | `edge_count × 4` |
| 4 | type_ids | `edge_count × 1` |
| 5 | weights | 空或 `edge_count × 4` |
| 6 | schema_reversed | `edge_count × 1` |
| 7 | resolution index | `4 + entry_count × 16` |
| 8 | primary key offsets | `(node_count+1) × 8` |
| 9 | primary key bytes | UTF-8 |
| 10 | filter index | `u32 长度 + bincode2 载荷` |
| 11 | edge type registry | `u32 长度 + bincode2 载荷` |

### 3.3 写路径

1. 创建父目录，预留 128B header。
2. 流式写各 section 到 `<path>.tmp`（不全量驻留内存）。
3. 增量计算 CRC32，回填 header 与 section 偏移。
4. `sync_all()` → 原子 rename 为 `main.pggraph`。
5. 同步检查点 `main.pggraph.sync`（同样 tmp+rename）。
6. 立即 `load_graph_file()` 重载校验；失败即报 build/vacuum 错误（fail-closed）。

### 3.4 读路径

1. 打开 → mmap 只读。
2. 校验 magic/version/CRC/section 布局/CSR 单调性/主键偏移/bincode 边界。
3. 前向 CSR、NodeStore、Resolution 主段保持 mmap（不拷贝）。
4. FilterIndex、edge_type_registry 反序列化（bincode）到后端堆；**反向 CSR 不在工件内**，由前向 CSR 经 `EdgeStore::reversed()` 以 O(V+E) 重建到后端堆（这是每 backend 数百 MB 私有内存与数秒加载的主要来源，见 §2）。
5. `Engine._mmap` 拥有 mmap 生命周期，所有 mmap-backed store 的原始指针依赖它。

### 3.5 工件复用规则

| 操作 | 工件是否有效 | 说明 |
|------|--------------|------|
| 重启 PostgreSQL | 有效 | 新 backend mmap 同一文件 |
| 重新编译 `.so`（不改格式） | 有效 | 重启加载新 `.so` 即可 |
| 修改 CSR/工件格式 | 失效 | 需 `clean-graph` + 重新 build |
| 重新导入/修改源表数据 | 可能失效 | 源表变了需 build/vacuum 或 sync |
| 修改注册表/边/过滤列 | 失效 | 需重新 build |

## 4. 内存归属

| 数据 | 归属 | 共享 | 备注 |
|------|------|------|------|
| `.pggraph` 前向 CSR / NodeStore / Resolution 主段 | OS page cache + 进程 mmap | 跨 backend 物理页共享 | RSS 会重复计，但 PSS/Private 可区分 |
| 反向 CSR | backend 私有堆 | 否 | 加载时 O(V+E) 重建 |
| FilterIndex / registry | backend 私有堆 | 否 | bincode 反序列化 |
| BFS/DFS 状态 | 查询级 | 否 | visited/depth/parent/frontier |
| 结果 String/path/JSON | 查询级 | 否 | hydrate/格式化时分配 |
| catalog 表页 | PG shared buffers | 是 | 正常 MVCC |
| 投影段/同步日志 | 表/文件 | 视模式 | mutable_overlay 跨 backend |

诊断时用：

```bash
# 单 backend 的 PSS/Private
awk '/^Pss:|^Private_(Clean|Dirty):/ {print}' /proc/<pid>/smaps_rollup

# 区分共享 mmap 与私有堆
awk '/^[0-9a-f].*pggraph/ {print}' /proc/<pid>/smaps
```

不要把 RSS 直接相加当作“图内存”；mmap 的 `.pggraph` 是共享的，`PSS` 才能近似
按进程分摊。每 backend 私有内存约等于反向 CSR + FilterIndex + 查询状态 + 结果。

## 5. 加载开销

每个 backend 首次访问图时：

1. 打开/映射 `.pggraph`（大文件 mmap 本身很快）。
2. CRC32 扫描全文件（与文件大小线性相关）。
3. 校验 CSR/主键/bincode 边界。
4. 从正向 CSR 重建反向 CSR（O(V+E)）。
5. 反序列化 FilterIndex/registry。

因此在并发压测中，warmup 阶段会看到多个 backend 同时加载、CPU/IO 抖动；
计时阶段应使用持久连接并预热。减少加载开销的候选包括：预热连接池、把反向 CSR
持久化并 mmap、提供受控的 checksum 跳过选项；后者有数据完整性风险，必须显式开关。

## 6. 与 `graph.status()` 的关系

`graph.status()` 报告的是**后端内存中的构建元数据**，不是工件状态。刚启动或刚
mmap 加载后可能显示 `node_count=0/edge_count=0`，但 `graph.traverse()` 已经可用。
排查顺序：先执行一次 1-hop traverse，再信 status。需要工件层面的信息时看文件大小、
`graph.data_dir`、以及 `report.json` 里的 `build_output`。
