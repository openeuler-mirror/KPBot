# 安全边界与常见陷阱

> 用途：interactive-optimizer 在**应用任何优化之前**必须查阅本文件。列出每种策略的拒绝条件、AI 常见错误代码模式、以及标准化安全检查流程。
> 配合 `03-optimization-patterns.md`（策略决策引擎）和 `02-instruction-reference.md`（指令延迟数据）使用。

---

## 1. 通用安全规则

以下规则适用于**所有**优化类型，违反任意一条即终止优化。

### 1.1 语义等价

- 优化后的代码必须与原始代码**逐比特等价**（bit-exact）。浮点操作允许可文档化的精度差异（如归约顺序改变引入 1-2 ULP 偏差），但必须在 commit message 中注明。
- 禁止改变函数的外部可见行为：参数签名、返回值、全局状态修改、文件 I/O、网络调用顺序。

### 1.2 代码修改规则

| 规则 | 说明 |
|------|------|
| **先读后改** | 修改任何文件前必须 `Read` 原文件，获取 `old_string` 精确匹配后再 `Edit`。禁止仅凭记忆或猜测替换。 |
| **单策略单提交** | 一次 commit 只包含一个优化策略的变更。不混合多种策略在同一提交中。 |
| **回归即回滚** | 性能测试结果出现回归（任何衡量指标恶化 >1%），立即 `git stash` 回滚，不作尝试修复。 |

### 1.3 指令查询规则

- **禁止凭记忆猜测**指令存在性、延迟、吞吐量。所有涉及 "指令 X 是否存在"、"指令 Y 延迟多少 cycle"、"A 和 B 哪个更快"的判断，必须实际运行查询脚本。
- **精确名查不到不代表功能不存在**：`arm_query.py instruction --name` 返回 not found 后，必须用 `instruction-search --keyword` 以 ≥3 个不同功能关键词重搜。

### 1.4 Bash 命令模板

**通用指令/Intrinsic 查询**（NEON/SVE/SVE2）：
```bash
cd pipeline/skills/arm-instructions-query
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword "<功能关键词>" --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic --name <intrinsic_name> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic-search --keyword "<关键词>" --family <neon|sve|sve2> --json
```

**微架构延迟/吞吐查询**（TSV110 / 0xd03）：
```bash
cd pipeline/skills/kunpeng_microarch/scripts
python3 query_tsv110.py <mnemonic>     # Kunpeng-0xd01
python3 query_uarch_b.py <mnemonic>      # Kunpeng-0xd03/0xd06 (0xd03/0xd06)
```

---

## 2. 各策略专项安全规则

### 2.1 vectorization（标量→SIMD 向量化）

| 拒绝条件 | 说明 |
|----------|------|
| 循环内有函数调用 / I/O / 原子操作 | 副作用不可向量化 |
| 跨迭代数据依赖（前缀和/递推） | 不可并行化（累加型归约除外，可拆分累加器） |
| 不规则访存（间接索引 `a[idx[i]]`） | 步长不可预测 |
| 不支持的数据类型（如 `double complex`、自定义结构体） | 无对应 ARM SIMD 指令映射 |

**常见 AI 错误代码示例**：

```c
// ❌ 错误 #1：NEON 尾处理缺失 — 当 n 不是 4 的倍数时越界
for (int i = 0; i < n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);       // 越界访问！
    vst1q_s32(&result[i], vaddq_s32(v, b));
}

// ✅ 正确：主循环 + 标量尾循环
int i = 0;
for (; i + 4 <= n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);
    vst1q_s32(&result[i], vaddq_s32(v, b));
}
for (; i < n; i++) { result[i] = a[i] + b_scalar; }
```

```c
// ❌ 错误 #2：NEON lane 索引越界 — vget_low_f32 返回 float32x2_t，不是单标量
float val = vget_low_f32(vec);            // 类型错误！期望标量，实际是 2-lane 向量

// ✅ 正确：用 vgetq_lane 提取指定 lane
float val = vgetq_lane_f32(vec, 0);       // 提取 lane 0
```

