## 4. 成功模式

### 4.1 SVE 向量化循环模板

```c
#include <arm_sve.h>

// 两个数组相加：result[i] = a[i] + b[i]
void add_arrays_sve(float *restrict result,
                    const float *restrict a,
                    const float *restrict b,
                    int n)
{
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t va = svld1_f32(pg, &a[i]);
        svfloat32_t vb = svld1_f32(pg, &b[i]);
        svfloat32_t vr = svadd_f32_x(pg, va, vb);
        svst1_f32(pg, &result[i], vr);
    }
}
```

要点：
- `svwhilelt_b32(i, n)` 为元素 `i..n-1` 创建谓词
- `svcntw()` 返回每个向量中 32 位元素的数量（运行时值）
- `_x` 谓词模式：非活跃元素不关心（性能最佳）
- `restrict` 关键字帮助编译器优化加载/存储

### 4.2 Neon 向量化循环模板

```c
#include <arm_neon.h>

void add_arrays_neon(float *restrict result,
                     const float *restrict a,
                     const float *restrict b,
                     int n)
{
    int i = 0;
    // 主向量化循环
    for (; i + 4 <= n; i += 4) {
        float32x4_t va = vld1q_f32(&a[i]);
        float32x4_t vb = vld1q_f32(&b[i]);
        float32x4_t vr = vaddq_f32(va, vb);
        vst1q_f32(&result[i], vr);
    }
    // 尾部处理（标量）
    for (; i < n; i++) {
        result[i] = a[i] + b[i];
    }
}
```

### 4.3 归约模式

```c
// SVE：求所有元素之和
float sum_sve(const float *data, int n) {
    svfloat32_t acc = svdup_f32(0.0f);
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        acc = svadd_f32_x(pg, acc, v);
    }
    return svaddv_f32(svptrue_b32(), acc);  // 水平求和
}

// Neon：求所有元素之和
float sum_neon(const float *data, int n) {
    float32x4_t acc = vdupq_n_f32(0.0f);
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t v = vld1q_f32(&data[i]);
        acc = vaddq_f32(acc, v);
    }
    float32x2_t sum2 = vadd_f32(vget_low_f32(acc), vget_high_f32(acc));
    float result = vget_lane_f32(vpadd_f32(sum2, sum2), 0);
    for (; i < n; i++) result += data[i];
    return result;
}
```

### 4.4 加宽/收窄模式

```c
// SVE：加宽 int16 → int32，计算，再收窄回来
void widen_compute_sve(const int16_t *a, const int16_t *b,
                       int16_t *result, int n) {
    for (int i = 0; i < n; i += svcnth()) {  // 16 位步长
        svbool_t pg = svwhilelt_b16(i, n);
        svint16_t va = svld1_s16(pg, &a[i]);
        svint16_t vb = svld1_s16(pg, &b[i]);

        // 加宽到 32 位（低半部分）
        svint32_t va_w = svunpklo_s32(va);
        svint32_t vb_w = svunpklo_s32(vb);
        svint32_t vr_w = svadd_s32_x(svptrue_b32(), va_w, vb_w);

        // 收窄回 16 位
        svint16_t vr = svqxtnb_s16(svdup_s32(0), vr_w);
        svst1_s16(pg, &result[i], vr);
    }
}
```

### 4.5 谓词组合模式（SVE）

```c
// 组合谓词实现条件操作
void conditional_add_sve(float *data, int n, float threshold) {
    svfloat32_t thresh_v = svdup_f32(threshold);
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);

        // 为大于阈值的元素创建谓词
        svbool_t above = svcmpgt_f32(pg, v, thresh_v);

        // 仅对大于阈值的元素加 1
        svfloat32_t result = svadd_f32_m(above, v, v, svdup_f32(1.0f));
        svst1_f32(pg, &data[i], result);
    }
}
```

### 4.6 NEON-SVE Bridge 迁移模式

