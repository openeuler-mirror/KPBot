# ARM 向量化规则（SVE2p1 / SME2p2，VL 固定 256 位）

本文只讲"把一段标量 C 改成 AArch64 向量代码"这件事的判断依据。命令、qemu 参数、
验收口径分别在 `toolchain-and-run.md` 和 `verification-gates.md`。

**文中的所有代码片段都在 `aarch64-elf-gcc 16.2.0` +
`-march=armv9.6-a+sve2p1+sme2p2 -msve-vector-bits=256` 下编译通过。** 换工具链版本
或换 `-march` 后必须重新验证：GCC 对 SVE2.1 多向量 intrinsic 的支持在各版本间
差异很大，凭记忆写 intrinsic 名字是最常见的返工来源。

## 0. 先量 R，再选方向

`R` = 最内层**一次能连续读到的字节数**。它是选 SVE 还是 NEON 的唯一依据：

```c
/* 行循环：R = lx * sizeof(elem) */
for (int x = 0; x < lx; x++) dst[x] = f(src[x]);        /* lx=32, elem=u8  -> R=32 */

/* 固定块：R = blockSize * sizeof(pixel) */
template<int log2Size> void planar_pred_c(...)           /* 32x32 u8          -> R=32 */
                                                         /* 16x16 u8          -> R=16 */
                                                         /* 4x4   u8          -> R=4  */

/* 跨步：R 只算连续那一段 */
for (int y = 0; y < h; y++) dst[y * stride + x] = ...;  /* 每次只 1 个元素   -> R=1  */
```

| `R` | 默认方向 | 判断理由 |
| --- | --- | --- |
| ≥ 32 | SVE | 填满 256 位，尾部交给谓词 |
| 17–31 | SVE | 一条谓词化向量覆盖整行；NEON 要拆两段 |
| 9–16 | **NEON** | 一个 128 位寄存器正好装下，不用付谓词 |
| ≤ 8 | NEON，或先把多行打包 | 128 位都填不满，SVE 更浪费 |

`R` 落在 16 附近且数据要扩宽时（8 位输入 → 16 位运算，lane 数减半），扩宽后可能刚好
填满 32 字节，这时 SVE 反而更合适。这种"卡在边界"的情况**两侧都试一次**，别只信表。

### R <= 16：写 NEON 而不是 SVE

这套技能默认讲 SVE，但方向判成 NEON 时不要硬套 256 位向量。NEON 的写法差别集中在
三点：固定 16 字节、没有谓词、尾部要自己收。

```c
#include <arm_neon.h>

void avg16_u8(uint8_t *dst, const uint8_t *a, const uint8_t *b, int n)
{
    int x = 0;
    for (; x + 16 <= n; x += 16) {                  /* 16 字节 = 一个 q 寄存器 */
        uint8x16_t va = vld1q_u8(a + x);
        uint8x16_t vb = vld1q_u8(b + x);
        vst1q_u8(dst + x, vrhaddq_u8(va, vb));      /* (a+b+1)>>1，一条指令 */
    }
    for (; x < n; x++)                              /* NEON 没有谓词，尾部标量收 */
        dst[x] = (uint8_t)(((int)a[x] + b[x] + 1) >> 1);
}
```

NEON 没有 `whilelt`，所以尾部只有两种处理：标量收尾（如上），或者"往回重叠一段"
再写一次（要求 `n >= 16`）。选后者时注意最后一段会重复计算，不能有副作用。

### R >= 17：走 SVE，看下面各节

后面所有内容都按 256 位固定向量长度写。

## 1. 固定 256 位意味着什么

`-msve-vector-bits=256` 把向量长度变成编译期常量：`svcntb()` 等会被直接折叠成常数。
好处是可以按常数展开、去掉运行时分支；代价是**运行时 VL 必须真的是 256 位**，否则
每次 `ld1/st1` 只处理 128 位，结果静默错误。裸机环境要显式写 `ZCR_EL1.LEN`
（见 `assets/bench/README.md`），Linux 用户态要确认内核和 `prctl` 给的就是 256。

| 类型 | 每向量元素数 | 说明 |
| --- | --- | --- |
| `b` 8 位 | 32 | |
| `h` 16 位 | 16 | |
| `s` 32 位 | 8 | 最常见的累加器宽度 |
| `d` 64 位 | 4 | |
| 谓词粒度 `.b` | 32 位掩码 | `svptrue_b8()` 全真 |

常用换算写在编译期即可，不要在热循环里调 `svcntw()`：

```c
svint32_t acc = svdup_s32(0);        /* 清零累加器，避免读未初始化寄存器 */
```

## 2. 可向量化判定（按顺序问，任何一个"否"就停下来）

1. **迭代间有没有真依赖？** 累加、滑动窗口、指针追逐、状态机、`prev = cur` 这类都
   是。前缀和、游程编码、自适应量化这类算法本质上不可并行，勉强向量化只会更慢。