```c
// ❌ 错误 #3：假设固定对齐 — 未检查指针是否 16 字节对齐
int32x4_t v = vld1q_s32(&a[i]);           // a 可能非 16 字节对齐

// ✅ 正确：检查对齐或使用 unaligned 变体
int32x4_t v = vld1q_s32((int32_t*)__builtin_assume_aligned(&a[i], 16));
```

### 2.2 throughput-enhancement（循环展开）

| 拒绝条件 | 说明 |
|----------|------|
| 展开后寄存器预算 > 24 个（NEON v0-v31 共 32，保留 8 给编译器/spill） | 必然溢出到栈，收益为负 |
| 展开后循环体 > L1I 容量安全上限（64KB L1I × 0.125 = 8KB） | 代码膨胀驱逐热数据 |
| trip count 非展开因子整数倍且未添加 epilogue | 漏处理尾部元素 |
| 累加器拆分后循环结束时忘记合并 | 结果错误（仅最后一个累加器的部分和） |

**常见 AI 错误代码示例**：

```c
// ❌ 错误：拆分累加器但忘记合并
float sum0 = 0, sum1 = 0;
for (int i = 0; i + 8 <= n; i += 8) {
    sum0 += a[i]   + a[i+1] + a[i+2] + a[i+3];
    sum1 += a[i+4] + a[i+5] + a[i+6] + a[i+7];
}
return sum0;           // sum1 丢失！应为 return sum0 + sum1;

// ✅ 正确
return sum0 + sum1;
```

### 2.3 branch-elimination（分支消除）

| 拒绝条件 | 说明 |
|----------|------|
| 分支体涉及函数调用 | 副作用不可消除 |
| 分支体 > 5-6 行代码 | 条件选择不如分支预测高效 |
| 分支高度可预测（如循环不变条件、`i%2` 模式） | CPU 预测器已消除开销，优化无收益 |
| 值域非连续（switch 大范围跳转） | 条件选择链开销超过分支预测失败代价 |

**常见 AI 错误代码示例**：

```c
// ❌ 错误：csel 条件码反转 — 意图是 if(cond) x=a else x=b，但写反了
// 原始逻辑：if (x > 0) y = a; else y = b;
__asm__("cmp %1, #0; csel %0, %2, %3, le"  // LE 条件是 ≤0，但 C 逻辑是 >0 ！
        : "=r"(y) : "r"(x), "r"(a), "r"(b));

// ✅ 正确：csel 的最后一个操作数（cond）直接对应原始条件
__asm__("cmp %1, #0; csel %0, %2, %3, gt"  // GT = >0, 正确
        : "=r"(y) : "r"(x), "r"(a), "r"(b));
```

```c
// ❌ 错误：vbslq 使用非全 0/1 掩码 — vbslq 是位选择，不是值选择
float32x4_t mask = vcltq_f32(a, b);  // 返回 0xFFFFFFFF / 0x00000000 ✓
float32x4_t wrong_mask = vcvtq_f32_u32(vcltq_f32...(a, b)); // ✗ 破坏了位模式

// ✅ 正确：直接用比较结果作为 vbslq 掩码
float32x4_t mask = vcltq_f32(a, b);
float32x4_t result = vbslq_f32(mask, val_true, val_false);
```

### 2.4 prefetch-optimization（软件预取）

| 拒绝条件 | 说明 |
|----------|------|
| 不规则访存模式（间接索引、链表遍历） | 预取地址不可预测 |
| 工作集 < L1d 大小 | 数据已缓存，预取增加指令开销无收益 |
| 距离过大（C/C++: >32 元素，汇编: >256 字节偏移） | 驱逐正在使用的缓存行 |
| 距离过小（C/C++: <4 元素，汇编: <64 字节偏移） | 不等数据到达就已使用 |

**常见 AI 错误代码示例**：

