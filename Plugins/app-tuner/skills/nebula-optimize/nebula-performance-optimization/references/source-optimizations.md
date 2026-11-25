# 源码优化点与实测影响

> 所有改动都必须在目标版本上重新编译、跑功能冒烟/路径比对，再做同口径 A/B。
> 以下“收益”来自 3.8.0 + 特定数据/部署的实测，不是通用承诺。

## 1. 方法

```
基线二进制 → 五场景 perf 采样 → 选热点 → 单点实现 → 编译
  → 正确性冒烟（GO/MATCH/LOOKUP/INSERT 回读/SUBGRAPH/FSP 路径比对）
  → 五场景压测 ≥3 轮（与 SKILL.md 核心纪律 1 的“≥3 轮”同口径，保留每场景逐轮结果）
  → 保留/回滚 → 记录
```

原则：**结构性去分配/去哈希 > 减少拷贝 > 指令级微优化**。先做容器/对象/缓存，再做
NEON/预取；不要一开始就改字符串比较。

## 2. Value SSO（opt1）

### 问题

`Value` 的字符串成员是 `std::unique_ptr<std::string>`：

- 短字符串（VID≤15B、边名）也要一次堆分配 + 间接访问；
- BFS/多跳中大量 Value 构造/析构，分配与 cache miss 放大。

### 改动

把 `unique_ptr<string>` 改为内联 `std::string`，利用 SSO：

```cpp
// 改前
std::unique_ptr<std::string> sVal;

// 改后
std::string sVal;
static_assert(sizeof(Value) == 40UL, "...");
```

移动构造要正确处理字符串（不能 memcpy 整个 union）。注意 3.8.0 中 `__EMPTY__` 定义在
`enum class Type`（Value.h）内，须写 `Type::__EMPTY__`——直接写 `__EMPTY__` 会遇到未声明的
标识符，示例按目标版本编译验证：

```cpp
Value::Value(Value&& rhs) noexcept : type_(Type::__EMPTY__) {
  if (rhs.type_ == Type::STRING) {
    new (&value_.sVal) std::string(std::move(rhs.value_.sVal));
  } else {
    memcpy(&value_, &rhs.value_, sizeof(value_));
  }
  type_ = rhs.type_;
  rhs.type_ = Type::__EMPTY__;
}
```

### 实测

- FindShortestPath **+12.8%**（部分批次 +11.4%）；
- 根因：BFS 中大量 Value string 构造/析构，SSO 让短串零堆分配。

### 风险

- `Value` 尺寸从 16B 变 40B，所有按 16B 假设的代码/序列化/内存布局要检查。
- 移动/拷贝/析构必须成对正确；建议跑全量单测 + 功能冒烟。
- 对 INT64 VID 收益较小；主要收益在字符串 VID/属性。

## 3. BFS 容器与拷贝消除（batch2/3）

### 3.1 flat_map 替代 unordered_multimap

```cpp
// 改前
std::unordered_multimap<Value, Edge> currentEdges;

// 改后
robin_hood::unordered_flat_map<Value, std::vector<Edge>> currentEdges;
```

收益：

- 开放寻址、同 key 的边连续存放，减少链式哈希的指针追逐与分配；
- 同 key 多条边用 vector，避免 `_M_insert_multi_node` 的节点分配；
- PathMap 同构改造。

实测：BFS 容器改造单独约 +6%，与后续改动叠加后 FSP **+15%**。

### 3.2 Edge 移动/拷贝消除

```cpp
// 改前：std::move(const Edge&) 实际退化为拷贝
// 改后：使用 mutableEdge()/真正的移动，dst 移出作为 key
```

要点：

- `std::move` 对 `const&` 不会移动，必须拿到非 const 对象。
- 移出 dst 前确认后续不再读 `edge.dst`；`createPath` 已核实不读。
- 每条边省一次 Edge 拷贝（Edge 含 atomic refcnt/props map，约 140B）。

### 3.3 删除 uniqueDst 容器

用 `currentEdges[dst]` 的空 vector 判断本步新 dst，逐步末把 map 键并入 visitedVids：

- 每边省 1 次 hash + 1 次 Value 拷贝 + 容器析构；
- 语义等价，需逐路径比对确认。

### 3.4 reserve

对 `createPath` result、`nextStepVids.rows` 等做 reserve，减少 rehash/扩容。

### 3.5 实测

| 改动 | FSP 影响 |
|------|---------|
| flat_map | +6% |
| + Edge 移动 | 叠加 |
| + 删除 uniqueDst | 叠加到 +15% |
| + reserve | 小幅/稳定 |

