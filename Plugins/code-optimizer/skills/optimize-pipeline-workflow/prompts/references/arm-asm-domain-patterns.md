# ARM64 汇编领域知识参考

本文件供 `perspective-asm`（汇编指令级分析师）检测时 Read 引用。不包含检测方法（方法在视角文件中），只提供**领域特征指令、专用替换规则、跨 lane/归约模式、内存层级模式**。

---

## 一、领域分类（多标签软匹配）

**分类原则**：一个函数可以同时匹配多个领域。扫描函数体内所有助记符，对每个领域统计特征指令命中数，命中 ≥ 2 个即标记该领域匹配。匹配后的领域触发对应的专项检查。

### 1. 🖼️ 媒体编解码（media_codec）

**特征指令**：
```
SMLAL, UMLAL, SMLSL, UMLSL       — 加宽乘累加（FIR、IDCT、运动补偿）
SQRDMULH, SQDMULH, SQRDMLAH      — 定点饱和乘（音频 DSP）
TBL, TBX                          — 向量查表（色彩空间转换、像素重排）
SADDLP, UADDLP                   — 成对加宽求和（SAD/SATD）
UABD, SABD, UABAL, SABAL         — 绝对差/累加（运动估计）
EXT                               — 向量拼接位移（滑动窗口）
ZIP1, ZIP2, UZP1, UZP2, TRN1, TRN2 — 交织/解交织（YUV↔RGB）
URHADD, UHADD                     — 带舍入平均（半像素插值）
FMLA (密集)                       — 浮点乘加（音频 IMDCT）
```
若检测到 `UABAL`/`SABAL` + 循环累加 → 标记**运动估计 SAD**子模式。
若检测到 `TBL` + 连续 `ld1` → 标记**像素重排**子模式。

### 2. 🔐 加解密（crypto）

**特征指令**：
```
AESE, AESD, AESMC, AESIMC          — AES 加密/解密
PMULL, PMULL2                      — 无进位多项式乘（GCM/CRC/GHASH）
SHA1C, SHA1P, SHA1M, SHA1H, SHA1SU0, SHA1SU1 — SHA-1
SHA256H, SHA256H2, SHA256SU0, SHA256SU1       — SHA-256
SHA512H, SHA512H2, SHA512SU0, SHA512SU1       — SHA-512
SM4E, SM4EKEY                       — 国密 SM4
EOR3, BCAX, RAX1, XAR              — SHA-3/Keccak/ChaCha
```
若检测到 `AESE`/`AESMC` 成对出现但未多块交织 → 标记**AES 流水线未填满**。
若检测到 `PMULL` 独立使用（非 AES-GCM 场景）→ 检查是否为高速 CRC。

### 3. 🧮 高性能计算（hpc）

**特征指令**：
```
FMLA, FMLS (密集, ≥4 条/循环)      — 融合乘加（GEMM 核心）
FADDV, FMAXV, FMINV                — 向量内归约
FCMLA, FCADD                        — 复数浮点（FFT/信号处理）
WHILELT, WHILELO, WHILELS, WHILEHI  — SVE 谓词生成
GLD1, SST1 (gather/scatter)        — 稀疏矩阵
INCT, DECP, INDEX                   — SVE 循环索引
```
若检测到 SVE `WHILELT` + `fmla` + `incw` + `b.first` 模式 → 标记**SVE 谓词循环**（已充分利用 SVE）。
若检测到 `FMLA` 密集但无 SVE 指令 → 标记**NEON→SVE 候选**（若平台支持 SVE）。

### 4. 💾 存储 CRC 与纠删码（storage_crc_ec）

**特征指令**：
```
CRC32B, CRC32H, CRC32W, CRC32X      — CRC 校验（0x04C11DB7）
CRC32CB, CRC32CH, CRC32CW, CRC32CX  — CRC Castagnoli（0x1EDC6F41）
PMULL, PMULL2                        — Galois Field 乘法（GF 折叠）
TBL + EOR (组合)                     — 查表法 GF 乘法（ISA-L 模式）
```
若检测到 `CRC32CX` 但单条/未展开 → 标记**CRC 展开不足**。
若检测到 `PMULL` + `TBL` + `EOR` 密集组合 → 标记**Reed-Solomon EC 模式**。

### 5. 📄 JSON 解析/正则/文本（text_processing）

**特征指令**：
```
CMEQ, CMHI, CMGE, CMTST             — 向量比较（分类/定位）
TBL, TBX                             — 查表字符分类
SHRN, SHRN2                          — 右移窄化（模拟 movemask）
ADDV, UMAXV, SMAXV                   — 向量内归约
CLZ, RBIT, CTZ                       — 位扫描（找下一个字符）
AND, ORR, EOR (位掩码密集)            — 结构字符处理
```
若检测到 `CMEQ` + `SHRN` + `AND` 流水线模式 → 标记**simdjson movemask 替代**（无需优化，已是最佳实践）。
若检测到逐字节 `ldrb` + `cmp` 做字符分类 → 标记**标量字符分类，应向量化**。