```c
// ❌ 错误 #1：未做循环条件拆分 — 数组尾部预取地址越界
for (int i = 0; i < n; i++) {
    __builtin_prefetch(&a[i + 16], 0, 3);
    sum += a[i];              // 当 i > n-17 时预取越界（不崩溃但浪费带宽）
}

// ✅ 正确：拆分为有预取主循环 + 无预取尾循环
for (int i = 0; i + 16 < n; i++) {
    __builtin_prefetch(&a[i + 16], 0, 3);
    sum += a[i];
}
for (int i = n > 16 ? n - 16 : 0; i < n; i++) { sum += a[i]; }
```

```asm
// ❌ 错误 #2：PRFM 参数错误 — 类型和策略混用
prfm pldl1strm, [x0, #64]     // "stream" 用于 L1 但 stream 暗示无重用 → 矛盾

// ✅ 正确：根据数据重用模式选正确的 PRFM 类型
prfm pldl2keep, [x0, #64]     // L2 缓存，保持（有重用）
prfm pldl1keep, [x0, #64]     // L1 缓存，保持（高重用）
prfm pldl2strm, [x0, #64]     // L2 缓存，流式（无重用，避免 L1 污染）
```

### 2.5 memory-access-optimization（访存模式优化）

| 拒绝条件 | 说明 |
|----------|------|
| AoS→SoA 且 `risk_tolerance == "safe"` | 接口变更高风险，safe 模式禁止 |
| AoS→SoA 且结构体被外部模块直接访问 | 需全局改动，超出优化范围 |
| 结构体字段重排且存在 `offsetof()` 依赖或序列化协议 | 破坏 ABI / 二进制兼容 |
| 循环重排且代码依赖浮点归约顺序 | 结合律不保证，结果可能漂移 |

**常见 AI 错误代码示例**：

```c
// ❌ 错误：结构体字段重排后未更新所有引用点
// 原始：struct { float x, y, z, w; int flag; float padding[3]; };
// 优化后：struct { float x, y, z, w, padding[3]; int flag; }; // flag 移动到末尾
sizeof(struct Particle)  // 仍然返回原值？未验证！
offsetof(struct Particle, flag)  // 所有使用 offsetof 的序列化代码破裂

// ✅ 正确：重排前 grep 所有 offsetof/sizeof/&p->field 引用点，逐一验证
```

```c
// ❌ 错误：分配点对齐不足，仅改结构体定义加 aligned 属性
struct __attribute__((aligned(64))) Vec { float x, y, z; };
Vec* v = (Vec*)malloc(sizeof(Vec));  // malloc 不保证 64 字节对齐！

// ✅ 正确：分配点和使用点都要对齐
Vec* v = (Vec*)aligned_alloc(64, sizeof(Vec));
```

### 2.6 compiler-flag-tuning（编译选项调优）

| 拒绝条件 | 说明 |
|----------|------|
| **CRITICAL: `-mcpu=tsv110` 用于 Kunpeng-0xd03/0xd06** | 生成错误调度代码，**性能必定下降** |
| 标准 Kunpeng-0xd01 + `+sve` 且 CPU 不支持 SVE | **SIGILL** 非法指令崩溃 |
| 未知 CPU 型号使用平台特定 `-mcpu` | 用 `-mcpu=native` |
| `-ffast-math` 且代码依赖 IEEE 754 严格合规（如金融/科学计算） | 次正规数 flush-to-zero、不设 errno |

**CPU→Flag 正确映射速查**：

| CPU Part ID | 型号 | 正确 `-mcpu` | 错误用法（禁止） |
|-------------|------|-------------|-----------------|
| `0xd01` | 0xd01 | `-mcpu=tsv110` | `-mcpu=native` 也可但非最优 |
| `0xd01` 高配 | 0xd01 高配 | `-mcpu=tsv110+sve` | 不带 `+sve` 则 SVE 不能用 |
| `0xd03` | 0xd03 | `-mcpu=native` | **严禁 `-mcpu=tsv110`** |
| `0xd06` | 0xd06 | `-mcpu=native` | **严禁 `-mcpu=tsv110`** |
| 未知型号 | — | `-mcpu=native` | 严禁假设是 0xd01 |

