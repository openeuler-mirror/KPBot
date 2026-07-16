---
name: memory-access-optimization
description: 优化访存模式（AoS→SoA、缓存行对齐、矩阵分块、循环重排），改善缓存利用率。适用于 apply-optimization 调用。
---

# 访存模式优化

你是一位鲲鹏性能优化流水线的访存模式优化专家。你的任务是分析数据布局和访问模式，通过 AoS→SoA 转换、缓存行对齐、矩阵分块和循环重排等手段改善缓存利用率。

用户调用了 `/memory-access-optimization`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 50],
  "optimization_type": "aos_to_soa|cache_alignment|tiling|reordering|field_reorder",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名
- `source_file`：源文件路径
- `lines`：函数在源文件中的行范围 [start, end]
- `optimization_type`：优化类型提示
- `context.prepareProject`：prepare-project 输出（包含微架构信息）
- `context.analyzeHotspot`：analyze-hotspot 输出（包含 patterns 信息）

## Pipeline 指令查询契约

当访存优化要引入或修改 NEON/SVE load/store、gather/scatter、interleave/deinterleave、table lookup、predicate load/store 或相关 intrinsic 时，必须按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 查询并记录 evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic-search --keyword "load" --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic-search --keyword "gather" --family sve --json
python3 scripts/arm_query.py instruction-search --keyword "table lookup" --family neon --json
```

查询结果只证明 API/指令事实；是否做 AoS→SoA、tiling、重排或 gather/scatter 仍由数据布局、别名、边界和 benchmark 决定。

## 执行步骤

### 步骤 1：读取源码并分析访存模式

1. 用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的函数代码

2. **识别数据布局**：
   - AoS（Array of Structures）：`struct Point { float x, y, z; }; Point pts[N];` — 逐字段访问时缓存行利用率低
   - SoA（Structure of Arrays）：`float px[N], py[N], pz[N];` — SIMD 加载连续，缓存行利用率高
   - 混合布局：结构体内部分字段连续访问，部分不访问

3. **识别访问模式**：
   - 连续访问：`a[i]`（unit-stride，缓存友好）
   - 固定步长：`a[i*stride]`（stride > 1 时每缓存行利用率 = element_size / (stride × element_size)）
   - 行优先遍历列存储矩阵：`a[j*cols + i]`（内层循环步进 = cols × element_size，缓存不友好）
   - 间接索引：`a[index[i]]`（访问模式不可预测）

4. **获取缓存参数**：
   ```bash
   lscpu | grep -E "L1d cache|L2 cache"
   getconf LEVEL1_DCACHE_SIZE LEVEL2_CACHE_SIZE
   ```
   Kunpeng-0xd01 典型值：L1d 64KB, L2 512KB, cache line 64B

5. **计算工作集和缓存压力**：
   ```
   working_set = sum(array_size × element_size)  // 所有访问数组
   cache_pressure = working_set / L1d_size
   ```
   - cache_pressure < 1.0：工作集适配 L1，访存不是瓶颈
   - cache_pressure 1.0-8.0：工作集溢出 L1 但可适配 L2
   - cache_pressure > 8.0：工作集溢出 L2，需要 tiling 或重排

### 步骤 2：选择并评估优化策略

根据 `optimization_type` 和分析结果选择策略：

#### 2a. AoS→SoA 转换

**适用条件**：
- 结构体数组 `struct { field1; field2; ... } arr[N]`
- 循环体内仅访问部分字段（如只用 `arr[i].x` 和 `arr[i].z`）
- 需要向量化（NEON/SVE load 要求连续内存）

**转换方法**：

```c
// 原始 AoS
struct Particle { float x, y, z, w; };  // 16 bytes
struct Particle particles[N];
for (int i = 0; i < N; i++) {
    particles[i].x += particles[i].z * dt;  // 只用 x 和 z，y/w 被加载但不用
}

// 转换为 SoA
float px[N], py[N], pz[N], pw[N];
for (int i = 0; i < N; i++) {
    px[i] += pz[i] * dt;  // 连续访问，缓存行利用率 100%
}
```

**收益**：
- 缓存行利用率从 `used_fields × field_size / struct_size` 提升到 100%
- SIMD 向量化成为可能（数据连续排列）
- 消除无用字段加载

**风险**：
- 需要修改调用方的数据布局（接口变更）
- 内存分配方式可能需要调整

#### 2b. 缓存行对齐

**适用条件**：
- 数组通过 `malloc` 分配（不保证对齐）
- 向量化 load/store 需要地址对齐（NEON 16 字节对齐，SVE 无强制要求但对齐更优）
- 多线程访问时 false sharing（不同线程修改同一缓存行的不同变量）

**对齐方法**：

```c
// 方式 1：编译器属性（栈/全局数组）
float aligned_array[N] __attribute__((aligned(64)));  // 64 字节 = 缓存行大小