### 6. 🧬 基因组学（bioinformatics）

**特征指令**：
```
SMAX, SMIN, UMAX, UMIN              — 向量极值（DP 打分）
SQADD, SQSUB                         — 饱和加减（防止溢出）
CMEQ + TBL                           — 碱基匹配
UADDLV, SADDLV, ADDV                — 归约（k-mer 计数）
```
若检测到 `SMAX`/`SMIN` + `CMEQ` + 循环 → 标记**Smith-Waterman DP 模式**。

### 7. 🤖 机器学习推理（ml_inference）

**特征指令**：
```
SDOT, UDOT                          — INT8 点积（量化推理）
SMMLA, UMMLA                        — INT8 矩阵乘（i8mm）
BFMMLA, BFDOT                       — BFloat16 矩阵乘/点积
FMLA (FP16)                         — 半精度乘加
FJCVTZS                             — FP→INT 量化转换
FMLA (密集, GEMM 模式)              — FP32 推理
```
若检测到 `SDOT`/`UDOT` 但仅 1 个累加器 → 标记**SDOT 累加器未展开，可拆分 N 路**。
若检测到 `BFMMLA` → 标记**BF16 推理**（通常已高度优化）。

### 8. 🌐 网络处理（networking）

**特征指令**：
```
CRC32C*                              — 流哈希
REV, REV16, REV32, REV64             — 字节序转换
TBL + CMEQ                           — 包头字段查表分类
LDADD, SWP, CAS, STADD (LSE)         — 无锁队列/计数
PRFM                                 — 预取包描述符
SVE2 MATCH, NMATCH                   — 深度包检测字符集
```
若检测到 `REV16` + `REV32` 密集 → 标记**网络序转换**。
若检测到 `LDXR`/`STXR` 循环（重试自旋锁）→ 标记**应升级为 LSE 原子指令**（若 ARMv8.1+）。

---

## 二、专用指令替换规则

以下规则检测**多条通用指令序列 → 单条专用指令**的替换机会。规则格式：`源模式 → 目标指令 | 替换条件 | 领域标签`。

### 媒体/信号处理

| # | 源序列 | 目标 | 条件 |
|---|--------|------|------|
| M1 | `uabd + add/uaddl` 累加 | → `uabal`/`sabal` | 绝对差 → 累加链，中间结果仅用于累加 |
| M2 | `smull/umull + add` 乘加 | → `smlal`/`umlal`/`smlsl`/`umlsl` | 乘法→累加链，乘累加在同一个寄存器上 |
| M3 | `sqdmulh + add/sub` | → `sqrdmlah`/`sqrdmlsh` | 定点乘后立即加减 |
| M4 | 标量查表：循环内 `ldrb + index` | → `TBL`/`TBX` (16 字节并行) | 查表索引可向量化 |
| M5 | `add/shr + mask` 平均 | → `urhadd`/`uhadd` | 无符号字节/半字，带舍入 |
| M6 | `zip/uzp/trn` 逐元素操作 | → 识别现有模式是否可用 `LD2`/`LD3`/`LD4` 结构加载 | 输入来自连续内存 |

### 加解密

| # | 源序列 | 目标 | 条件 |
|---|--------|------|------|
| C1 | 多个 `eor + eor` (三路异或) | → `EOR3` | ARMv8.2+ SHA3，中间 XOR 结果仅在此处使用 |
| C2 | `bic + eor` (位选择+异或) | → `BCAX` | ARMv8.2+ SHA3 |
| C3 | 单块 AES (1×AESE+1×AESMC/轮) | → **多块交织**（4-8 块并行） | AES-CTR/GCM 模式支持并行块，CPU 有独立 AES 流水线 |

### 原子操作

| # | 源序列 | 目标 | 条件 |
|---|--------|------|------|
| L1 | `retry: ldaxr + ... + stlxr + cbnz retry` | → `LDADD`/`SWP`/`CAS`/`STADD` (LSE) | ARMv8.1+，高竞争场景 |

### 跨领域通用

| # | 源序列 | 目标 | 条件 |
|---|--------|------|------|
| G1 | 逐字节清零循环 | → `DC ZVA` (按 cache line 清零) | memset(0), 长度 ≥ 64 字节 |
| G2 | `ldp + stp` 流式拷贝（数据仅用一次） | → `ldnp`/`stnp` (non-temporal) | 避免缓存污染，大块数据 |
| G3 | 逐元素横向归约循环 | → `ADDV`/`FADDV`/`UADDLV`/`SMAXV`/`UMINV` | 归约操作 ≥ 4 元素 |