2. **输入输出会不会重叠？** `restrict` 只是给编译器的承诺，不是证明。如果调用方可能
   传同一个缓冲区（原地 in-place），要么改算法（先读后写、临时块），要么放弃。
3. **有没有副作用？** 写全局、调用外部函数、`volatile` 访问、可能 longjmp 的调用。
4. **访存是不是连续？** 跨步访问要用 gather/scatter（`svld1_gather_*`），代价通常
   高于标量循环，除非跨步很小或能先转置。
5. **中间结果会不会溢出？** 这是破坏语义的第一名。8 位逐像素和、16 位系数平方、
   32 位乘 32 位都得先扩宽。改完必须逐元素比对位模式，不能"看起来差不多"。
6. **浮点语义能不能变？** 归约顺序一变，结果就不位精确。原代码没有 `-ffast-math`
   就不要假设可以重排；有严格位精确要求时，只有"每个元素独立计算"的那部分能并行，
   归约必须保持原顺序或用等价的定点实现。
7. **尾部怎么处理？** `n` 一般不是 VL 的整数倍。三种做法：无谓词主体 + 谓词尾、
   全程谓词、重叠载荷（最后一段往前挪一个向量，重复计算）。
8. **数据结构对不对？** AoS 布局（xyzxyz…）几乎一定要先转 SoA 或做 deinterleave；
   转置本身的开销要算进收益。

## 3. 主循环骨架

**首选：无谓词主体 + 谓词尾部。** 主体用 `svptrue_b32()`，编译器能把它提到循环外且
省掉每轮的谓词构造；尾部只跑一次。

```c
const size_t vl = 32 / 4;                 /* 8 个 32 位元素 */
size_t i = 0;
for (; i + vl <= n; i += vl) {
    svint32_t va = svld1_s32(svptrue_b32(), a + i);
    svint32_t vb = svld1_s32(svptrue_b32(), b + i);
    svst1_s32(svptrue_b32(), o + i, svadd_s32_x(svptrue_b32(), va, vb));
}
if (i < n) {
    svbool_t p = svwhilelt_b32_u64(i, n);
    svst1_s32(p, o + i, svadd_s32_x(p, svld1_s32(p, a + i), svld1_s32(p, b + i)));
}
```

**全程谓词**适合短的、块状的函数（4x4、8x8 变换块），代码更短，一轮就结束：

```c
for (size_t i = 0; i < n; i += vl) {
    svbool_t p = svwhilelt_b32_u64(i, n);
    svst1_s32(p, o + i, svadd_s32_x(p, svld1_s32(p, a + i), svld1_s32(p, b + i)));
}
```

**展开 2 或 4 个向量**只有在循环体很小、分支开销占比高时才值得。VL=256 时一个向量的
工作量已经很大（32 字节），盲目展开会顶到寄存器上限并触发溢出。

用 `_u64` 版本的 `svwhilelt` 更稳：`n` 为 0 或很大时不会出现有符号比较的边界问题。

## 4. 常用改写手法

### 扩宽装载 / 窄化存储

8 位输入要进 32 位累加时，直接扩宽装载，省掉一条 `unpk`：

```c
svint32_t va = svld1ub_s32(svptrue_b32(), a + i);   /* u8 -> s32，零扩展 */
```

窄化用 `svqxtnb`（低位一半）+ `svqxtnt`（拼到高半），或 `svqxtunb`（饱和到无符号）：

```c
svint32_t v = svld1_s32(svptrue_b32(), src + i);
svint16_t lo = svqxtnb_s32(v);
svst1_s16(svptrue_b16(), dst + i, lo);
```

宽运算用 `svaddlb_*` / `svmullb_*` / `svmlalb_*`。注意**源类型是窄的那一边**：

```c
svint32_t wide = svaddlb_s32(svint16_t_a, svint16_t_b);        /* s16 + s16 -> s32 */
svint64_t w64  = svmlalb_s64(acc64, svint32_t_a, svint32_t_b); /* s32 * s32 + s64 */
```

照着记忆写成 `svaddlb_s32(svint32_t, svint32_t)` 会编译失败——这类参数类型错误是
probe 阶段最常见的失败原因。

### 去交织（YUV / RGB / 隔点下采样）

```c
svint16_t v0 = svld1_s16(svptrue_b16(), src + i);
svint16_t v1 = svld1_s16(svptrue_b16(), src + i + 16);
svst1_s16(svptrue_b16(), even + i / 2, svuzp1_s16(v0, v1));   /* 偶数下标 */
svst1_s16(svptrue_b16(), odd  + i / 2, svuzp2_s16(v0, v1));   /* 奇数下标 */
```

`svuzp1/2` 是按元素类型交错的，用错类型（`.b` vs `.s`）得到的是完全不同的排列。
`svzip1/2` 是它的逆操作，`svrev` 逆序，`svext` 做滑窗移位。

### 比较与选择

三目运算符逐元素写通常不能向量化，改成掩码 + 选择：

