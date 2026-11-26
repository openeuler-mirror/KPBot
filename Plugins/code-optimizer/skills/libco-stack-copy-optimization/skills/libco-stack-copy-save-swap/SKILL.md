---
name: libco-stack-copy-save-swap
description: >
  用于 libco 共享栈切换的栈换出/换入拷贝；区分两个复用方向并选择缓存策略。
  适用于 libco 共享栈、协程栈换出/换入、拷贝占切换热点的场景。触发词：
  libco 共享栈、栈换出、栈换入、拷贝方向、缓存污染。不适用：普通一次性 memcpy、
  无复用方向的拷贝。
---

# save/swap 语义拆分与 temporal hints

## 适用条件

- 协程共享栈在切换时把旧协程栈写进 `save_buffer`，换入时再恢复到栈上。
- 两个方向的复用距离不同：
  - **save/换出**：写后不会立即读 → 非临时存储（`stnp`）。
  - **swap/换入**：写后立即被协程执行读取 → 非临时加载 + 普通存储（`ldnp+stp`）。
- 热点在 `memcpy`/栈拷贝，且共享栈模式开启。

不适用：
- 只有单向拷贝，没有 save/restore 语义。
- 目标平台不是 AArch64/NEON；使用 libc `memcpy`。

## 方法

### 1. 拆成两个入口

```c
void *co_memcpy_save(void *dst, const void *src, unsigned long len);
void *co_memcpy_swap(void *dst, const void *src, unsigned long len);
```

调用点：
- `save_stack_buffer()` → `co_memcpy_save(save_buffer, stack_sp, len)`
- `co_swap()` 恢复 → `co_memcpy_swap(pending->stack_sp, save_buffer, save_size)`

### 2. 参数化 ld/st

```c
#define memcpy_blk_save(dst, src, len, rem)  memcpy_blk_neon(dst, src, len, rem, "ldp",  "stnp")
#define memcpy_blk_swap(dst, src, len, rem)  memcpy_blk_neon(dst, src, len, rem, "ldnp", "stp")
```

大块宏同理，save 传 `"ldp","stnp"`，swap 传 `"ldnp","stp"`。不要复制两套内核；
用宏参数化，减少漂移。

### 3. 非 AArch64 路径

用函数指针/constructor 初始化，默认 `memcpy`；AArch64 下切到自定义实现：

```c
static memcpy_func_name save_func = (memcpy_func_name)memcpy;
static void __attribute__((constructor)) init(void) {
#ifdef __aarch64__
    save_func = (memcpy_func_name)co_memcpy_save_aarch64;
#endif
}
```

## 验证

- 逐字节比较 save/swap 输出；不要只比长度。
- 对齐/尺寸矩阵；tier 临界 ±1。
- save/swap 分别测吞吐，不要只测一个方向。
- 共享栈 ping-pong：depth 1/4/16/32/64/96KB。

## 反例

- 不要把 save 的 `stnp` 用到 swap：恢复后立即读，会伤缓存局部性。
- 不要给 save 的源用 `ldnp`：save 的瓶颈在写侧，不在读侧。
- 不要只测 1KB/64KB 两个点；边界尺寸容易回归。
- 不要在 NEON 内联汇编里使用 q8-q15；用 q0-q7/q16-q31，避免 callee-saved 冲突。

## 迁移到其他应用

- 先确认目标拷贝确有两个复用方向；只有单向拷贝时不要拆 save/swap。
- 把 `co_memcpy_save/swap` 映射到目标栈换出/换入函数；接口名和调用点可换，temporal 语义不变。
- 用目标应用的尺寸分布和 ns/switch（或端到端指标）重测；小尺寸先与 libc 对照再决策。

## 证据

- save 与 swap 按写后复用距离区分；参考实现用 temporal load/store hint 表达。
- 本 skill 固化方法与门禁，不固化性能数字；目标应用需按自身负载复测。
- 共享参考：包级 `references/libco-baseline-and-method-map.md`、`references/other-applicability.md`。