---

## 三、SIMD 跨 Lane / 数据重排模式

### 3.1 x86 PMOVMSKB 替代模式（ARM 专用技巧）

x86 有 `PMOVMSKB` 把 16 字节向量比较结果提取为 16-bit 掩码，ARM 没有直接等价物。以下为已证明的替代方案：

| 方案 | 实现 | 延迟 | 适用场景 |
|------|------|------|---------|
| SHRN 折叠 | `SHRN #4` + `and` 逐级压缩 | ~5c | 128-bit NEON（simdjson 的方案） |
| ADDV 归约 | `ADDV` + `umov` 提取 | ~8c | 少量提取，简洁 |
| SVE WHILELO | `WHILELO` + `LASTB` 谓词提取 | ~3c | SVE 平台最优 |

**检测**：若代码中有 `CMEQ` + 逐位循环提取 → 标记"应用 SHRN 替代 PMOVMSKB"。

### 3.2 跨 Lane 数据移动优化

| 模式 | 当前写法 | 优化方案 |
|------|---------|---------|
| 滑动窗口逐个加载 | 循环内多次 `ld1`（重叠加载） | `EXT` 拼接位移，复用上一轮数据 |
| YUV 平面→打包 | 逐元素 `ins`/`mov` | `ZIP1`/`ZIP2` + `TRN1`/`TRN2` 批量交织 |
| 多通道解交织 | 逐元素提取 | `UZP1`/`UZP2` |
| 结构体数组→数组结构体 | 多次 `ldr/str` | `LD4`/`ST4` 结构加载/存储 |

### 3.3 SVE 谓词循环识别

**最佳实践模式**（已充分利用 SVE，无需优化）：
```asm
whilelt p0.s, x0, x1
.loop:
    ld1w  z0.s, p0/z, [x2, x0, lsl #2]
    fmla  z2.s, p0/m, z0.s, z1.s
    incw  x0
    whilelt p0.s, x0, x1
    b.first .loop
```

**待优化模式**（有 SVE 指令但仍用标量尾循环）：
- SVE `WHILELT` + `b.cont` 后跟标量尾循环 → 标记"尾循环可消除"。

---

## 四、内存层级调优模式

### 4.1 软件预取（PRFM）距离校准

| 循环体指令数 | 建议预取距离（迭代数） | PRFM type |
|-------------|---------------------|-----------|
| < 10 | 4 | `PLDL1KEEP`（重用）/ `PLDL1STRM`（流式） |
| 10-20 | 2 | `PLDL1KEEP` |
| > 20 | 1 | `PLDL2KEEP`（远距离） |

**检测**：若已有 `prfm` 但距离固定（如 `#256`，未按循环体大小调整）→ 标记"PRFM 距离可调优"。

### 4.2 Non-temporal 访存

| 场景 | 当前 | 优化 |
|------|------|------|
| 大块 memcpy（> L2 大小） | `ldp`/`stp`（缓存污染） | → `ldnp`/`stnp` |
| memset(0) ≥ 64 字节 | 逐字节 `str wzr, [xn], #4` | → `DC ZVA` (cache line 粒度的清零) |

### 4.3 屏障强度降级

| 当前 | 可降为 | 条件 |
|------|--------|------|
| `dmb sy` | `dmb ish` | 仅多核共享域同步，不涉及外设 |
| `dmb ish` | `dmb ishst` | 仅需 store 顺序保证 |
| `dsb sy` | `dsb ish` | 同上 |

---

## 五、领域驱动优先级调整

匹配到特定领域后，对应检测项优先级提升：

| 领域 | 提升优先级检查项 |
|------|----------------|
| media_codec | `specialized_instruction` (UABAL/SMLAL/TBL 替换)、`simd_cross_lane` (ZIP/UZP/TRN/EXT) |
| crypto | `specialized_instruction` (AES 多块交织、EOR3/BCAX 融合)、`loop_unroll` (AES/PMULL 流水线填充) |
| hpc | `specialized_instruction` (FMLA→GEMM 展开)、FMA 累加器拆分、SVE 谓词循环 |
| storage_crc_ec | `specialized_instruction` (CRC32/PMULL 展开) |
| text_processing | `simd_cross_lane` (SHRN 替代 PMOVMSKB)、`specialized_instruction` (TBL 查表) |
| bioinformatics | `specialized_instruction` (SMAX/SMIN)、`simd_cross_lane` (归约) |
| ml_inference | `specialized_instruction` (SDOT/SMMLA 累加器展开) |
| networking | `specialized_instruction` (LSE 原子、REV 字节序)、`memory_hierarchy` (PRFM 包预取) |
