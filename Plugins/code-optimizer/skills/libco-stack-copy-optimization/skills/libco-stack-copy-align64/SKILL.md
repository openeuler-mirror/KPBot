---
name: libco-stack-copy-align64
description: >
  用于 libco 共享栈/大块拷贝的目标地址未对齐；处理前缀与块循环边界。
  适用于 libco 共享栈拷贝、目标缓冲区非对齐、前缀余数场景。触发词：
  libco 栈拷贝、目标对齐、前缀、非对齐、缓存行。不适用：源/目标天然对齐
  且已验证的实现。
---

# 64B 目标对齐与前缀处理

## 适用条件

- NEON 块内核用 `stp/stnp` 写目标；目标未按 64B 对齐时，块边界跨缓存行。
- 共享栈的 `save_buffer` 或恢复栈地址不保证 64B 对齐。
- 需要把“前缀”与“主循环”分开，主循环只处理对齐后的块。

不适用：
- 目标地址始终 64B 对齐（仍建议保留前缀路径做防御）。
- 块内核已用非对齐安全指令且验证通过；不要无证据删除前缀。

## 方法

```c
void *copy_shared(void *dst, const void *src, size_t len) {
    unsigned char *d = (unsigned char *)dst;
    const unsigned char *s = (const unsigned char *)src;
    /* 距下一 64B 边界的距离，不是 d & 0x3f */
    size_t align_offset = (64 - ((uintptr_t)d & 63)) & 63;
    if (align_offset) {
        if (len <= align_offset) {      // 短拷贝：不要做 len -= align_offset
            memcpy_rem(d, s, len);
            return dst;
        }
        memcpy_rem(d, s, align_offset); // 前缀用余数拷贝
        d += align_offset;
        s += align_offset;
        len -= align_offset;
    }
    // 之后的块循环从 64B 对齐的 d 开始
    return dst;
}
```

要点：
- 前缀长度取 `min(len, (64 - ((uintptr_t)dst & 63)) & 63)`。
- `len <= align_offset` 时必须只拷贝 `len` 并返回，禁止做 `len -= align_offset`。
- 前缀拷贝必须与余数路径共用同一套安全实现，不能越界。
- 主循环只处理 `len / BLOCK_SIZE` 个完整块，剩余 `len % BLOCK_SIZE` 再走余数。
- 对齐后仍要保证总长度正确；前缀 + 块 + 余数 = 原始 len。

## 验证

- 目标偏移矩阵：0/1/3/7/15/31/63。
- 短拷贝矩阵：`len <= dst 偏移`，至少覆盖 (63,31)、(31,16)、(7,1)、(63,0)。
- 源偏移矩阵同样覆盖；源/目标可不同偏移。
- 每个偏移 × tier 临界 ±1 × save/swap，逐字节比对。
- 前后哨兵 16B，检测前缀/余数越界。
- 如果删除对齐前缀，必须用同一矩阵验证正确性和目标场景表现；否则保留。
- 测试必须断言进入首个完整块时目标地址满足 `(uintptr_t)dst % 64 == 0`。

## 反例

- 不要删除 64B 对齐前缀：该实验曾被回滚。
- 不要用固定 64B 前缀；未对齐量是 `dst & 0x3f`。
- 不要在主循环里混入未对齐块，导致 `stp` 跨行/性能抖动。
- 不要在 swap 路径单独去对齐；两个方向都需要。

## 迁移到其他应用

- 先确认目标分配器/调用方是否已保证 dst 64B 对齐；已保证可省前缀，未保证则保留。
- 把对齐宏和前缀处理映射到目标拷贝入口，不改变长度/返回值契约。
- 用目标偏移分布（0/1/3/7/15/31/63）验证；小尺寸需单独验证，不要由大块结果外推。

## 证据

- 块存储按目标 64B 对齐可减少跨缓存行；参考实现保留前缀路径。
- 本 skill 固化方法与门禁，不固化性能数字；目标应用需按自身负载复测。
- 共享参考：包级 `references/libco-baseline-and-method-map.md`、`references/other-applicability.md`。
