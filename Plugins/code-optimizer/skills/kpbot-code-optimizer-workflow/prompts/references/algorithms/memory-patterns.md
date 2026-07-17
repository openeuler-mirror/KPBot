# 内存操作模式 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 重复拷贝 / 零拷贝

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 循环内多次 `memcpy`/`memmove` | `string_view` / `span<const T>` 零拷贝 | O(N) → O(1)（传指针代替拷贝） | C++17/20，纯语言特性 |
| 模式填充（逐元素递增写入）| `memcpy` 翻倍传播 (doubling copy) | O(N) → O(log N) 次 memcpy 调用 | 适用 Huffman 解码、RLE 解码等 |

**检测信号**：
- `memcpy(dst, src, len)` 出现在循环内，dst 每次偏移 len
- 或 `out[pos++] = pattern[i % pattern_len]` 逐元素填充（应翻倍 `memcpy` 传播）
- 零拷贝机会：函数接收 `const char*` + `size_t`，内部仅读取后传递给子函数——可改为 `string_view`

## 2. memcpy / memset 实现

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| libc `memcpy`（通用实现）| 替换为已知最佳实现（glibc aarch64 memcpy 已高度优化） | 常数因子 ↓ | glibc aarch64 使用 ldp/stp + 展开 |
| `memset(0)` 逐字节 | libc memset（通常已优化） | 常数因子 ↓ | glibc 使用 DC ZVA |
| 大块 memcpy（流式数据，仅用一次）| `ldnp/stnp` non-temporal | 避免 L1/L2 污染 | 仅当数据 > L2 cache 时有收益 |

**检测信号**：
- 手写 `for(i=0; i<n; i++) dst[i] = src[i]` 代替 memcpy 调用
- 手写 `for(i=0; i<n; i++) buf[i] = 0` 代替 memset 调用
- 调用 memcpy 前已确定是大块流式数据（数据仅用一次，不再访问）