```c
#include <arm_neon.h>
#include <arm_sve.h>
#include <arm_neon_sve_bridge.h>

// 使用 Bridge 逐步将 Neon 代码迁移到 SVE
void mixed_neon_sve(float *data, int n) {
    // 用 Neon 处理前 128 位（已有代码）
    float32x4_t neon_chunk = vld1q_f32(data);
    neon_chunk = vmulq_f32(neon_chunk, neon_chunk);

    // 将 Neon 结果转换为 SVE 继续处理
    svfloat32_t sve_v = svset_neonq_f32(svundef_f32(), neon_chunk);

    // 用 SVE 处理剩余元素
    for (int i = 4; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        svst1_f32(pg, &data[i], svmul_f32_x(pg, v, v));
    }

    // 将第一个块存回
    vst1q_f32(data, svget_neonq_f32(sve_v));
}
```

### 4.7 Gather/Scatter 模式（SVE）

```c
// 使用 gather load 进行间接内存访问
void gather_add_sve(float *result,
                    const float *data,
                    const int32_t *indices,
                    int n) {
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);

        // 加载索引
        svint32_t idx = svld1_s32(pg, &indices[i]);

        // Gather：result[i] = data[indices[i]]
        svfloat32_t gathered = svld1_gather_s32index_f32(pg, data, idx);

        svst1_f32(pg, &result[i], gathered);
    }
}
```

### 4.8 SVE 寻址模式

SVE 加载/存储内联函数通过后缀修饰符支持多种寻址模式。基址始终是
指针；偏移量可以是标量（字节、元素或整个向量）或偏移/索引向量。

**标量偏移：**

| 后缀 | 偏移单位 | 示例 |
|------|----------|------|
| _(无)_ | 仅基址指针，元素 `0..N-1` | `svld1_s16(pg, base)` |
| `_offset` | 64 位标量**字节**偏移 | `svld1_offset_s16(pg, base, byte_off)` |
| `_index` | 64 位标量**元素**偏移 | `svld1_index_s16(pg, base, elem_idx)` |
| `_vnum` | 64 位标量，以整个向量为单位计数 | `svld1_vnum_s16(pg, base, 2)` = 跳过 2 个向量 |

```c
// 从第 3 个向量的数据开始加载
svint16_t v = svld1_vnum_s16(pg, base, 2);
// 等价于从 base + 2 * svcnth() 加载

// 使用字节偏移加载
svint16_t v = svld1_offset_s16(pg, base, 64);  // 跳过 64 字节
```

**向量偏移（gather/scatter）：**

| 后缀 | 偏移 | 元素 i 的地址 |
|------|------|--------------|
| `_[s32]offset` | 有符号 32 位**字节**偏移向量 | `(char*)base + offsets[i]` |
| `_[u32]offset` | 无符号 32 位字节偏移向量 | `(char*)base + offsets[i]` |
| `_[s32]index` | 有符号 32 位**元素**索引向量 | `&base[indices[i]]` |
| `_[s64]offset` | 64 位字节偏移向量 | `(char*)base + offsets[i]` |
| `_[s64]index` | 64 位元素索引向量 | `&base[indices[i]]` |

```c
// 使用字节偏移进行 Gather（对应 SXTW 寻址模式）
svint32_t v = svld1_gather_s32offset_s32(pg, base, byte_offsets);

// 使用元素索引进行 Scatter
svst1_scatter_s32index_s32(pg, base, indices, values);

// 向量基址配标量索引
svint32_t v = svld1_gather_u32base_index_s32(pg, bases, 3);
// 元素 i 的地址：((int32_t*)(uintptr_t)bases[i]) + 3
```

**要点：** 偏移量**不需要**是常量或在特定范围内 — 编译器会处理转换。

### 4.9 SVE 标量操作（`_n` 后缀）

SVE 算术内联函数通过 `_n` 消歧符接受标量作为最终操作数。编译器会
选择最高效的指令形式：可能时使用立即数，否则使用寄存器广播。

```c
// 整数常量 — 可能编译为 ADD 立即数形式
svint32_t r = svadd_n_s32_x(pg, a, 1);

// 从内存加载的标量 — 可能编译为 LD1RD + ADD
svfloat64_t r = svadd_n_f64_x(pg, a, *ptr);

// 浮点常量 — 广播到所有通道
svfloat32_t r = svmul_n_f32_x(pg, a, 2.0f);
```