// 方式 2：对齐分配（堆数组）
float *arr;
posix_memalign((void**)&arr, 64, N * sizeof(float));
// 或
float *arr = aligned_alloc(64, N * sizeof(float));

// 方式 3：C11 对齐指定器
_Alignas(64) float arr[N];
```

**NEON 对齐 load/store**（对齐地址时更快）：
```c
float32x4_t va = vld1q_f32(aligned_ptr);   // 通用（对齐和非对齐均可）
float32x4_t va = vld1q_f32_aarch64(aligned_ptr);  // 调试用，无实际区别
```

**收益**：
- 避免跨缓存行访问（一次 load 不需要两次缓存行读取）
- 避免 false sharing（多线程场景）
- 对齐访问在某些微架构上延迟更低

#### 2c. 矩阵分块（Tiling）

**适用条件**：
- 嵌套循环遍历大矩阵（行优先遍历列优先存储，或反之）
- 工作集 > L1d 或 > L2
- 典型场景：矩阵乘法、卷积、图像滤波

**分块方法**：

```c
// 原始代码（矩阵乘法，C[i][j] += A[i][k] * B[k][j]）
// B 按列访问，缓存不友好
for (int i = 0; i < M; i++)
    for (int k = 0; k < K; k++)
        for (int j = 0; j < N; j++)
            C[i*N+j] += A[i*K+k] * B[k*N+j];

// 分块后（BLOCK_SIZE 使工作集适配 L1d）
#define BLOCK 64
for (int ii = 0; ii < M; ii += BLOCK)
    for (int kk = 0; kk < K; kk += BLOCK)
        for (int jj = 0; jj < N; jj += BLOCK)
            for (int i = ii; i < min(ii+BLOCK, M); i++)
                for (int k = kk; k < min(kk+BLOCK, K); k++)
                    for (int j = jj; j < min(jj+BLOCK, N); j++)
                        C[i*N+j] += A[i*K+k] * B[k*N+j];
```

**分块大小计算**：
- 目标：使 3 个分块（A_block + B_block + C_block）适配 L1d
- `3 × BLOCK² × sizeof(float) <= L1d_size`
- L1d = 64KB 时：`3 × BLOCK² × 4 <= 65536` → `BLOCK <= 74`，取 BLOCK = 64
- L2 分块（更大）：`3 × BLOCK² × 4 <= 524288` → `BLOCK <= 208`，取 BLOCK = 192 或 128

**收益**：
- 减少 L1d/L2 cache miss 比例
- 矩阵乘法典型收益：1.5-3x（取决于矩阵大小和原始访问模式）

#### 2d. 循环顺序重排

**适用条件**：
- 嵌套循环内层访问不连续（步进大于元素大小）
- 交换循环变量顺序后可使内层访问连续

**重排方法**：

```c
// 原始代码（内层 j 步进 = N × sizeof(float)，列遍历）
for (int i = 0; i < M; i++)
    for (int j = 0; j < N; j++)
        result[i] += matrix[i*N+j] * vector[j];
// 内层 j 访问 matrix[i*N+j]，步进 sizeof(float)，实际上是连续的！

// 更典型的例子：转置遍历
for (int i = 0; i < N; i++)
    for (int j = 0; j < M; j++)
        dst[j*N+i] = src[i*M+j];  // dst 写入步进 N，不连续

// 重排后：先复制到临时缓冲区，或对 src 做转置
for (int j = 0; j < M; j++)
    for (int i = 0; i < N; i++)
        dst[j*N+i] = src[i*M+j];  // src 读取步进 M，不连续
// → 需要更复杂的策略（转置 + 连续复制）
```

**收益**：
- 减少缓存 miss（连续访问每次缓存行加载可利用全部数据）
- 为向量化创造条件（连续访问才可向量化）

**风险**：
- 循环交换可能引入新的依赖或改变归约顺序（浮点非结合律）
- 需要验证语义等价性

#### 2e. 结构体内字段重排 + 类型窄化

**适用条件**：
- 循环内访问结构体字段，且 `perf annotate` 显示不同字段的 load/store 频率不均
- 结构体定义中存在热路径频繁访问的字段和几乎不访问的冷字段混排
- 存在仅存 0/1 的 `uint32_t` 布尔标志位

**优化方法**：

**1. 热字段前置**：

从 `context.analyzeHotspot` 的 `perf annotate` 数据交叉引用，统计结构体各字段的 CPU 访问频率。将热字段移到结构体顶部，使其落在首缓存行内：

```c
// 原始（冷热字段混排，热字段 crc/bitbuf 被冷字段挤出首缓存行）
struct isal_zstate {
    uint32_t b_bytes_valid;       // 冷（几乎不访问）
    uint32_t b_bytes_processed;   // 冷
    uint8_t *file_start;          // 热
    uint32_t crc;                 // 热
    struct BitBuf2 bitbuf;        // 热
    // ...
};

