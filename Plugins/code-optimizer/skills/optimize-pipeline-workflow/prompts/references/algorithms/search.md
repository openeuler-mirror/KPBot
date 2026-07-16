# 搜索与最近邻 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 精确最近邻

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 暴力 O(Nd) | KD-tree (k-d tree) | O(Nd) → O(log N) 期望（低维） | FLANN/libkdtree 可移植，维数 > 30 退化为暴力 |
| KD-tree（维数高） | Ball tree | O(N) 期望（中维） | sklearn ball tree 可参考 |
| 暴力（小 N，维数小） | SIMD 距离计算 NEON FMLA | 常数因子 ↓ 4-8× | NEON 4-lane 同时计算欧氏距离 |
| 暴力（极小 N ≤ 100） | SIMD 排序网络选出 K 最近 | 常数因子 ↓ | NEON 比较 + 选择网络 |

**检测信号**：
- 嵌套循环 + 欧氏距离：`sqrt(dx*dx + dy*dy)` 或 `sum((a[i]-b[i])^2)`
- 朴素 KNN：维护 K 个最小距离的数组/堆，循环内 `if(dist < max_dist) insert_sorted(...)`

## 2. 字符串搜索

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 朴素 O(NM) | KMP / Boyer-Moore / Sunday | O(N+M) 或 O(N/M) 期望 | 标准实现，无特殊指令 |
| Sunday 算法 | SIMD 宽窗口搜索 (NEON CMEQ 16 字节匹配) | 常数因子 ↓ 4-8× | NEON CMEQ + SHRN 生成 movemask |
| 多模式匹配 | Aho-Corasick（AC 自动机）| O(N + Σ|P_i| + matches) | 适合关键词过滤 |

**检测信号**：
- 双重循环外层遍历文本内层遍历模式串
- KMP: `next[i]` / `failure` 数组 + `while(j && pat[j] != txt[i]) j = next[j]`
- AC 自动机: trie 节点 + `fail` 指针
