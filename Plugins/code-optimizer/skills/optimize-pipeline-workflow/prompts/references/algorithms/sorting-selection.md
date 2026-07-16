# 排序与选择 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 通用排序

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 快速排序 (quicksort) | pdqsort (pattern-defeating quicksort) | O(N log N)，常数因子 ↓，避免最坏 O(N²) | 纯 C++ 模板，平台无关 |
| 快速排序（整数 key）| 基数排序 (radix sort, LSD/MSD) | O(N log N) → O(Nk) ≈ O(N)（固定宽度 key） | NEON 可加速计数/分区阶段 |
| 快速排序（浮点 key）| 基数排序（IEEE 754 整数视角排序浮点） | O(N log N) → O(N) | 需要 IEEE 754 位操作技巧 |

## 2. 小规模排序网络（N ≤ 32）

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `if/else` 比较交换链 | 排序网络 (Batcher/odd-even/bitonic) | O(N²) → O(N log² N) 网络深度 | NEON 可同时比较 4 个 lane，适合排序网络的每级 SIMD 化 |
| 排序网络 | SIMD 排序网络（NEON 4-lane 同时排序） | 常数因子 ↓ 2-4× | NEON CMPGE + BSL（位选择替代分支） |

**检测信号**：
- 小规模（N≤32）的 `if(a[i] > a[j]) swap` 模式
- 固定 N 的冒泡/插入排序

## 3. Top-K / 中位数 / 分位数

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 全排序后取 Top-K | `std::nth_element` / QuickSelect | O(N log N) → O(N) 期望 | 标准库已实现 |
| QuickSelect | 基数选择 (radix select) | O(N) 确定性 | 适合整数 key |
| 中位数滤波 | 滑动窗口快速中位数（直方图增量更新） | O(NK) → O(N) (K 为窗口) | 每个窗口只增删一个元素，增量更新直方图 |
