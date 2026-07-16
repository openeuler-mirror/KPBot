# 预取优化指南

## 概述

预取（Prefetch）是 CPU 的一种优化技术，通过提前将数据从内存加载到缓存中，减少 CPU 等待内存访问的时间。

## 预取类型

### 1. 硬件预取（Hardware Prefetch）

CPU 硬件自动检测访问模式并预取数据。

**触发条件：**
- 检测到连续的内存访问模式
- 跨步访问（stride <= 2 cache lines）

**优点：**
- 无需代码修改
- 自动适应运行时的访问模式

**缺点：**
- 无法处理复杂访问模式
- 跨步访问 stride > 2 时可能失效

### 2. 编译器预取（Compiler Prefetch）

编译器自动分析循环并插入预取指令。

**GCC 选项：**
```bash
gcc -O3 -fprefetch-loop-arrays target.c -o target
```

**Clang 选项：**
```bash
clang -O3 -fprefetch-loop-arrays target.c -o target
```

### 3. 软件预取（Software Prefetch）

程序员手动插入预取指令。

**GCC/Clang intrinsics：**
```c
__builtin_prefetch(addr, rw, locality);
```

**参数说明：**
- `addr`: 要预取的内存地址
- `rw`: 0 = 准备读取, 1 = 准备写入
- `locality`: 0-3，缓存层级提示
  - 0: 非临时，预取后不保留在缓存
  - 3: 最临时，强烈保留在缓存

## 预取距离计算

### 公式

```
PREFETCH_DISTANCE = stride × (CACHE_LINE_SIZE / ELEMENT_SIZE) × 2
```

### 示例

假设：
- 缓存行大小：64 字节
- 元素大小：8 字节（double）
- stride：4

```
PREFETCH_DISTANCE = 4 × (64 / 8) × 2 = 16
```

### 预取距离参考表

| stride | 元素大小 | 缓存行 | 预取距离 |
|--------|---------|--------|---------|
| 1 | 8 (double) | 64 | 16 |
| 2 | 8 | 64 | 8 |
| 4 | 8 | 64 | 4 |
| 8 | 8 | 64 | 2 |
| 16 | 8 | 64 | 1 |

## 优化策略选择

### 决策流程

```
访问模式分析
    │
    ├── 顺序访问 (stride = 1)
    │       └── 编译器预取 (-fprefetch-loop-arrays)
    │
    ├── 跨步访问 (stride > 1, stride <= 16)
    │       └── 软件预取 (__builtin_prefetch)
    │
    └── 随机访问 / 复杂模式
            └── 考虑数据布局重构或 cache-blocking
```

### 顺序访问优化

```c
// 编译器优化
gcc -O3 -fprefetch-loop-arrays -march=native target.c -o target

// 源代码无需修改
for (int i = 0; i < N; i++) {
    a[i] = b[i] + c[i];
}
```

### 跨步访问优化

```c
#define PREFETCH_DISTANCE 16

for (int i = 0; i < N; i += 4) {
    // 预取下一次迭代的数据
    if (i + PREFETCH_DISTANCE < N) {
        __builtin_prefetch(&a[i + PREFETCH_DISTANCE], 0, 3);
    }
    // 处理当前数据
    process(a[i]);
}
```

### 嵌套循环优化（Cache Blocking + Prefetch）

```c
#define BLOCK_SIZE 64
#define PREFETCH_DISTANCE 16

for (int i = 0; i < N; i += BLOCK_SIZE) {
    for (int j = 0; j < M; j += BLOCK_SIZE) {
        // 预取下一个 block
        if (i + BLOCK_SIZE < N) {
            for (int k = j; k < j + BLOCK_SIZE; k += 4) {
                if (k + PREFETCH_DISTANCE < M) {
                    __builtin_prefetch(&a[i + BLOCK_SIZE][k + PREFETCH_DISTANCE], 0, 3);
                }
            }
        }

        // 处理当前 block
        for (int ii = i; ii < min(i + BLOCK_SIZE, N); ii++) {
            for (int jj = j; jj < min(j + BLOCK_SIZE, M); jj++) {
                c[ii][jj] = a[ii][jj] + b[ii][jj];
            }
        }
    }
}
```

## 常见错误

### 1. 预取距离过近

```c
// 错误：预取距离 = 1，数据还没用到就已经换出
for (int i = 0; i < N; i++) {
    __builtin_prefetch(&a[i + 1], 0, 3);  // 距离太近
    process(a[i]);
}
```

