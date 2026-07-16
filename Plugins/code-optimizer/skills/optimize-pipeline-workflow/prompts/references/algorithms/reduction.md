# 归约与扫描 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 求和/求积归约

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `sum += a[i]` 简单累加 | 成对求和 (pairwise summation) | 精度 ↑ O(√N ε) → O(ε log N) | 递归/展开，无特殊指令 |
| 成对求和 | Kahan 补偿求和 | 精度 O(ε) → O(ε) 但误差不累积 | 每条加法 +1 补偿项，开销 ×2 |
| 求和（仅需结果，不关注中间） | NEON 分 lane 归约 (4 路并行求和后合并) | O(N)，常数因子 ↓ 4× | NEON ADDV/FADDV/UADDLV |

**检测信号**：
- `sum += a[i]` 简单累加循环（最常见模式）
- Kahan: `y = x - c; t = sum + y; c = (t - sum) - y; sum = t;` 4 步补偿

## 2. 极值归约

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `if(a[i] > max) max = a[i]` | NEON SMAX/UMAX/FMAX 4-lane 并行 | O(N)，常数因子 ↓ 4× | NEON SMAXV/UMAXV/FMAXV 归约 |
| 同时求 min+max | NEON SMIN+SMAX 成对，每轮更新两个极值 | 常数因子 ↓ 8× | NEON SMINV + SMAXV |

**检测信号**：
- `if(a[i] > max) max = a[i]; if(a[i] < min) min = a[i];` 成对极值