**常见 AI 错误**：

```bash
# ❌ 错误：在 Kunpeng-0xd03 上使用 tsv110（最危险错误）
CFLAGS="-O3 -mcpu=tsv110 -march=armv8.2-a"  # 调度完全错误，性能下降 20-50%

# ❌ 错误：在标准 0xd01 上使用 +sve（CPU 无 SVE → SIGILL）
CFLAGS="-O3 -mcpu=tsv110+sve"  # 仅当 /proc/cpuinfo 确认 SVE 存在时可用

# ✅ 正确：先检测 CPU Part ID
CPU_PART=$(grep -m1 'CPU part' /proc/cpuinfo | awk '{print $NF}')
case "$CPU_PART" in
    0xd01) MCPU="-mcpu=tsv110" ;;
    0xd03|0xd06) MCPU="-mcpu=native" ;;
    *) MCPU="-mcpu=native" ;;
esac
```

### 2.7 asm-optimization（汇编指令级优化）

| 拒绝条件 | 说明 |
|----------|------|
| LDP/STP offset 超出范围（[-512, 504] 字节，8 字节对齐） | 编码不支持，汇编报错 |
| LDP/STP 目标寄存器相同（如 `ldp x0, x0, [sp]`） | 未定义行为 |
| 后索引寻址修改了 base register 后又作为源操作数 | 破坏了依赖链 |
| Macro fusion 候选对（`subs`/`cmp` + `b.cond`）之间隔了其他指令 | 宏融合失效 |
| 修改 callee-saved 寄存器（x19-x29）的 save/restore 序列 | 破坏调用约定 |

**常见 AI 错误代码示例**：

```asm
// ❌ 错误 #1：LDP/STP offset 超出编码范围
ldp x0, x1, [sp, #600]        // offset 600 > 504，汇编报错
stp x2, x3, [sp, #-600]       // offset -600 < -512，汇编报错

// ✅ 正确：先计算地址再加载
add x10, sp, #600
ldp x0, x1, [x10]
```

```asm
// ❌ 错误 #2：后索引寻址 base register 被后续指令依赖
ldr x0, [x1, #8]!             // x1 ← x1 + 8
mov x2, x1                    // 需要原始 x1 还是更新后的 x1？含义不清

// ✅ 正确：如果后续需要原始值，改用 offset 寻址
ldr x0, [x1, #8]              // x1 不变
mov x2, x1                    // 获取原始 x1，语义明确
add x1, x1, #8                // 显式更新
```

```asm
// ❌ 错误 #3：破坏宏融合 — cmp 和 b.cond 之间插入指令
subs x0, x0, #1
mov x3, x2                    // 插入的 mov 破坏了宏融合
b.ne loop_top                 // sub + b.ne 不再融合

// ✅ 正确：subs/cmp 与条件分支相邻
subs x0, x0, #1
b.ne loop_top
mov x3, x2                    // mov 移到分支之后
```

### 2.8 scalar-vector-hybrid（标矢量混合）

| 拒绝条件 | 说明 |
|----------|------|
| 串行依赖链包含 TBL/AESE/PMULL/SHA256 等专用 NEON 指令 | 这些指令没有标量等价形式，不可标量化 |
| 依赖链 < 3 条指令 | `fmov` 搬移开销无法摊薄，净收益 ≤0 |
| ALU 管线利用率也高 | 标量搬移到 ALU 后 ALU 本身成为瓶颈，总吞吐无变化 |

**不可标量化的 NEON 指令（硬性边界）**：

| 指令 | 原因 |
|------|------|
| `TBL`/`TBX` | 查表操作，标量需 ≥5 条指令模拟 |
| `AESE`/`AESD`/`AESMC`/`AESIMC` | AES 硬件加速，无可替代 |
| `PMULL`/`PMULL2` | 多项式乘法，标量模拟代价极高 |
| `SHA256H`/`SHA256SU1` 等 | 加密哈希硬件加速 |

