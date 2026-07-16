# ARM 指令延迟/吞吐量快速参考

> 用途：interactive-optimizer 指令级周期建模时快速查阅指令性能数据，避免每次查询 `query_tsv110.py`。
> 配合 `01-kunpeng-hardware.md` 使用：01 识别瓶颈类型（ALU/FP/访存/分支），02 量化瓶颈的具体周期数。
> 数据来源：LLVM `AArch64SchedTSV110.td`、`arm64-instruction-patterns.md`、鲲鹏微架构文档。标记 `*` 的数值为推理值，建议运行时用 `query_tsv110.py` / `query_uarch_b.py` 验证。

---

## 1. TSV110 (Kunpeng-0xd01) 指令性能

> **端口命名对照**：本文用 P0-P7 对应 OSACA 端口模型。FP/SIMD 端口 P4/P5 在微架构文档中也称为 FP0/FP1 或 FSU1/FSU2。标量 ALU 端口 P0/P1/P2 对应 ALU0/ALU1/ALU2。
> **`*` 标记**：标注 `*` 的延迟/吞吐量为从 LLVM 调度模型推断或条件实测值，未在硬件优化手册中直接确认。精确数据运行时调 `query_tsv110.py <mnemonic>`。

### 1.1 标量整数指令

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 执行端口 | 说明 |
|------|-------------|-----------------|---------|------|
| ADD/SUB/MOV (reg) | 1 | 3 | P0/P1/P2 | 含 MOVZ/MOVN/MOVK |
| ADDS/SUBS (flag-set) | 1 | 3 | P0/P1/P2 | 可与相邻 B.cond 宏融合 |
| EOR/ORR/AND (reg) | 1 | 3 | P0/P1/P2 | 位逻辑操作 |
| LSL/LSR/ASR | 1 | 3 | P0/P1/P2 | 移位操作 |
| CSEL/CINC/CSET | 1 | 3 | P0/P1/P2 | 条件选择，无分支预测惩罚 |
| CMP/CMN/TST | 1 | 3 | P0/P1/P2 | 可与相邻 B.cond 宏融合 |
| MUL (32-bit) | 3* | 1 | P3 (MDU) | 权威来源标注 4c，LLVM 模型 3c |
| MUL (64-bit) | 4 | 0.5 | P3 (MDU) | 64-bit 乘法吞吐减半 |
| SDIV/UDIV | 12 | 0.08 | P3 (MDU) | 除法极慢，优先用乘法倒数替代 |
| B.cond (预测正确) | 1 | 1 | P1/P2 | 每次最多 1 条分支 |
| B.cond (预测失败) | 15–20 | — | — | 误预测惩罚，BTB 64 entry，间接分支 ~256 目标 |
| ADDS+SUBS → B.cond (宏融合) | 1 (融合) | 1 | P1/P2 | 必须字面相邻，标签不阻断 |

> **关键规则**：
> - 整数 ALU 3 端口（P0/P1/P2），分支仅 P1/P2。P0 不接分支，将分支排在 P1/P2 可让 P0 同时执行 ALU 指令。
> - `ADDS/SUBS/CMP + B.cond` 相邻可宏融合为 1 uop，节省发射槽。
> - CSEL 替代不可预测分支：1c vs 15-20c 误预测惩罚，收益显著。

### 1.2 标量浮点指令

标量浮点操作使用 FP/SIMD 执行单元（P4/P5），与 NEON 共享资源。

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 执行端口 | 说明 |
|------|-------------|-----------------|---------|------|
| FADD/FSUB Sd, Sn, Sm (FP32) | 5 | 1 | P4 only | FP32 ADD 仅 P4 |
| FADD/FSUB Dd, Dn, Dm (FP64) | 5* | 1 | P4 only | 推测与 FP32 同延迟 |
| FMUL Sd, Sn, Sm (FP32) | 5 | 1 | P5 only | FP32 MUL 仅 P5 |
| FMUL Dd, Dn, Dm (FP64) | 5* | 1 | P5 only | 推测与 FP32 同延迟 |
| FMADD/FMSUB (FP32 FMA) | 5 | 2 | P4/P5 | 标量融合乘加 |
| FMADD/FMSUB (FP64 FMA) | 5* | 0.5 | P4/P5 | FP64 FMA 四分之一速率 |
| FDIV/FSQRT | 多周期 | <0.1 | P4/P5 | 尽量避免，优先倒数近似 |

> **端口不对称**：FP32 ADD 仅 P4，FP32 MUL 仅 P5。混合 ADD/MUL 密集循环需交错排布避免单端口拥塞。

