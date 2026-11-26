# HNSW 算法与源码导航

## 主要结构

- `faiss/IndexHNSW*`：索引包装、storage、train/add/search 与具体存储变体。
- `faiss/impl/HNSW*`：图层级、邻接表、入口点、构建与搜索。
- storage 对应的 DistanceComputer：决定原始浮点、PQ/SQ 或其他存储的距离路径。
- visited 状态和候选容器：类型、清理/复用方式及生命周期由实际版本确定。

## 构建链

```text
IndexHNSW::add → 添加节点 / 分配层级
  → 上层定位 → 搜索候选邻居 → 选择并连接邻居 → 更新图结构
```
核对线程、锁、随机种子和插入顺序。构建优化需同时评估成本、图结构和查询质量。

## 查询链

```text
IndexHNSW::search → 构造 query 距离计算机
  → 上层贪心下降 → 底层候选扩展
  → 邻接表访问 / visited 去重 / 距离计算
  → 候选容器与结果容器 → 最终结果
```
常见定位符号有 `greedy_update_nearest`、`search_from_candidates`、`neighbor_range`、`MinimaxHeap`；名称和职责需在目标版本确认。内积实现可能在内部使用取负转换，比较方向与最终输出需一起核对。

## 可选能力

批量距离、量化存储、邻接表预取、节点重排和额外 ID 映射都应从实际源码取证。它们不是所有 `IndexHNSW` 的成员或默认行为。修改任一能力时，检查 storage、序列化、映射与搜索循环之间的契约。