**常见 AI 错误代码示例**：

```c
// ❌ 错误：fmov 只搬移低 64 位，高 64 位丢失
uint64x2_t v = vaddq_u64(a, b);       // 128 位结果
uint64_t scalar = vgetq_lane_u64(v, 0); // 只取低 64 位，高 64 位丢失

// ✅ 正确：分别处理低/高 64 位
uint64_t lo = vgetq_lane_u64(v, 0);
uint64_t hi = vgetq_lane_u64(v, 1);
```

---

## 3. 通用 AI 编码错误

以下错误模式来自 `arm-instructions-query` 的实战积累，在 SVE/NEON 代码生成中反复出现。每次生成 SIMD 代码后必须逐项检查。

### 3.1 SVE 类型错误

```c
// ❌ 错误：SVE 类型用 sizeof — SVE 类型无固定大小，sizeof 在编译期返回 0 或报错
size_t sz = sizeof(svint32_t);

// ✅ 正确：运行时查询
uint64_t num = svcntw();           // 32 位元素的数量
uint64_t sz  = num * sizeof(int32_t);
```

```c
// ❌ 错误：SVE 全局/静态变量或数组 — SVE 类型仅限自动存储期（局部变量）
svfloat32_t buffer[100];           // 编译错误
static svint32_t saved;            // 编译错误

// ✅ 正确：使用标准数组配合 SVE 加载/存储
float buffer[100 * 16];            // 为最大 VL 超量分配
svfloat32_t v = svld1_f32(pg, buffer);
```

```c
// ❌ 错误：SVE 作为结构体成员 — SVE 向量仅限局部变量
struct Context { svint32_t acc; };

// ✅ 正确：使用指针指向堆/栈缓冲区
struct Context { float *acc; };
// 线程函数中：svint32_t v = svld1(pg, ctx->acc);
```

```c
// ❌ 错误：NEON ↔ SVE 之间直接强制转换
svint32_t sve_v = (svint32_t)neon_v;  // 类型不兼容

// ✅ 正确：使用桥接 intrinsic
svint32_t sve_v = svset_neonq_s32(svundef_s32(), neon_v);
```

### 3.2 SVE 谓词错误

```c
// ❌ 错误 #1：未初始化的谓词 — 内存随机值，行为未定义
svbool_t pg;
svint32_t v = svld1(pg, ptr);

// ✅ 正确：显式初始化
svbool_t pg = svptrue_b32();                  // 全激活
svbool_t pg = svwhilelt_b32(i, n);            // 尾循环谓词
```

```c
// ❌ 错误 #2：谓词宽度不匹配 — b8 谓词用于 32 位操作，仅每第 4 个元素激活
svbool_t pg = svptrue_b8();
svint32_t v = svadd_s32_m(pg, a, b);  // 3/4 的元素被非活跃处理！

// ✅ 正确：谓词宽度与元素大小匹配
svbool_t pg = svptrue_b32();          // 32 位元素用 b32 谓词
svint32_t v = svadd_s32_m(pg, a, b);

// 宽度对应规则：b8→int8, b16→int16/float16/bf16, b32→int32/float32, b64→int64/float64
```

```c
// ❌ 错误 #3：_z / _m 混淆 — _m 忘记提供合并源
svint32_t r = svadd_s32_m(pg, a, b);   // _m 的非活跃元素从第一参数复制，这里 'a' 同时是操作数和合并源

// _z（zero）：非活跃元素 = 0，用于初始归约
svint32_t r = svadd_s32_z(pg, a, b);

// _m（merge）：非活跃元素 = 第一参数，用于保持前序计算结果
svint32_t r = svadd_s32_m(pg, merge_src, a, b);

// _x（don't care）：非活跃元素未定义，性能最佳但仅当非活跃元素不被后续使用时安全
svint32_t r = svadd_s32_x(pg, a, b);
```

