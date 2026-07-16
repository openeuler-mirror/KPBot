## 9. 内存预取内联函数

ACLE 提供了用于显式数据和指令预取的内联函数。这些是对 CPU 的提示 — 在某些硬件上可能被实现为空操作。

### 简单数据预取

```c
#include <arm_acle.h>

// 预取到最内层缓存以供读取
__pld(addr);

// 预取到最内层缓存以供写入
// （没有简单内联函数 — 使用下面的 __pldx）
```

### 详细数据预取（`__pldx`）

```c
// __pldx(access_kind, cache_level, retention_policy, addr)

// 预取以供 READ 到 L1，时间性（保留在缓存中）
__pldx(0, 0, 0, addr);

// 预取以供 WRITE 到 L2，流式（使用一次后驱逐）
__pldx(1, 1, 1, addr);
```

**参数值：**

| 参数 | 值 | 含义 |
|------|------|------|
| 访问类型 | 0 | `PLD` — 预取以供读取 |
|          | 1 | `PST` — 预取以供写入 |
| 缓存级别 | 0 | L1 |
|          | 1 | L2 |
|          | 2 | L3 |
|          | 3 | SLC（系统级缓存） |
| 保留策略 | 0 | `KEEP` — 时间性（正常分配到缓存中） |
|          | 1 | `STRM` — 流式（仅使用一次） |

### 范围预取（高级）

```c
#if defined(__ARM_PREFETCH_RANGE)
// 预取已知的访问模式：length 字节，count 个块，
// 块间 stride 字节，重用距离
__pldx_range(0, 0, length, count, stride, reuse_distance, base_addr);

// 紧凑形式，打包的元数据
__pld_range(0, 0, metadata_u64, base_addr);
#endif
```

### 指令预取

```c
__pli(func_addr);              // 预取到最内层统一缓存
__plix(0, 0, func_addr);       // 预取到 L1，时间性
```

### 实用预取模式

```c
// 处理当前迭代时预取下一次迭代的数据
void process_with_prefetch(float *data, int n) {
    const int BLOCK = 64;  // 缓存行大小（以 float 为单位）
    for (int i = 0; i < n; i += BLOCK) {
        // 预取下一次迭代的数据
        if (i + BLOCK < n) {
            __pld(&data[i + BLOCK]);
        }
        // 处理当前块
        for (int j = 0; j < BLOCK && i + j < n; j++) {
            data[i + j] = compute(data[i + j]);
        }
    }
}
```

**注意事项：**
- 预取内联函数是**提示** — CPU 可能忽略它们
- 过度预取会损害性能（缓存污染）
- 步长和访问模式必须是可预测的，预取才能发挥作用
- 在某些实现上这些可能是完全的空操作

---
