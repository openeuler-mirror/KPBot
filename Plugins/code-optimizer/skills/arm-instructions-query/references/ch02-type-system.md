## 2. 类型系统规则

### 2.1 Neon 定长类型

Neon 类型具有**固定**大小（64 或 128 位），行为与普通 C 类型类似：

```c
#include <arm_neon.h>

int32x4_t a;          // 正确：局部变量
static int32x4_t g;   // 正确：全局变量
sizeof(int32x4_t);    // 正确：返回 16
int32x4_t arr[10];    // 正确：数组元素

struct MyVec {
    int32x4_t v;      // 正确：结构体成员
};
```

用于多向量加载/存储的数组类型：
```c
int32x4x2_t pair;    // struct { int32x4_t val[2]; }
int32x4x3_t triple;  // struct { int32x4_t val[3]; }
int32x4x4_t quad;    // struct { int32x4_t val[4]; }
```

### 2.2 SVE 无大小类型

SVE 类型是**无大小的**——其大小在运行时由向量长度（VL）决定。这是 SVE 长度无关设计的根本。

**可以做的：**
```c
svint32_t v;                    // 局部变量
svint32_t foo(svint32_t a);     // 函数参数/返回值
svint32_t *ptr;                 // 指向 SVE 类型的指针
svbool_t pg = svptrue_b32();   // 谓词变量
```

**不可以做的：**
```c
sizeof(svint32_t);              // 错误：无大小
svint32_t arr[10];              // 错误：数组元素
static svint32_t global_v;     // 错误：静态/线程局部变量
struct S { svint32_t v; };      // 错误：结构体成员
svint32_t *p; p++;             // 错误：指针运算
new svint32_t;                  // 错误：C++ new 表达式
std::vector<svint32_t> vec;    // 错误：容器元素
[=](svint32_t v){ }            // 错误：lambda 按值捕获
```

**运行时向量长度查询：**
```c
uint64_t num_bytes = svcntb();   // 一个 SVE 向量的字节数
uint64_t num_words = svcntw();   // 32 位元素的数量
uint64_t num_dwords = svcntd();  // 64 位元素的数量
```

### 2.3 SVE 固定长度类型（可选扩展）

ACLE 提供了一个**可选**扩展，允许将 SVE 类型固定为已知的向量长度。
这会将无大小类型转换为普通的有大小类型，可以用作全局变量、结构体成员或数组元素。

**可用性检查：**
```c
#if defined(__ARM_FEATURE_SVE_BITS) && __ARM_FEATURE_SVE_BITS > 0
    // 固定长度 SVE 可用 — 向量长度在编译时已知
#endif
```

**声明固定长度 SVE 类型：**
```c
#if __ARM_FEATURE_SVE_BITS == 512
typedef svint32_t vec __attribute__((arm_sve_vector_bits(512)));
typedef svbool_t pred __attribute__((arm_sve_vector_bits(512)));

vec global_v;             // ✅ 正确：普通有大小类型
struct S { vec v; };      // ✅ 正确：结构体成员
vec arr[10];              // ✅ 正确：数组元素
sizeof(vec);              // ✅ 正确：返回 64（512 位）
#endif
```

**规则：**
1. 属性参数 `N` 必须等于 `__ARM_FEATURE_SVE_BITS`
2. 生成的类型**不再是无大小的** — 它是普通的 C/C++ 类型
3. 固定长度 SVE 类型和无大小 SVE 类型是**不同的类型**
4. 仍然可以对固定长度类型使用无大小 SVE 内联函数
5. 这是一个**可选**特性 — 可移植代码不应依赖它

**使用场景 — 存储中间 SVE 结果：**
```c
#if __ARM_FEATURE_SVE_BITS == 256
typedef svfloat32_t fvec __attribute__((arm_sve_vector_bits(256)));

struct Accumulator {
    fvec partial_sums[4];   // 现在可以将 SVE 向量存储在结构体中
};

void accumulate(float *data, int n, struct Accumulator *acc) {
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        // 对固定长度类型使用无大小 SVE 内联函数 — 可行
        acc->partial_sums[0] = svadd_f32_x(pg, acc->partial_sums[0], v);
    }
}
#endif
```

### 2.4 Neon 类型转换规则

ACLE **不**定义不同 Neon 向量类型之间的隐式转换。未经显式转换的类型混用是不可移植的。

**错误：隐式类型转换**
```c
int32x4_t x;
uint32x4_t y = x;          // ❌ 不可移植：隐式转换
float32x4_t z = x;         // ❌ 不可移植：整数到浮点未经内联函数
```

**正确：使用显式内联函数**
```c
int32x4_t x;
// 使用 vreinterpret 重新解释位模式（无数据移动）
uint32x4_t y = vreinterpretq_u32_s32(x);
// 使用 vcvt 进行实际数值转换
float32x4_t z = vcvtq_f32_s32(x);
```

**错误：静态构造向量类型**
```c
int32x4_t x = { 1, 2, 3, 4 };  // ❌ 不可移植
```

**正确：使用构造内联函数**
```c
// 用单个标量值填充
int32x4_t x = vdupq_n_s32(42);

// 通过加载从单独的值创建
static const int32_t init[4] = {1, 2, 3, 4};
int32x4_t x = vld1q_s32(init);

// 或对 64 位向量使用 vcreate_*
int32x2_t x = vcreate_s32(((uint64_t)2 << 32) | 1);
```

**错误：使用 GCC 向量扩展语法**
```c
uint32x2_t x = {0, 1};               // ❌ GCC 扩展，不可移植
uint32_t y = vget_lane_s32(x, 0);    // 行为取决于字节序！
```

**正确：一致使用 ACLE 内联函数**
```c
static const int32_t tmp[2] = {0, 1};
uint32x2_t x = vld1_s32(tmp);        // ✅ 可移植
uint32_t y = vget_lane_s32(x, 0);    // ✅ 确定性行为
```

### 2.5 Neon ↔ SVE 类型转换规则

```c
// Neon → SVE：使用 Bridge 内联函数
int32x4_t neon_v = vdupq_n_s32(42);
svint32_t sve_v = svset_neonq_s32(svundef_s32(), neon_v);  // ✅

// SVE → Neon：使用 Bridge 内联函数
int32x4_t back = svget_neonq_s32(sve_v);                    // ✅

// 绝不在 Neon 和 SVE 类型之间使用 C 强制转换
svint32_t bad = (svint32_t)neon_v;                          // ❌ 未定义
```

---