### 1.3 NEON 向量指令

所有 NEON 操作通过 P4/P5 执行，向量宽度 128-bit（4×32-bit lane）。

#### 浮点向量

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|------|-------------|-----------------|------|
| FMUL v.4s | 5 | 1 | FP32 向量乘法，仅 P5 |
| FADD v.4s | 5 | 1 | FP32 向量加法，仅 P4 |
| FMLA v.4s (融合乘加) | 5 | 2 | FMUL+FADD 合并，省 1 指令 + 1c |
| FMLA v.4s 分离模拟 (FMUL+FADD) | 5+5=10 | 等效 4 ops | FMLA 节省延迟和发射槽 |
| FMAX/FMIN v.4s | 3 | 2 | — |
| FCVT 系列 | 3* | 2 | 类型转换 |

#### 整数向量

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|------|-------------|-----------------|------|
| ADD/SUB v.4s | 2 | 2 | 向量整数加减 |
| MUL v.4s | 8* | 1 | 向量整数乘法，仅 FP0 |
| MLA v.4s (乘加) | 8* | 1 | 整数融合乘加 |
| SHL v.4s, #N | 4* | 2 | 立即数左移 |
| USHR v.4s, #N | 3* | 2 | 立即数逻辑右移 |
| EOR/ORR/AND v.16b | 2 | 2 | 位逻辑操作（2/cycle=FP/SIMD 端口上限） |
| CMEQ v.4s, v.4s, #0 | 3* | 2 | 向量比较等于零 |
| CMGT v.4s, v.4s, v.4s | 3* | 2 | 向量有符号大于比较 |
| UMAXV s, v.4s (水平归约) | 5* | 0.5 | 跨 lane 归约，吞吐低 |
| DUP v.4s, Wn | 4* | 2 | 标量→向量广播 |
| INS v.s[idx], Wn | 4* | 1 | 向量元素插入 |

#### 向量访存

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|------|-------------|-----------------|------|
| LD1 {v.4s}, [Xn] (L1 hit) | 5 | 1 | NEON 128-bit 加载 |
| LD1 {v.4s}, [Xn], #16 (L1 hit) | 5 | 1 | 后索引寻址 |
| ST1 {v.4s}, [Xn] | 3* | 1 | NEON 128-bit 存储 |
| LD1R {v.4s}, [Xn] | 7* | 1 | 加载+广播单值到所有 lane |
| LDP/STP (Q 寄存器对) (L1 hit) | 5/1 | 1 | 一次加载/存储 2 个 128-bit 寄存器 |

#### V↔X 搬移与数据重排

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|------|-------------|-----------------|------|
| FMOV Wn, Sn (V→X, 32-bit) | 1 | 2 | 搬移低 32 位 |
| FMOV Xn, Dn (V→X, 64-bit) | 1 | 2 | 搬移低 64 位 |
| FMOV vN.d[1], Xn (X→V, 高 64-bit) | 2* | 1 | 需要 INS 或特殊 FMOV 变体 |
| EXT v.16b, v.16b, v.16b, #imm | 2 | 0.5 | 跨 lane 字节提取，标量拆解需 ~3 条 ALU 指令 |
| TBL v.16b, {v.16b}, v.16b | 4 | 0.5 | 并行查表，**无标量等价指令** |

#### 专有指令（不可标量化）

| 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|------|-------------|-----------------|------|
| AESE v.16b, v.16b | 3* | 1* | AES 加密轮，**无标量等价** |
| AESMC v.16b, v.16b | 3* | 1* | AES MixColumns，**无标量等价** |
| PMULL v.1q, v.1d, v.1d | 3* | 1* | 多项式乘法（GHASH），**无标量等价** |
| SHA256 系列 | 2-3* | 0.5* | SHA-256 哈希加速，**无标量等价** |

### 1.4 NEON→标量 映射与标量化决策

当 V 管线饱和而 ALU 管线空闲时，可将 V 管线上的串行依赖链迁移到标量 ALU。

