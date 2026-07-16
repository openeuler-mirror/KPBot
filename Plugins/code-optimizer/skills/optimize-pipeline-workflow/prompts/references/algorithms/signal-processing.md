# 信号处理 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 傅里叶变换（FFT/DCT/DST）

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 朴素 DFT O(N²) | Cooley-Tukey FFT | O(N²) → O(N log N) | FFTW/FFTS 库已有 NEON 优化 |
| 复数 FFT（一般 N）| 实输入 FFT（RFFT, N 为偶数时仅需 N/2+1 复数） | O(N log N)，常数因子 ↓ ~2× | 实部/虚部可用 NEON ZIP 交错处理 |
| 小 N FFT（N ≤ 32）| Winograd 最小乘法 FFT | 乘法次数最小化 | NEON FMLA 密集 |
| DCT (type II, 8×8) | AAN (Arai-Agui-Nakajima) 算法 | 乘法 ↓ 2-3× 于朴素 | NEON FMLA，JPEG 编解码常用 |

**检测信号**：
- 嵌套循环 + 复数旋转因子 `exp(-2πikn/N)` 或 `cos()`/`sin()` 查表
- 蝶形运算：`tmp = a + b; b = (a - b) * W; a = tmp`

## 2. 卷积

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 直接卷积 (small kernel) | 直接展开 (direct unroll) | O(K²N²)，常数因子 ↓ | NEON FMLA 同时计算 4 个输出像素 |
| 直接卷积 (large kernel) | FFT-based 卷积 (Overlap-Add/Save) | O(K²N²) → O(N log N) | FFTW NEON 后端 |
| 3×3/5×5 卷积 | Winograd F(2×2, 3×3) 最小滤波 | 乘法 ↓ 2.25× | NEON FMLA，推理引擎常用 |
| 深度可分离卷积 (depthwise) | im2col 展开 + GEMM | 常数因子 ↓ | NEON GEMM 微内核 |

**检测信号**：
- 嵌套循环 + 滑动窗口 + 乘累加，kernel 大小固定
- `output[y][x] += input[y+ky][x+kx] * kernel[ky][kx]`

## 3. 多项式评估

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `a0 + a1*x + a2*x² + ...`（逐项求幂） | Horner 方法: `((a_n*x + a_{n-1})*x + ...)` | O(N²) → O(N) | 乘加链天然适合 FMLA |
| Horner 方法（高次多项式，≥ 8）| Estrin 方案 (分段并行评估) | O(N)，但延迟 ↓（减少依赖链） | NEON 同时评估 4 段，打破依赖链 |

**检测信号**：
- 循环内逐项计算 `pow(x, i)` 或 `x = x * x_orig` 累积
- 或连续 `a + b * x + c * x * x + d * x * x * x` 展开