注意：短 SSO 字符串的“移动”≈拷贝，单独做移动收益可能很小；要配合去重容器删除。

## 4. 哈希：SpookyHash/分块

### 问题

`std::hash<Value/Edge/Vertex>` 逐字节 FNV-1a，且 Edge/Vertex 可能先 `toString()` 再哈希，
产生临时字符串分配。

### 改动

- 用 `folly::hasher`/SpookyHash 分块处理；
- 直接对字段哈希，避免 `toString` 堆分配；
- 保持相等对象的哈希一致（正确性关键）。

### 收益

- 读场景（尤其 FSP/多跳）有收益；
- 与 flat_map/SSO 叠加时效果更好。

### 风险

- 哈希函数改变可能影响迭代顺序/分片；不要依赖哈希顺序做语义。
- 必须验证碰撞/相等性；用单测 + 路径比对。

## 5. storaged 迭代器复用与 schema 缓存

### 5.1 迭代器复用

`SingleEdgeNode`/`HashJoinNode`/`GetDstBySrcNode` 等每顶点 `new` 迭代器；改为成员迭代器 +
`reset()` 复用，减少分配/析构。

### 5.2 GetNeighborsIter 列名索引缓存

每响应都 `split + 多 map 构建` 列名索引；改为按线程 LRU-1 缓存，同模板查询 100% 命中
（仅指针引用计数）。

### 5.3 RowReaderWrapper schema 版本缓存

每行 reset 时内联解析头部 + 缓存 `schemaVer→schema`，命中免 `shared_ptr` 解引用/版本比对。

缓存键必须限定在**同一份 schema 集合**内（按 Space/schema 集合实例号区分）：两个 Tag 都可能
出现版本 0，仅用 schemaVer 做键会按前一个 Tag 的字段布局解码；切换 schema 集合时缓存整体
失效。并保留行头与版本范围校验：版本越界仍走慢路径报错，不静默用错布局。

实测：Go2/Go3 **+0.5–0.6%**（每行快路径）。

### 5.4 schema 哈希表拷贝

`getAllVerTagSchema/getAllVerEdgeSchema` 返回 schema 表的**值拷贝**，每查询深拷贝+析构，
Go1 storaged 约占 8%。理想改法是返回 `shared_ptr<const TagSchemas>`，但这是虚函数接口，
影响 10+ 文件，风险大，实测未实施。

可选的低风险替代：

- 缓存列名/索引（见 5.2），减少每次构建；
- 避免在热路径重复获取 schema；
- 不做“返回引用”的侵入式改造，除非有完整回归能力。

## 6. 其他小优化

| 优化 | 改动 | 实测/适用 |
|------|------|----------|
| NEON null 扫描 | `findFirstNull` 16B 一次比较 | 取决于字符串占比，常为噪声 |
| NEON VID 比较 | 16B 比较替代 memcmp | 同上；注意 `vminvq` 判等 |
| aarch64 TSC | `cntvct_el0` 替代 `clock_gettime` | Go1 +0.4%；高频计时路径 |
| UNLIKELY | 热点 switch/if 加分支提示 | 小幅/噪声，需实测 |
| prefetch | `SingleEdgeIterator::next` 预取 val | 边际；规律访存才有效 |
| planKilled 降频 | 每 64 条检查一次 | Go2 +0.7% 量级 |
| `to_string` 改 `snprintf` | 自环边判断 | 小幅 |
| 字符串池（opt10） | 复用短字符串 | 视分配模式 |

这些不要一次性全上；按热点证据选择，单独验证。

## 7. 补丁清单（实测版本）

| 补丁/批次 | 内容 | 影响 |
|-----------|------|------|
| opt01 | Value SSO | FSP +12.8% |
| opt02 | BFS flat_map + NEON VID 比较 | FSP 主要收益 |
| opt03 | SpookyHash | 读场景 |
| opt04/07 | 自环边判断、planKilled 降频 | Go2 +0.7% |
| opt08 | prefetch | 边际 |
| opt10 | 字符串池 | 视场景 |
| batch2 | 字符串池、NEON/TSC、GetNeighborsIter 缓存、BFS reserve、storaged 迭代器复用、HashJoin 缓冲复用 | 综合小幅 |
| batch3 | BFS flat_map、Edge 移动、删除 uniqueDst、NameIndex 缓存、RowReader schema 缓存、reserve | FSP +15% |

应用方式：

```bash
cd "${NEBULA_HOME}"
git apply /path/to/all_5_optimizations.patch
# 或逐个 patch -p1 < ...
git diff --stat
# 编译 + 功能冒烟 + A/B
```

回滚：