| NEON 指令 | 标量等价 | V 延迟 | ALU 延迟 | 决策 |
|-----------|---------|--------|---------|------|
| `shl vN.2d, vN.2d, #1` | `lsl xN, xN, #1` | 3c | 1c | **标量化优先** |
| `ushr vN.2d, vN.2d, #63` | `lsr xN, xN, #63` | 2c | 1c | **标量化优先** |
| `and vN.16b, vN.16b, vM.16b` | `and xN, xN, xM` | 2c | 1c | **标量化优先** |
| `eor vN.16b, vN.16b, vM.16b` | `eor xN, xN, xM` | 2c | 1c | **标量化优先** |
| `orr vN.16b, vN.16b, vM.16b` | `orr xN, xN, xM` | 2c | 1c | **标量化优先** |
| `mov vN.16b, vM.16b` | `mov xN, xM` | 2c | 1c | **标量化优先** |
| `dup vN.2d, xN` | 无需，直接使用 xN | — | — | 广播后立即可省 |
| `ext vN.16b, ..., #8` | lsl+eor+and (~3 指令) | 2c | ~3c | **保持 V**（拆解开销大） |
| `tbl vN.16b, ...` | 不可标量化 | 4c | — | **必须保持 V** |
| `aese/aesmc` | 不可标量化 | 2c | — | **必须保持 V** |
| `pmull` | 不可标量化 | 3c | — | **必须保持 V** |

**标量化决策规则**：

```
评估串行依赖链长度 chain_len：
  chain_len < 5  → 保持 V 管线（搬移开销无法摊薄）
  chain_len ≥ 5 且 V 管线饱和（利用率 > 80%）→ 考虑标量化
  chain_len ≥ 5 且含 TBL/AESE/PMULL 等不可标量化指令 → 整个链保持 V
```

**搬移开销**：V→X 单次 FMOV 1c（64-bit），128-bit 数据需 2 次 FMOV。标量化净收益：
```
scalar_benefit = neon_chain_cycles - (scalar_chain_cycles + fmov_count × fmov_latency)
```
仅当 `scalar_benefit > 0` 且 V 管线确为瓶颈时才执行。

### 1.5 内存访问

| 操作 | L1 延迟 | L2 延迟 | L3 延迟 | DRAM 延迟 | 说明 |
|------|--------|--------|--------|----------|------|
| LDR (简单寻址) | 4c | 10c | Partition ~36c, Shared >90c | 96ns (~230c @ 2.4GHz) | 2/cycle 吞吐 |
| LDR (索引寻址) | 5–6c | +同上 | +同上 | +同上 | 地址计算多 1–2c |
| LDP (L1 hit) | 4c | — | — | — | 1/cycle，一次取 2 寄存器 |
| LDRB/LDRH | 4c | — | — | — | 字节/半字加载同延迟 |
| STR | 1c | — | — | — | 0xd01: 1/cycle; 0xd03/0xd06: 2/cycle |
| STP | 1c | — | — | — | 1/cycle |
| LD1 {v.4s} (L1 hit) | 5c | — | — | — | NEON 128-bit 加载 |
| ST1 {v.4s} | 1c | — | — | — | NEON 128-bit 存储 |

**Store Forwarding**：
| 场景 | 延迟 | 说明 |
|------|------|------|
| 同地址同大小 | 6–7c | Store→Load 转发 |
| 跨 16B 边界 | 7–9c | +1~2c 惩罚 |
| 部分重叠 | 6–7c | 与完全重叠同延迟 |

**PRFM 预取**：
| 预取类型 | prfop 示例 | 含义 |
|---------|-----------|------|
| PLDL1KEEP | `prfm #3, [Xn, #256]` | 预取到 L1，保持（多次访问） |
| PLDL2KEEP | `prfm #2, [Xn, #256]` | 预取到 L2，保持 |
| PLDL3KEEP | `prfm #1, [Xn, #256]` | 预取到 L3，保持 |
| PLDL1STRM | `prfm #0, [Xn, #256]` | 预取到 L1，streaming（用完即弃） |
| PSTL1KEEP | `prfm #3, [Xn, #256]` (写) | 写预取到 L1 |

- PRFM 地址无效时被 CPU 忽略（不抛异常），不占通用寄存器，不影响标志位。
- 预取距离 = 目标延迟 / 单轮计算时间，通常 2-3 轮迭代提前量。
- 避免过度预取占用 L/S 带宽。

**TLB 层次**：
| 层级 | 条目 | 命中延迟 |
|------|------|---------|
| L1 I-TLB | 32 (全相联) | — |
| L1 D-TLB | 32 (全相联) | — |
| L2 Unified TLB | 1024 | +11c |

> L2 TLB miss +11c 代价高。大页（2MB/1GB）可显著降低 TLB miss。

### 1.6 SVE 关键差异（仅 Kunpeng-0xd01 高配版 / 0xd03 / 0xd06）

TSV110 基础版不支持 SVE。高配版及 0xd03/LC950 支持 SVE（256-bit）。

