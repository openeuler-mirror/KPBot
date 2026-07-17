# 有限域 / 多项式运算 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. GF(2^8) 乘法

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 循环移位 + XOR 逐 bit（对数表法）| 完整乘法表 (256×256 lookup) | O(N) → O(N)，常数因子 ↓ 8-32× | 查表法，无特殊指令 |
| 完整乘法表 | PMULL 指令（GF(2^128) 域乘法） | O(N)，常数因子 ↓ 2-4× | ARMv8.0+ PMULL/PMULL2 |
| GF(2^8) 通用乘法 | 预计算 log/exp 表 + 加法替代乘法 | O(N)，常数因子 ↓ | NEON TBL 4 路并行查表 |

**检测信号**：
- GF 乘法: `while(b) { if(b&1) r ^= a; a <<= 1; b >>= 1; }` 循环移位 XOR
- 或 `gflog[gfantilog[a] + gfantilog[b]]` 对数表法

## 2. Reed-Solomon 纠删码

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| Vandermonde 矩阵（求逆开销大）| Cauchy 矩阵（求逆更简单） | 编解码复杂度 ↓ | ISA-L/Intel ISA-L 已有实现，可移植到 ARM |
| 逐列 GF 乘法 | 批量 GF 乘法（NEON PMULL 或 TBL+EOR） | O(NK)，常数因子 ↓ 4-16× | NEON TBL/EOR GF 乘法实现 |
| 软件 GF | PMULL 硬件加速 | O(NK)，常数因子 ↓ | ARMv8.0+ PMULL |

**检测信号**：
- Vandermonde 矩阵生成：`matrix[i][j] = gf_pow(gf_prim, i*j)`
- 或编码循环内 GF 乘法密集 + XOR 归约