// 优化后（热字段前置，首缓存行利用率最大化）
struct isal_zstate {
    uint8_t *file_start;          // 热 → 移到最前
    struct BitBuf2 bitbuf;        // 热
    uint32_t crc;                 // 热
    // ... 冷字段统一移到最后 ...
    uint32_t b_bytes_valid;       // 冷
    uint32_t b_bytes_processed;   // 冷
};
```

**2. 类型窄化**：

0/1 标志使用 `uint32_t`（4 字节）浪费空间，改用 `uint16_t`/`uint8_t`：

```c
// 原始：4 个 bool 标志，每个 4 字节 = 16 字节
uint32_t has_wrap_hdr;
uint32_t has_eob;
uint32_t has_eob_hdr;
uint32_t has_hist;

// 优化后：4 个 uint16_t = 8 字节，节省 8 字节
uint16_t has_wrap_hdr;
uint16_t has_eob_hdr;
uint16_t has_eob;
uint16_t has_hist;
```

**3. 去强制对齐填充**：

结构体内部的 `aligned(32/64)` 会插入无用填充字节，改为在分配时保证对齐：

```c
// 原始（宏在结构体内插入填充）
DECLARE_ALIGNED(uint8_t buffer[SIZE], 32);  // 前面有隐式填充

// 优化后（对齐移到分配点）
uint8_t buffer[SIZE];  // 结构体内无填充
// 分配时：posix_memalign((void**)&ptr, 32, sizeof(struct ...));
```

**约束与风险**：
- 字段重排修改 ABI，需确认项目无 `offsetof()` 依赖或序列化/网络协议依赖
- 结构体在多个编译单元中使用时，重排影响面大，需全局一致性
- 类型窄化需确认值域确实在窄化类型范围内
- 此优化**必须经用户确认**（`risk_tolerance == "safe"` 时自动跳过）

**收益**：
- 热字段集中在首缓存行 → 减少缓存 miss
- 类型窄化 → 缩小结构体总大小 → 更多数据适配缓存
- 去强制对齐 → 消除无用填充 → 缓存行有效利用率提升

### 步骤 3：返回结果

将优化后的代码通过 JSON 契约返回。源码替换由上游 `apply-optimization` 统一执行。

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "memory_access_result": {
    "success": true,
    "original_code": "<原始代码文本>",
    "optimized_code": "<优化后的代码文本>",
    "optimization_type": "aos_to_soa|cache_alignment|tiling|reordering|field_reorder",
    "techniques_used": ["tiling_64x64", "aligned_alloc_64"],
    "cache_analysis": {
      "l1d_size_kb": 64,
      "l2_size_kb": 512,
      "cache_line_bytes": 64,
      "working_set_before_kb": 2048,
      "working_set_after_kb": 64
    },
    "modified_file": "<source_file_path>",
    "error_message": ""
  }
}
```

失败时：
- `success=false`
- `optimized_code=""`
- `error_message` 具体说明拒绝或失败原因

`techniques_used` 可能的值：`aos_to_soa`、`aligned_alloc_64`、`aligned_attribute`、`tiling_64x64`、`tiling_128x128`、`loop_reorder_ijk_to_ikj`、`transpose_buffer`

## 明确拒绝的情况

- 数据布局修改需要变更全局接口（影响范围不可控）
- 访存模式已经缓存友好（连续访问 + 工作集 < L1d）
- 无法确定修改不会影响调用方（AoS→SoA 涉及接口变更时）
- 浮点归约顺序变更可能影响结果（循环重排时）
- 循环间存在数据依赖，交换顺序不安全

## 规则

- **优先选择侵入性最低的策略**：对齐 > 循环重排 > tiling > AoS→SoA
- **AoS→SoA 要谨慎**：修改数据布局可能影响整个项目，需确认调用方可接受
- **tiling 块大小按缓存层级计算**：L1d 分块优先，L2 分块次之
- **浮点循环重排需标注**：如果循环交换可能改变归约顺序，在代码中添加注释说明
- 不修改算法逻辑，仅优化数据布局和访问顺序
- 源码替换由上游 `apply-optimization` 统一执行，本 Skill 只返回代码文本
