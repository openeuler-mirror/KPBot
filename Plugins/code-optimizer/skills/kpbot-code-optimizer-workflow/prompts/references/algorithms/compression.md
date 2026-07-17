# 压缩 / 解压缩 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. Huffman 解码

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 逐 bit 遍历 Huffman 树 | 查表法 Huffman 解码 (table-based) | O(B) → O(B/K)，K=查表宽度 | NEON TBL 16 字节并行查表 |
| 查表法 | 多符号同时解码（SIMD bit 流解析） | 常数因子 ↓ 2-8× | NEON SHRN（movemask 替代）+ TBL |

**检测信号**：
- `while(bits < needed) { bits |= *src++ << pos; pos += 8; }` 逐 bit 读取
- 或 Huffman 树遍历：`node = tree[node].child[bit]`

## 2. LZ77 / 滑动窗口匹配

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 朴素 O(NW) 匹配 | 哈希链 (hash chain) | O(NW) → O(N) 期望 | zlib/deflate 标准实现 |
| 哈希链 | 后缀数组/后缀树（极限压缩率） | O(N)，压缩率 ↑ | 内存开销大，慎用 |
| 字符串匹配（字节级）| SIMD 字节比较 (NEON CMEQ + AND reduction) | 常数因子 ↓ 4-16× | NEON 16 字节并行比较 |

## 3. 熵编码

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| Huffman 编码 | 范式 Huffman (canonical Huffman) | 内存 ↓，编解码更快 | zstd/zlib 已使用 |
| 算术编码 | 范围编码 (range coding) | O(N)，常数因子 ↓（只用整数运算） | 无特殊指令依赖 |
| 范围编码 | ANS/FSE (Asymmetric Numeral Systems) | O(N)，常数因子 ↓ 2-4× | zstd/FSE 已有实现 |

**检测信号**：
- 范围编码：`range >>= shift; low += range * prob;` 整数区间更新模式
- ANS: 状态机 `state = table[state].next + (bits & mask)` 模式