| SVE 指令 | 延迟 (cycles) | 吞吐量 (/cycle) | 说明 |
|----------|-------------|-----------------|------|
| `ld1w {z.s}, p0/z, [xn]` (L1) | 5+ | 1 | SVE 256-bit 加载 |
| `st1w {z.s}, p0, [xn]` | 2 | 1 | SVE 256-bit 存储 |
| `fmla z.s, p0/m, z.s, z.s` | 5 | 2 | SVE 融合乘加 |
| `fmul z.s, p0/m, z.s, z.s` | 3 | 2 | SVE 乘法 |
| `fadd z.s, p0/m, z.s, z.s` | 3 | 2 | SVE 加法 |
| `add z.s, p0/m, z.s, z.s` | 2 | 2 | SVE 整数加法 |
| `sel z.s, p0, z.s, z.s` | 2* | 2 | 谓词选择（类似 CSEL） |
| `cmpeq p.s, p0/z, z.s, #0` | 3 | 2 | 谓词比较 |
| `orrs p.b, p0/z, p.b, p.b` | 2 | 2 | 谓词逻辑 |
| `ptrues p.s` | 1 | 2 | 全真谓词生成 |
| `whilelt p.s, xn, xm` | 2 | 1 | 循环边界谓词 |

**关键结论**：SVE 256-bit = 2 个 NEON 128-bit lane。SVE 256-bit FMLA 吞吐 = 2 NEON 128-bit FMLA = 8 FP32 FLOPS/cycle，**无纯计算吞吐优势**。仅在以下场景选择 SVE：
- 谓词化消除尾循环（减少分支和代码膨胀）
- Gather/scatter（NEON 无等价指令）
- First-fault load（NEON 无等价指令）
- 长度无关循环（跨 SVE 宽度移植）

---

## 2. 0xd03/LC950 差异

0xd03（Kunpeng-0xd03/0xd03）和 LC950（Kunpeng-0xd06）相对 TSV110 的关键差异：

| 维度 | TSV110 (0xd01) | 0xd03 | LC950 (0xd06) |
|------|-------------|-------------|-------------|
| ISA | ARMv8.2 | ARMv9-A | ARMv8-A (v9.2 兼容) |
| SVE | 基础版无/高配有 SVE | SVE + SVE2 (256-bit) | SVE + SVE2 (256-bit) |
| 取指/译码 | 4-wide | **8-wide** | **8-wide** |
| ROB | ~96 | **192** | **192** |
| SMT | 无 | **SMT2** | **SMT2** |
| FP/SIMD 吞吐 | 2 FMA/cycle (128-bit) | **4 FMA/cycle (128-bit)** | **4 FMA/cycle (128-bit)** |
| L/S 带宽 | 2L 或 1L+1S/cycle | **3L+2S/cycle** | **3L+2S/cycle** |
| L1 I-Cache | 64 KB | **128 KB** | **128 KB** |
| L2 Cache | 512 KB/core, 10-way | **1 MB/core**, 8-way | **1 MB/core**, 8-way |
| 分支预测 | BTB 64 entry | **Main BTB 12K + Nano BTB** | **Main BTB 12K + Nano BTB** |
| 整数 ALU | 3/cycle | **4+/cycle**（推测） | **4+/cycle**（推测） |
| 硬件预取器 | SEQ | **BOF+SEQ+SMS+MOP+META (5 种)** | **BOF+SEQ+SMS+MOP+META (5 种)** |
| 核心数 | 最多 64/芯片 | 最多 192 | 最多 192 |

> **使用提示**：0xd03/LC950 在取指宽度、FP 吞吐、L/S 带宽、分支预测方面大幅提升。建模时**不要直接套用 TSV110 的延迟/吞吐数据**。运行时调用 `query_uarch_b.py` 获取精确的每指令数据（LLVM 调度模型中 0xd03 的指令数远超 TSV110，不宜硬编码全文）。

**0xd03/LC950 相对 TSV110 的指令级变化趋势**：
- 整数 ALU 指令：延迟相似（1c），吞吐更高（4+ vs 3/cycle）
- FP/NEON 指令：延迟相似，吞吐翻倍（4 vs 2/cycle）
- L/S 指令：吞吐大幅提升（3L+2S vs 2L+1S/cycle）
- 分支预测：误预测惩罚差异较大（更宽的流水线 → 误预测惩罚可能略增）
- 乘法/除法：延迟可能降低（管线更深 + 更宽的发射）

---

## 3. 使用指南

### 3.1 指令级周期建模流程

对热点函数执行以下 6 步：