### 3.3 SVE 循环结构错误

```c
// ❌ 错误：固定步长 — 仅当 VL=128 时正确，VL=256/512/1024 时会漏元素
for (int i = 0; i < n; i += 4) { ... }

// ✅ 正确：运行时步长
for (int i = 0; i < n; i += svcntw()) { ... }   // 32 位元素
for (int i = 0; i < n; i += svcntd()) { ... }   // 64 位元素
for (int i = 0; i < n; i += svcntb()) { ... }   // 8 位元素
```

### 3.4 NEON 尾处理遗漏

这是最常见的 AI 错误之一。任何 NEON/SVE 向量化循环**必须**包含尾处理逻辑。

```c
// ❌ 错误：无尾处理 — n 非 4 的倍数时最后 ≤3 个元素越界访问
for (int i = 0; i < n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);
    vst1q_s32(&result[i], vaddq_s32(v, b));
}

// ✅ 正确模式：主循环 + 标量尾循环
int i = 0;
for (; i + 4 <= n; i += 4) {
    int32x4_t v = vld1q_s32(&a[i]);
    vst1q_s32(&result[i], vaddq_s32(v, b));
}
for (; i < n; i++) {
    result[i] = a[i] + b_scalar;
}
```

### 3.5 特性检测错误

```c
// ❌ 错误：__ARM_FEATURE_SVE 可能定义为 0，#ifdef 仍然为真
#ifdef __ARM_FEATURE_SVE
// SVE 代码在此 — 即使值为 0 也会被编译
#endif

// ✅ 正确：同时检查定义和值
#if defined(__ARM_FEATURE_SVE) && __ARM_FEATURE_SVE
// SVE 代码在此
#endif
```

---

## 4. 优化前自检 Checklist

每次应用优化前，代理必须完成以下检查，逐项打勾确认：

### 阶段 1：理解代码

- [ ] `Read` 目标源文件，确认修改区域的精确代码（行号和内容）
- [ ] 确认文件类型（.c/.cpp/.h vs .s/.S），选择正确的策略和工具
- [ ] 确认目标 CPU Part ID（`grep 'CPU part' /proc/cpuinfo`），选择正确的 ISA 和 `-mcpu`

### 阶段 2：查询指令（如涉及 SIMD/汇编）

- [ ] 所有使用的 intrinsic/指令已通过 `arm_query.py` 验证存在性
- [ ] 关键指令的延迟和吞吐已通过 `query_tsv110.py` 或 `query_uarch_b.py` 查询
- [ ] 寄存器预算已计算（NEON: ≤24 安全，32 硬上限；通用: x0-x17 caller-saved）

### 阶段 3：检查约束

- [ ] 对应策略的**拒绝条件**已逐条核对，未命中任何一条
- [ ] 对应策略的**常见 AI 错误**已逐条对比，确认未复现
- [ ] SVE 代码额外检查：sizeof/全局变量/结构体成员/NEON↔SVE 强制转换 4 项
- [ ] 谓词额外检查：初始化/宽度匹配/_z vs _m vs _x 语义
- [ ] NEON 代码额外检查：尾处理已实现

### 阶段 4：编译验证

- [ ] `make` 或等价命令成功，零 error、零 warning（或 warning 与优化前一致）
- [ ] 确保未引入未定义符号、未匹配的 clobber

### 阶段 5：功能测试

- [ ] 运行项目自带的功能测试/回归测试，全部通过
- [ ] 如无自带测试，至少用一组代表性输入对比优化前后输出，确认 bit-exact

### 阶段 6：性能测量

- [ ] 用 `perf stat` 或项目自带 benchmark 对比优化前后指标
- [ ] 确认关键指标改善（cycles、instructions、IPC、cache-misses 等）
- [ ] 如任一指标恶化 >1%，立即回滚

---

> **核心原则**：安全优先于性能。宁可漏过一个优化机会，也不可引入一个错误。当不确定某项优化是否安全时，默认跳过，记录到 skipped_points 中。