```bash
git apply -R /path/to/all_5_optimizations.patch
# 或 git checkout -- <files>（确认没有其他未提交改动）
```

## 8. 正确性检查清单（源码优化必做）

1. GO 1/2/3 返回行数与基线一致。
2. MATCH 单跳/变长路径结果一致（固定排序后 diff）。
3. FETCH/LOOKUP（等值/范围）结果一致。
4. FSP 已知点对逐条路径比对（顶点序列/边类型/方向/rank）。
5. FSP 不可达点对返回空集。
6. INSERT → FETCH 回读 → UPDATE → DELETE → 再 FETCH。
7. GET SUBGRAPH 顶点/边类型与数量一致。
8. 计数对账（隔离 Space 的写测试后清理）。
9. 任何 SIMD/哈希/容器改动，增加针对性单测（边界、空值、重复 key、自环）。

## 9. 适用性与优先级

| 场景 | 优先做的源码优化 |
|------|-----------------|
| Go1 点查 | 减少分配/对象池、schema 缓存；队列问题优先部署 |
| Go2 多跳 | 迭代器复用、RowReader 缓存、减少拷贝 |
| Go3 重读 | 结果组装/内存、RocksDB 扫描；减少 Value 分配 |
| FSP | Value SSO、BFS flat_map、Edge 移动、去重容器 |
| Insert | 解析/编码；WAL/compaction 优先配置/磁盘 |

不要为了“看起来优化了”而改热路径；每个改动都要能对应一个热点证据。

## 10. 失败/回退案例

| 改动 | 结果 | 原因 |
|------|------|------|
| NEON `vmaxvq_u8(cmp)==0` 判等 | QPS 虚高 +126% | 误判不同 VID 相等，少做工作；正确性否决 |
| `std::move(const Edge&)` | 无收益 | 实际退化为拷贝 |
| 单做 Edge 移动 | FSP 几乎无变化 | 短 SSO 字符串移动≈拷贝，需配合去重容器删除 |
| schema 返回引用 | 未实施 | 虚函数接口影响面大 |
| 全量第三方升级 | 读路径≈0 | 收益主要来自 Nebula 补丁 |

## 11. 如何挖掘新的优化点

从 profile 出发，不靠“猜”：

| 观察到 | 可能的优化 | 先验证什么 |
|--------|-----------|-----------|
| self% 高且伴随 `malloc/sdallocx/tcache` | 对象池、SSO、减少临时对象、flat 容器 | 分配次数/对象大小/生命周期 |
| `_Hashtable`/`unordered_map` 构造/析构/插入高 | 复用容器、reserve、flat_map/开放寻址 | key 分布/冲突/同 key 多值 |
| `Value::setS/clearSlow` 高 | SSO、减少字符串拷贝、string_view/移动 | VID 类型/长度/属性数量 |
| 每行/每响应重复解析（列名、schema、头部） | 缓存（按线程/Space/schema 版本） | 命中率与失效策略 |
| `std::move(const&)`/拷贝构造高 | 拿非 const 对象、真移动、移动语义 | 调用点是否还需要源对象 |
| 每项 `new/delete` 迭代器 | 成员化 + `reset()` 复用 | 生命周期/并发安全 |
| `clock_gettime`/`gettimeofday` 高 | aarch64 `cntvct_el0` 快速路径 | 时钟精度/单调性要求 |
| `memcmp/strcmp/find` 逐字节 | NEON 16B 比较/扫描 | 长度分布/边界/`\0` 填充 |
| 分支 miss 高 | 分支less/UNLIKELY/查表 | 分支可预测性与正确性 |
| 反复 rehash/扩容 | reserve/预估容量 | 容量估计与内存 |
| RPC 次数/扇出大 | 批量、合并、谓词下推、结果裁剪 | 语义是否允许合并 |
| 锁/futex 高 | 分片、无锁读、减少共享、批量 | 正确性/内存序 |
| I/O/compaction 高 | 批量写、WAL/compaction 调优 | 数据安全/恢复 |

流程：

1. 在火焰图中找 **self 高 + children 高** 的函数/路径。
2. 看它属于哪个机制（分配/哈希/拷贝/锁/IO/解析）。
3. 读源码确认数据结构与调用频次（不是看名字猜）。
4. 设计最小改动，先做微基准/单场景，再做五场景。
5. 正确性门禁 + A/B；无效就回滚并记录。

避免：

- 只因为“这个容器理论上更快”就替换；要看实际访问模式。
- 只看 self%；children 高的路径可能更值得改。
- 没有正确性用例就改哈希/容器/移动语义。
- 一次堆很多补丁；无法归因，也无法回滚。
