## 3. 常见 AI 错误

### 3.1 类型错误

**错误：对 SVE 类型使用 sizeof**
```c
// ❌ 错误：SVE 类型是无大小的
size_t sz = sizeof(svint32_t);

// ✅ 正确：使用运行时查询
uint64_t num_elems = svcntw();  // 32 位元素的数量
uint64_t sz = num_elems * sizeof(int32_t);
```

**错误：SVE 数组或全局变量**
```c
// ❌ 错误
svfloat32_t buffer[100];
static svint32_t saved;

// ✅ 正确：使用标准数组配合 SVE 加载/存储
float buffer[100 * 16];  // 为最大 VL 超量分配
svfloat32_t v = svld1_f32(pg, buffer);
```

**错误：SVE 结构体成员**
```c
// ❌ 错误
struct Context {
    svint32_t accumulator;
};

// ✅ 正确：SVE 向量仅作为局部变量
struct Context {
    float *accumulator;  // 指向堆/栈缓冲区的指针
};
// 使用 svld1/svst1 进行数据进出
```

**错误：在 Neon 和 SVE 之间强制转换**
```c
// ❌ 错误
int32x4_t neon_v = ...;
svint32_t sve_v = (svint32_t)neon_v;

// ✅ 正确
svint32_t sve_v = svset_neonq_s32(svundef_s32(), neon_v);
```

### 3.2 谓词错误

**错误：未初始化的谓词**
```c
// ❌ 错误：谓词未初始化
svbool_t pg;
svint32_t v = svld1(pg, ptr);  // 未定义行为

// ✅ 正确：初始化谓词
svbool_t pg = svptrue_b32();  // 所有元素激活
// 或
svbool_t pg = svwhilelt_b32(i, n);  // 元素 i..n-1 激活
```

**错误：谓词宽度不匹配**
```c
// ❌ 错误：对 32 位操作使用字节谓词
svbool_t pg = svptrue_b8();     // 8 位粒度
svint32_t v = svadd_s32_m(pg, a, b);  // 只有每第 4 个元素被激活！

// ✅ 正确：将谓词宽度与元素大小匹配
svbool_t pg = svptrue_b32();    // 32 位粒度
svint32_t v = svadd_s32_m(pg, a, b);  // 所有元素激活
```

**错误：混淆谓词模式**
```c
// _z（清零）：非活跃元素设为零
svint32_t r = svadd_s32_z(pg, a, b);  // 非活跃 = 0

// _m（合并）：非活跃元素从第一个参数复制
svint32_t r = svadd_s32_m(pg, a, a, b);  // 非活跃 = a[i]

// _x（随意）：非活跃元素未定义（性能最佳）
svint32_t r = svadd_s32_x(pg, a, b);  // 非活跃 = 未定义

// ❌ 错误：使用 _m 但忘记合并源
svint32_t r = svadd_s32_m(pg, a, b);  // 'a' 同时作为合并源和操作数

// ✅ 需要不同合并源时：
svint32_t r = svadd_s32_m(pg, merge_val, a, b);  // merge_val 用于非活跃元素
```

### 3.3 特性检测错误

**错误：使用 #ifdef 而非 #if**
```c
// ❌ 错误：__ARM_FEATURE_SVE 可能被定义为 0
#ifdef __ARM_FEATURE_SVE
    // SVE 代码在此 — 即使 SVE 被禁用也会编译！
#endif

// ✅ 正确
#if defined(__ARM_FEATURE_SVE) && __ARM_FEATURE_SVE
    // SVE 代码在此
#endif
// 或简化写法：
#ifdef __ARM_FEATURE_SVE
    // 对大多数编译器（GCC/Clang 仅在可用时定义）这样即可
#endif
```

**错误：假设固定向量长度**
```c
// ❌ 错误：为 128 位 SVE 硬编码 VL=4
for (int i = 0; i < n; i += 4) {
    svbool_t pg = svptrue_b32();
    svfloat32_t v = svld1(pg, &a[i]);
    // 错误：当 VL > 128 位时会漏掉元素！
}

// ✅ 正确：使用 svcntw() 作为步长，使用 svwhilelt 处理尾部
for (int i = 0; i < n; i += svcntw()) {
    svbool_t pg = svwhilelt_b32(i, n);
    svfloat32_t v = svld1(pg, &a[i]);
    svst1(pg, &a[i], result);
}
```

### 3.4 循环结构错误

**错误：SVE 循环中步长错误**
```c
// ❌ 错误：固定步长
for (int i = 0; i < n; i += 4) { ... }     // 仅当 VL=128 时正确
for (int i = 0; i < n; i += 8) { ... }     // 仅当 VL=256 时正确

// ✅ 正确：运行时步长
for (int i = 0; i < n; i += svcntw()) { ... }  // 32 位元素
for (int i = 0; i < n; i += svcntd()) { ... }  // 64 位元素
for (int i = 0; i < n; i += svcntb()) { ... }  // 8 位元素
```

**错误：Neon 循环中遗漏尾部处理**
```c
// ❌ 错误：忽略无法填满一个完整向量的元素
for (int i = 0; i < n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);  // 当 n 不是 4 的倍数时越界！
    vst1q_s32(&result[i], vaddq_s32(v, b_vec));
}

// ✅ 正确：处理尾部元素
int i = 0;
for (; i + 4 <= n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);
    vst1q_s32(&result[i], vaddq_s32(v, b_vec));
}
for (; i < n; i++) {
    result[i] = a[i] + b_scalar;
}
```

### 3.5 FMV（函数多版本化）错误

**错误：缺少默认版本**
```c
// ❌ 错误：没有默认版本 — 链接错误
int __attribute__((target_version("sve"))) compute(int n);
int __attribute__((target_version("simd"))) compute(int n);
// 缺少：默认版本！

// ✅ 正确：始终提供默认版本
int __attribute__((target_version("default"))) compute(int n) {
    return scalar_compute(n);
}
int __attribute__((target_version("sve"))) compute(int n) {
    return sve_compute(n);
}
```

**错误：调用版本化函数时默认声明不可见**
```c
// ❌ 错误：caller.c 看不到默认声明
int result = compute(100);  // 错误或错误的分发

// ✅ 正确：默认声明必须在调用点可见
int compute(int n);  // 默认版本声明可见
int result = compute(100);  // 运行时自动分发到最佳版本
```

---