### 2. 预取距离过远

```c
// 错误：预取距离过大，可能污染缓存
for (int i = 0; i < N; i++) {
    __builtin_prefetch(&a[i + 10000], 0, 3);  // 距离太大
    process(a[i]);
}
```

### 3. 预取无效的地址

```c
// 错误：预取地址可能无效
for (int i = 0; i < N; i++) {
    if (i + PREFETCH_DISTANCE < N) {  // 必须检查边界
        __builtin_prefetch(&a[i + PREFETCH_DISTANCE], 0, 3);
    }
    process(a[i]);
}
```

### 4. 过度预取

```c
// 错误：在一个循环中预取太多会导致缓存污染
for (int i = 0; i < N; i++) {
    __builtin_prefetch(&a[i], 0, 3);   // 预取 a
    __builtin_prefetch(&b[i], 0, 3);   // 预取 b
    __builtin_prefetch(&c[i], 0, 3);   // 预取 c
    __builtin_prefetch(&d[i], 0, 3);   // 预取 d - 可能过多
    process(a[i], b[i], c[i], d[i]);
}
```

## 性能验证

### 验证步骤

1. **编译基准版本**
   ```bash
   gcc -O2 -g target.c -o target_baseline
   ```

2. **编译优化版本**
   ```bash
   gcc -O3 -fprefetch-loop-arrays target.c -o target_optimized
   # 或添加软件预取后编译
   ```

3. **运行对比**
   ```bash
   # 执行时间对比
   time ./target_baseline
   time ./target_optimized

   # 缓存指标对比
   perf stat -e cache-misses,L1-dcache-load-misses ./target_baseline
   perf stat -e cache-misses,L1-dcache-load-misses ./target_optimized
   ```

4. **判断优化效果**
   - 执行时间减少 > 10%：有效
   - cache-misses 减少 > 20%：显著有效
   - 性能反而下降：回退优化

## ARM64 汇编 prfm 指令参考

在汇编文件 (.s/.S) 或内联 asm 中直接插入 prfm 指令的参考：

### prfm 指令格式

```assembly
prfm <type>, [<Xn|SP>{, #<pimm>}]          @ 基址寄存器 + 立即数偏移
prfm <type>, [<Xn|SP>, <Xm>{, <extend>}]   @ 基址寄存器 + 索引寄存器
```

### 常用 prfm 类型速查表

| 类型 | 含义 | locality | 适用场景 |
|------|------|----------|----------|
| `pldl1keep` | 预取到 L1 并保持 | 3 | 被多次访问的数据（visited 数组、累加器） |
| `pldl1strm` | 预取到 L1，用完即 evict | 0 | 一次性数据（距离计算的向量） |
| `pldl2keep` | 预取到 L2，不进 L1 | 2 | 大工作集，避免 L1 污染 |
| `pldl3keep` | 预取到 L3 | 1 | 跨 NUMA 数据 |
| `pstl1keep` | 预取写入到 L1 | 3(write) | 写入前预分配缓存行 |

### 汇编循环插入示例

```assembly
// 典型 NEON 向量循环，每次处理 4 个 float（16 字节）
.Lloop:
    // distance=16 元素 × 4 字节 = 64 字节偏移
    prfm pldl2keep, [x0, #64]    // 预取 a[i+16]
    prfm pldl2keep, [x1, #64]    // 预取 b[i+16]
    ldp q0, q1, [x0], #32        // 加载 a[i..i+7]（2×4 floats）
    ldp q2, q3, [x1], #32        // 加载 b[i..i+7]
    fmla v4.4s, v0.4s, v2.4s     // 乘加
    fmla v5.4s, v1.4s, v3.4s
    subs x2, x2, #8
    b.ne .Lloop
```

### 注意事项

- prfm 不占用通用寄存器，不影响标志位
- prfm 对无效地址不产生异常（CPU 忽略）
- 偏移量 #pimm 范围：0-32760（字节），必须是 8 的倍数
- 汇编文件中 prfm 必须用 pimm 或 register offset 形式，不能用 `[reg]` 裸寄存器形式

## 回退方法

```bash
# 1. 移除编译器预取选项
gcc -O2 target.c -o target_no_prefetch

# 2. 注释掉软件预取
# if (i + PREFETCH_DISTANCE < N) {
#     __builtin_prefetch(&a[i + PREFETCH_DISTANCE], 0, 3);
# }

# 3. 使用 git 回滚
git checkout target.c
```
