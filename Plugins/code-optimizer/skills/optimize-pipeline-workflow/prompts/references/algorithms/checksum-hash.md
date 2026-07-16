# 校验和 / 哈希 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. CRC32/CRC64

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 逐字节查表（1 字节/轮） | Slice-by-4 / Slice-by-8（4/8 字节/轮） | O(N) → O(N)，常数因子 ↓ 4-8× | 标准 C 移植，无特殊指令依赖 |
| Slice-by-N | PMULL 无进位乘法折叠（16 字节/轮） | O(N) → O(N)，常数因子 ↓ 进一步 2-4× | ARMv8.0+ PMULL/PMULL2，aarch64 原生支持 |
| 任意实现 | 三路 CRC 折叠 + 切片合并 | O(N)，最高吞吐 | 需要 PMULL + 展开循环 |

**参考文献**：
- Kounavis & Berry, "A Systematic Approach to Building High Performance Software-based CRC Generators" (2005)
- zlib crc32.c (Slice-by-8 + PCLMULQDQ 实现)
- linux/lib/crc32.c (ARM PMULL 加速实现)

**检测信号**：
- 256 字节 `static const` 数组 + 循环内 `data[i] ^ crc >> 8` 模式
- 或 `crc32b/crc32h/crc32w/crc32x/crc32cx/crc32cb/crc32ch/crc32cw/crc32cx` 汇编指令（说明已用硬件 CRC，但可能展开不足）

## 2. 多项式哈希 / Rolling Hash

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `hash = (hash * P + data[i]) % M`（模运算/循环） | CLMUL 无进位乘法替代整数模（GF(2^k) 域） | 消除除法/取模指令（~20-80 cycle → ~3-5 cycle） | ARMv8.0+ PMULL |
| Rabin-Karp 滚动哈希 | 双多项式窗口哈希（避免除法） | O(Nk) → O(N) | 纯加减，天然 SIMD |

**参考文献**：
- Lemire & Kaser, "Faster 64-bit universal hashing using carry-less multiplications" (2016)
- chromium/bidirectional_stream 的 PMULL 哈希实现

**检测信号**：
- 循环内出现 `%` 常量模运算（尤其是大质数 2^61-1, 2^31-1, 2^64-59 等）
- 或 `* P + data[i]` 模式，P 为大质数

## 3. 通用哈希 / 摘要

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| xxHash32/64（标量） | xxHash NEON 并行分块 | 常数因子 ↓ 2-4× | NEON 128-bit 分块，ARMv8.0+ |
| CityHash/FarmHash | FarmHash aarch64 优化版 | 常数因子 ↓ | 社区已有 NEON 移植 |
| adler32（逐字节） | SIMD 批量字节求和（NEON UADDLV） | O(N)，常数因子 ↓ 4-8× | NEON UADDLV/ADDV |

**检测信号**：
- xxHash: `PRIME32_1/PRIME32_2` 宏定义，或 `_rotl` + XOR + 乘法模式
- adler32: `s1 += data[i]; s2 += s1` 累积模式