```
1. objdump 反汇编热点函数
   objdump -d <binary> | sed -n '/<function>:/,/^$/p'

2. 提取循环体指令序列
   从循环标签（如 .LBB0_5）到分支回跳指令（b.ne/b.eq）

3. 逐条查表，标注每条指令：
   - 延迟（latency）：从产生结果到可使用结果的周期数
   - 吞吐（throughput）：该指令类型每周期最多发射数
   - 端口（port）：使用的执行单元

4. 计算 latency_bound（最长依赖链）
   - 从循环入口寄存器到最后写回的寄存器
   - 沿 RAW（Read After Write）依赖边累加延迟
   - 示例：LDR(4c) → FMUL(3c) → FADD(3c) → STR(1c) = 4+3+3+1 = 11c
     若有 4 路并行（NEON），等效每元素 11c/4 ≈ 2.75c

5. 计算 throughput_bound（最大端口压力）
   - 统计循环内各类指令在关键端口上的占比
   - 示例：循环 20 条指令，其中 8 条在 P4/P5（2 端口）→ min iteration = 8/2 = 4c
   - 循环 20 条指令，其中 4 条在 L/S（2 端口）→ min iteration = 4/2 = 2c
   - throughput_bound = max(4c, 2c) = 4c

6. 计算 theoretical_min 并对比实际周期
   theoretical_min = max(latency_bound, throughput_bound)
   实际周期 = perf stat cycles / iterations
   余量 = 实际周期 - theoretical_min
   若余量 > 20% → 存在未被建模的开销（缓存 miss、分支误预测、端口冲突等）
```

### 3.2 快速端口压力速查

| 瓶颈类型 | 关键端口 | 每周期能力 | 判断依据 |
|---------|---------|-----------|---------|
| ALU 瓶颈 | P0/P1/P2 | 3 uop/cycle | 循环内整数指令占比 > 60% |
| FP 瓶颈 | P4/P5 | 2 uop/cycle (FP32) | 循环内 FP/NEON 指令占比 > 50% |
| 访存瓶颈 | P6/P7 | 2 load 或 1L+1S/cycle | 循环内 LDR/STR 占比 > 40% |
| 分支瓶颈 | P1/P2 | 1 branch/cycle | 循环内分支 > 1 条或高误预测率 |
| 多端口争用 | 跨类型 | — | 单类指令 < 60% 但仍有端口压力 |

### 3.3 端口交错排布模板

TSV110 理想交错模式（避免 ≥3 条连续同单元指令）：

```
L/S → F → X → L/S → F → X → B
```

- L/S = ldr/str/ldp/stp/ld1/st1/prfm（P6/P7）
- F = fmul/fadd/fmla/fmax/eor v/tbl（P4/P5）
- X = add/sub/mov/eor/and/orr/lsl（P0/P1/P2）
- B = b.cond/b/ret/cbz/cbnz（P1/P2）

### 3.4 常见性能反模式

| 反模式 | 表现 | 修复 |
|--------|------|------|
| 连续 ≥3 条 F 指令 | P4/P5 拥塞 | 交错插入 X 或 L/S 指令 |
| 连续 ≥3 条 L/S 指令 | P6/P7 拥塞 | 交错插入计算指令，利用加载延迟 |
| 不可预测分支在热路径 | 15-20c 误预测惩罚 | CSEL/CINC 替代 |
| 标量除法在循环内 | 12c/指令，吞吐 0.08 | 乘法倒数替代 |
| DUP 后立即 FMOV 回 X | 冗余搬移 | 直接在 X 寄存器上操作 |
| 64-bit MUL 替代 32-bit | 吞吐减半（0.5 vs 1/cycle） | 能用 32-bit 就用 32-bit |
| 软件预取距离不当 | 预取太近（已到达）或太远（被逐出） | 预取距离 = ceil(目标延迟 / 单轮迭代时间) |

### 3.5 与其他文档的关系

| 需要什么 | 去哪里 |
|---------|--------|
| 完整微架构参数（ROB/PRF/TLB/缓存层次） | `01-kunpeng-hardware.md` |
| TSV110 精确每指令数据（暴露延迟/吞吐/端口） | `python3 .../query_tsv110.py <指令名>` |
| 0xd03 精确每指令数据 | `python3 .../query_uarch_b.py <指令名>` |
| NEON/SVE/SME 指令语义和 intrinsic | 调用 `arm-instructions-query` skill |
| ARM SPE 微架构事件分析 | 调用 `arm-spe-analysis` skill |
| 标矢量混合决策（完整方法论） | `scalar-vector-hybrid/SKILL.md` |
| ARM64 指令编码约束（LDP/STP/后索引/PRFM 范围） | `arm64-instruction-patterns.md` |