**规则：**
1. `_n` 表示"最终操作数是标量"（不是向量）
2. 编译器可以自动使用立即数形式 — 无需手动检查范围
3. 没有 `_n` 时，最终操作数必须是匹配类型的向量

**常见 AI 错误：**
```c
// ❌ 错误：将标量传给向量操作数内联函数
svint32_t r = svadd_s32_x(pg, a, 1);    // 错误：1 不是 svint32_t

// ✅ 正确：对标量操作数使用 _n
svint32_t r = svadd_n_s32_x(pg, a, 1);

// ✅ 正确：先将标量广播为向量
svint32_t r = svadd_s32_x(pg, a, svdup_s32(1));
```

### 4.10 First-Faulting Loads（推测性内存访问）

SVE 提供了 first-faulting loads，可以安全地尝试加载超出可访问
内存边界的数据。只有第一个活跃元素会触发故障；后续会触发故障的
元素会被静默跳过，并记录在 First Fault Register（FFR）中。

**典型模式 — 带安全性的推测性读取：**
```c
#include <arm_sve.h>

// 安全地处理链表或稀疏数据结构
void speculative_process(float *data, int n) {
    svsetffr();  // 将 FFR 重置为"全部有效"

    svbool_t pg = svptrue_b32();

    // 第一个活跃元素正常触发故障（检测真正的错误）
    // 其他会触发故障的元素被静默跳过
    svfloat32_t v = svldff1_f32(pg, data);

    // 检查哪些元素成功加载
    svbool_t valid = svrdffr();

    // 仅处理成功加载的元素
    svfloat32_t result = svadd_f32_z(valid, v, v);

    // 如果 valid != pg，某些元素失败了 — 单独处理它们
    if (!svptest_any(svptrue_b32(), svnot_b_z(svptrue_b32(), valid))) {
        // 所有元素都成功加载 — 快速路径
    }
}
```

**Non-faulting load（抑制所有故障）：**
```c
// 任何元素都不触发故障 — 甚至第一个活跃元素也被抑制
svfloat32_t v = svldnf1_f32(pg, data);
// 适用于想探测内存而不触发任何信号处理的场景
```

**要点：**
- 使用 `svsetffr()` 开始一个新的 FFR 组
- 使用 `svrdffr()` 读取哪些元素成功了
- 使用 `svptest_*` 检查是否所有元素都已加载
- 始终与显式 FFR 读/写配对 — 编译器需要它们进行正确优化

### 4.11 SVE Reinterpret 内联函数

使用 `svreinterpret` 在 SVE 向量类型之间进行类型双关，而不引发
未定义行为。与 C 强制转换不同，`svreinterpret` 保证定义良好的位级转换。

```c
svint32_t int_v = svdup_s32(0x3F800000);  // 1.0f 的位模式

// ❌ 错误：SVE 类型之间的 C 强制转换是未定义的
svfloat32_t float_v = (svfloat32_t)int_v;  // UB！

// ✅ 正确：使用 svreinterpret
svfloat32_t float_v = svreinterpret_f32_s32(int_v);

// 重载别名也可用（元素数量/类型自动推断）
svfloat32_t float_v = svreinterpret_f32(int_v);
```

**元组类型（SVE2.1）：**
```c
svint32x2_t pair;
svuint16x2_t reinterpreted = svreinterpret_u16_s32_x2(pair);
// 或使用重载别名：
svuint16x2_t reinterpreted = svreinterpret_u16(pair);
```

**常见用例 — 对浮点向量进行位操作：**
```c
// 通过整数操作清除浮点向量的符号位
svfloat32_t abs_f32(svbool_t pg, svfloat32_t v) {
    svint32_t as_int = svreinterpret_s32_f32(v);
    svint32_t mask = svdup_s32(0x7FFFFFFF);
    svint32_t abs_int = svand_s32_x(pg, as_int, mask);
    return svreinterpret_f32_s32(abs_int);
}
```

---