```c
svint32_t v = svld1_s32(p, src + i);
v = svsel_s32(svcmplt_s32(p, v, lo), lo, v);
v = svsel_s32(svcmplt_s32(p, hi, v), hi, v);
```

`svsel_s32(p, a, b)` 在 `p` 为真的 lane 取 `a`，否则取 `b`。

### 归约

`svaddv_s32(p, v)` 是**横向**归约，延迟随 VL 增长。长循环里先把结果纵向累加到向量
寄存器，循环结束再做一次横向归约：

```c
svint32_t acc = svdup_s32(0);
for (; i + vl <= n; i += vl)
    acc = svadd_s32_x(svptrue_b32(), acc, svld1_s32(svptrue_b32(), a + i));
int32_t total = svaddv_s32(svptrue_b32(), acc);
```

千万不要在循环体内每次 `svaddv` 再广播回去——那样比标量还慢。

浮点归约顺序改变就不位精确。需要位精确时，把并行只用在"每元素独立"的部分，归约
要么保持标量顺序，要么在报告里明确标注"结果不再逐位一致"并得到用户确认。

### 点积（SVE2 才有）

```c
acc = svdot_s32(acc, svld1_s8(svptrue_b8(), a + i),
                     svld1_s8(svptrue_b8(), b + i));
```

`svdot_s32` 一次做 4 组 8 位乘加并累加到 32 位，是 SAD/SSD/卷积/量化代价这类算子
的主力。注意它要求两端点积宽度一致（8 位配 8 位、16 位配 16 位），混用会走不通。

### 查表

`svtbl` 的表就是向量寄存器本身，**VL=256 时最多 32 个字节项**。超过这个大小的 LUT
不能靠零扩展解决，要么分块（先判断落在哪一块）、要么用 `svld1_gather`、要么保留
标量。这一步经常被漏掉，导致"向量化后结果对但慢"。

### 位操作

`svclz_u32_x`（前导零）、`svcnt_u32_x`（置位数）、`svlsr_n_u32_x`（立即数移位）、
`sveor3_s32`（SVE2 三输入异或）、`svrax1_u64`（SVE2 循环异或）在哈希、CRC、熵编码
这类代码里比逐元素循环划算得多。这些函数名和参数顺序都要 probe 确认。

## 5. `_x` / `_z` / `_m` 与不活跃 lane

- `_x`（don't care）：不活跃 lane 的值未定义。只在"整条向量随后被同一个谓词存储
  或覆盖"时用，否则会读到脏值。
- `_z`（zero）：不活跃 lane 置零。稍慢，但结果确定。
- `_m`（merging）：不活跃 lane 保留第一个操作数。用于"只更新部分 lane"。

最常见的静默错误：用 `_x` 累加，然后 `svaddv` 把未定义的 lane 也加了进去。要么
保证尾部用 `_z`，要么在横向归约前先把不活跃 lane 清掉。

## 6. 多向量寄存器（SVE2.1）

ACLE 里有 `svint32x2_t` / `x3` / `x4` 类型，但**在本工具链里只提供了构造与通道访问**：

```c
svint32x2_t pair = svcreate2_s32(a, b);
svint32_t  lo   = svget2_s32(pair, 0);
```

`svadd_s32_x2`、`svld1_s32_x2` 这类"多向量运算/装载"在当前 GCC 里没有声明。看到
网上示例用了它们，先 probe 再决定；不要在方案里把它当既有能力。

## 7. SME / ZA 什么时候才用

SME 的 ZA tile 针对的是**矩阵乘型** kernel（外积累加、tile 复用），不是逐元素循环。
把一个逐元素循环改成 SME 通常不会更快，而且引入它的代价很实在：

- 需要 `__arm_streaming` / `__arm_inout("za")` 之类的函数属性，混合 streaming 与
  非 streaming 调用要在边界做 `SMSTART`/`SMSTOP`；
- 需要内核和 ABI 支持 SME 状态保存（用户态可能直接 `SIGILL`）；
- 裸机还要额外配 `SCTLR_EL1.SMEN`、`SMCR_EL1.LEN`。

所以规则是：**项目里已经有 SME 路径、或者这个函数确实是 GEMM/卷积外积结构时才用**，
其余情况留在 SVE。不要为了"用上新特性"而引入一个新的运行时依赖。

## 8. 动手前先 probe

任何 intrinsic，先花 30 秒确认它能编译：

```bash
printf '#include <arm_sve.h>\nvoid f(void){ /* 片段 */ }\n' > /tmp/probe.c
aarch64-elf-gcc -O2 -march=armv9.6-a+sve2p1+sme2p2 \
  -msve-vector-bits=256 -ffreestanding -c /tmp/probe.c -o /tmp/probe.o
```

加上 `-Wall` 能看到隐式声明的警告（那通常意味着名字拼错或该版本不支持）。这一步
比"先写完整实现再修 20 个编译错误"快得多。
