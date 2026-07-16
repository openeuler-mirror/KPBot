# ARM64 指令编码参考（Kunpeng 优化用）

## LDP/STP 指令编码约束

### 寄存器对寻址

```
LDP Xt1, Xt2, [Xn|SP{, #imm}]   // 64-bit 通用寄存器对
STP Xt1, Xt2, [Xn|SP{, #imm}]
LDP Wt1, Wt2, [Xn|SP{, #imm}]   // 32-bit 通用寄存器对
STP Wt1, Wt2, [Xn|SP{, #imm}]
LDP Dt1, Dt2, [Xn|SP{, #imm}]   // 64-bit 浮点/SIMD 寄存器对
STP Dt1, Dt2, [Xn|SP{, #imm}]
LDP Qt1, Qt2, [Xn|SP{, #imm}]   // 128-bit SIMD 寄存器对
STP Qt1, Qt2, [Xn|SP{, #imm}]
```

### Immediate offset 范围

| 寄存器宽度 | 偏移范围 | 编码 |
|-----------|---------|------|
| 32-bit (W) | [-256, 252]，4 字节对齐 | signed ÷4 |
| 64-bit (X) | [-512, 504]，8 字节对齐 | signed ÷8 |
| 128-bit (Q) | [-1024，1008]，16 字节对齐 | signed ÷16 |
| 64-bit SIMD (D) | [-512, 504]，8 字节对齐 | signed ÷8 |

### 寄存器对规则

- Xt1 和 Xt2 必须不同（架构要求，即使 `ldp x0, x0, [sp]` 编码非法）
- 32-bit (W) 和 64-bit (X) 不能混用
- SIMD D（64-bit）和 Q（128-bit）不能混用

## 后索引寻址约束

### 格式

```
LDR Xt, [Xn|SP], #simm     // 加载 + 基址递增
STR Xt, [Xn|SP], #simm     // 存储 + 基址递增
LD1 {Vt.4s}, [Xn], #16     // NEON 向量加载 + 基址递增
LD1W {Zt.s}, p0/z, [Xn]    // SVE 加载，用 incw 递增
```

### Immediate 范围

| 指令 | 范围 |
|------|------|
| LDR/STR (X) | [-256, 255]，无对齐要求 |
| LDR/STR (W) | [-256, 255]，无对齐要求 |
| LDR/STR (Q) | [-256, 255]，无对齐要求 |
| LD1/ST1 SIMD | [寄存器大小] 到 [寄存器大小 × 255]，无符号 |
| SVE INC/ADDVL | 根据元素大小变化 |

## Kunpeng-0xd01/0xd03/0xd06 微架构参考

### 执行单元

| 单元 | 数量 | 吞吐量 |
|------|------|--------|
| L/S (Load/Store) | 2 | 2 loads + 1 store/cycle (0xd01)；2 loads + 2 stores/cycle (0xd03/0xd06) |
| NEON 整数 ALU | 2×2 | 4 NEON integer ops/cycle |
| NEON FMA | 2 | 2 FMA/cycle = 8 FLOPS/cycle (NEON 128-bit FP32) |
| SVE | 2×2 | 与 NEON 共享 FMA/ALU lane |
| Branch | 1 | 1 branch/cycle |

### 指令延迟

| 指令 | 延迟（cycles）| 吞吐量 |
|------|-------------|--------|
| LDR (L1 hit) | 4 | 2/cycle |
| LDR (L2 hit) | 10-12 | — |
| LDR (DDR) | ~100 | — |
| LDP (L1 hit) | 4 | 1/cycle（一次取 2 寄存器） |
| FMLA/FMLS | 5 | 2/cycle |
| FMUL | 3 | 2/cycle |
| FADD | 3 | 2/cycle |
| MOV (reg) | 1 | 3/cycle |
| SUB/SUBS | 1 | 3/cycle |
| B.cond | 1（预测正确）/ 15-20（预测失败） | 1/cycle |

### Kunpeng SVE 特殊说明

- SVE 256-bit 实现使用 2 个 128-bit NEON 执行 lane
- 吞吐量：SVE 256-bit FMLA = 2 NEON 128-bit FMLA = 8 FP32 FLOPS/cycle，无吞吐优势
- SVE 优势：谓词化（避免尾循环）、gather/scatter、first-fault load
- NEON→SVE 转换在 Kunpeng 上无纯计算吞吐优势（256-bit SVE = 2×128-bit NEON lane），仅在需要谓词化（消除尾循环）、gather/scatter 或 first-fault load 时考虑。SVE 实现由 apply-vectorization 和 loop-unrolling 处理，不属于 asm-optimization 范围

## prfm 指令

### 格式

```
prfm <prfop>, [Xn|SP{, #pimm}]
```

### pimm 范围：0-32760，8 字节对齐

### 常用 prfop

| 类型 | locality | 含义 |
|------|----------|------|
| PLDL1KEEP | 3 | 预取到 L1，保持（多次访问） |
| PLDL2KEEP | 2 | 预取到 L2，保持 |
| PLDL3KEEP | 1 | 预取到 L3，保持 |
| PLDL1STRM | 0 | 预取到 L1，streaming（用完即弃） |
| PSTL1KEEP | 3 | 写预取到 L1 |

### 行为

- 地址无效时被 CPU 忽略（不产生异常）
- 不占用通用寄存器
- 不影响标志位
- 可以安全插入任何位置

## Kunpeng-0xd01/0xd03/0xd06 指令延迟与吞吐量

### 通用指令

| 指令 | 延迟（cycles） | 吞吐量（/cycle） | 执行单元 |
|------|-------------|-----------------|---------|
| ADD/SUB (reg) | 1 | 3 | X |
| ADD/SUB (imm) | 1 | 3 | X |
| ADDS/SUBS | 1 | 3 | X |
| MOV (reg) | 1 | 3 | X |
| MOV (imm 16-bit) | 1 | 3 | X |
| MOVZ/MOVN/MOVK | 1 | 3 | X |
| EOR/ORR/AND (reg) | 1 | 3 | X |
| LSL/LSR/ASR | 1 | 3 | X |
| MUL (32-bit) | 3 | 1 | X |
| MUL (64-bit) | 4 | 0.5 | X |
| SDIV/UDIV | 12 | 0.08 | X |
| LDR (L1 hit) | 4 | 2 | L/S |
| LDR (L2 hit) | 10-12 | — | L/S |
| STR | 1 | 1 (0xd01); 2 (0xd03/0xd06) | L/S |
| LDP (L1 hit) | 4 | 1 | L/S |
| STP | 1 | 1 | L/S |
| LDRB/LDRH | 4 | 2 | L/S |

### NEON/SIMD 指令

| 指令 | 延迟（cycles） | 吞吐量（/cycle） | 执行单元 |
|------|-------------|-----------------|---------|
| LD1 {v.4s}, [Xn] (L1 hit) | 5 | 1 | L/S |
| LD1 {v.4s}, [Xn], #16 (L1 hit) | 5 | 1 | L/S |
| ST1 {v.4s}, [Xn] | 1 | 1 | L/S |
| LD1R {v.4s}, [Xn] | 4 | 1 | L/S |
| FMUL v.4s | 3 | 2 | F |
| FADD v.4s | 3 | 2 | F |
| FMLA v.4s (融合乘加) | 5 | 2 | F |
| FMLA v.4s (分离: FMUL+FADD) | 3+3=6 | 2+2=4 等效 | F |
| FMAX/FMIN v.4s | 3 | 2 | F |
| MUL v.4s | 3 | 2 | F |
| ADD v.4s | 2 | 2 | F |
| MLA v.4s | 5 | 2 | F |
| EOR/ORR/AND v.16b | 2 | 3 | F |
| CMEQ v.4s, v.4s, #0 | 3 | 2 | F |
| CMGT v.4s, v.4s, v.4s | 3 | 2 | F |
| UMAXV s, v.4s（水平归约） | 5 | 0.5 | F |
| DUP v.4s, Wn | 1 | 2 | F |
| FMOV Wn, Sn | 1 | 2 | F |
| INS v.s[idx], Wn | 2 | 1 | F |
| SHL v.4s, v.4s, #N | 3 | 2 | F |
| USHR v.4s, v.4s, #N | 2 | 2 | F |
| TBL v.16b, {v.16b}, v.16b | 4 | 0.5 | F |

### SVE 指令 (Kunpeng 256-bit)

| 指令 | 延迟（cycles） | 吞吐量（/cycle） | 执行单元 |
|------|-------------|-----------------|---------|
| LD1W {z.s}, p0/z, [Xn] (L1) | 5+ | 1 | L/S |
| ST1W {z.s}, p0, [Xn] | 2 | 1 | L/S |
| FMLA z.s, p0/m, z.s, z.s | 5 | 2 | F |
| FMUL z.s, p0/m, z.s, z.s | 3 | 2 | F |
| FADD z.s, p0/m, z.s, z.s | 3 | 2 | F |
| CMPEQ p.s, p0/z, z.s, #0 | 3 | 2 | F |
| ORRS p.b, p0/z, p.b, p.b | 2 | 2 | F |
| BRKAS p.b, p0/z, p.b | 1 | 1 | B |
| INCW Xn | 1 | 2 | X |
| WHILELT p.s, Xn, Xm | 2 | 1 | X |
| PTRUE p.s | 1 | 2 | X |

### 关键替换规则

基于以上数据，以下替换有确切收益：

| 原始序列 | 替换序列 | 节省 | 条件 |
|---------|---------|------|------|
| `FMUL vT.4s, vA.4s, vB.4s; FADD vD.4s, vD.4s, vT.4s` | `FMLA vD.4s, vA.4s, vB.4s` | 1 指令 + 1 延迟周期 | vT 后续无使用 |
| `dup vD_H.2d, vX.d[1]; dup vD_L.2d, vX.d[0]; pmull xxx` | `pmull2 vD_H.1q, vX.2d, vC.2d; pmull vD_L.1q, vX.1d, vC.1d` | 消除 2 条 dup | ARM64 CRC/多项式运算 |
| `mov Xn, #0; add Xd, Xm, Xn` | `add Xd, Xm, xzr` | 1 指令 + 打破依赖 | Xn 仅此一处使用 |
| 连续 2×ldr | `ldp` | 1 指令 | 见 ldp_stp_merge |
| 4×ldr（连续偏移） | 2×ldp | 2 指令 | 见 ldp_stp_merge |

### Kunpeng-0xd01/0xd03/0xd06 功能单元分布

| 单元类型 | 数量 | 指令类别 |
|---------|------|---------|
| L/S (Load/Store) | 2 | ldr/str/ldp/stp/ld1/st1/prfm |
| F (NEON/SIMD Float) | 2×2 (4 lanes) | fmul/fadd/fmla/fmax/cmeq/eor v/orr v/tbl |
| X (Integer ALU) | 3 | add/sub/mov/eor/and/orr/lsl/mul |
| B (Branch) | 1 | b.cond/b/ret/bl/cbz/cbnz/brkas |

**指令交错策略**：循环内避免 ≥3 条连续同单元指令。理想交错模式：L/S → F → X → L/S → F → X → B（各单元均衡使用）。

## Kunpeng-0xd01/0xd03/0xd06 微操作融合规则

以下指令对在**相邻**时可融合为 1 个微操作：

| 标志设置指令 | 条件分支 | 说明 |
|-------------|---------|------|
| SUBS Xn, Xn, #imm | B.NE/B.EQ | 减计数 + 循环分支（最常见） |
| SUBS Xn, Xn, #imm | B.HI/B.LS/B.GT/B.LE/... | 带条件码的所有变体 |
| ADDS Xn, Xn, #imm | B.NE/B.EQ | 加计数模式 |
| CMP Xn, #imm | B.EQ/B.NE/B.GT/B.LE/... | 比较+分支 |

**关键约束**：
- 两条指令必须**字面相邻**——中间不能有任何其他指令
- 标签不算"指令"（`subs x0, x0, #1; .Llabel: b.ne .Lloop` 仍可融合）
- `CMP` 的立即数必须是 12-bit 无符号编码范围 (0-4095)
- 融合仅在单发射槽执行，收益是减少 1 个重命名/发射槽占用

## EOR3 / BCAX 多输入逻辑指令融合

### EOR3（三输入异或）

EOR3 Vd.16B, Vn.16B, Vm.16B, Va.16B

**编码字段**：
- Vd (5 bits)：目标 SIMD 寄存器（不可为 SP/31）
- Vn (5 bits)：第一源 SIMD 寄存器
- Vm (5 bits)：第二源 SIMD 寄存器
- Va (5 bits)：第三源 SIMD 寄存器

**功能等价性**：
- EOR3 Vd, Vn, Vm, Va ≡ Vd = Vn ^ Vm ^ Va（128-bit 按位异或，三输入）
- 等价于：EOR Vtmp.16B, Vn.16B, Vm.16B; EOR Vd.16B, Vtmp.16B, Va.16B（2 指令 → 1 指令）
- 由于 XOR 满足交换律和结合律，操作数顺序可任意排列而结果不变

**ISA 要求**：FEAT_SHA3（Kunpeng-0xd01/0xd03/0xd06/0xd03/0xd06 均支持）

**融合检测模式**：
```
源模式（需替换）：
  EOR Vd.16B, Va.16B, Vb.16B    // 第一步：合并 Va 和 Vb
  EOR Vd.16B, Vd.16B, Vc.16B    // 第二步：混入 Vc（依赖上一条 Vd）

替换为：
  EOR3 Vd.16B, Vb.16B, Vc.16B, Va.16B  // 单指令完成三输入异或
```

**延迟/吞吐参考**（0xd03 / Kunpeng-0xd03/0xd06）：
- EOR (NEON v.16b)：latency=1c，throughput=4/cycle，pipeline=V
- EOR3 (NEON v.16b)：latency=1c，throughput=4/cycle，pipeline=V
- 融合收益：2c 串行关键路径 → 1c（-1c），2 条指令 → 1 条指令（-1 发射槽位）
- 收益单位：每条融合节省 1c 关键路径延迟 + 1 条指令

**安全验证清单**：
1. Vd 在两条 EOR 之间无其他消费者（临时结果仅用于连接）
2. Va、Vb、Vc 互不相同且均不同于最终目标寄存器（在第二条 EOR 中的角色）
3. 不跨越汇编标签或分支目标
4. 不修改 callee-saved 寄存器（x19-x29/v8-v15）的上下文

### BCAX（三输入位清除 + 异或）

BCAX Vd.16B, Vn.16B, Vm.16B, Va.16B ≡ Vd = Vn ^ (Vm & ~Va)

**融合检测模式**：
```
源模式（需替换）：
  BIC Vt.16B, Vm.16B, Va.16B     // 第一步：清除 Va 中的位
  EOR Vd.16B, Vn.16B, Vt.16B     // 第二步：异或

替换为：
  BCAX Vd.16B, Vn.16B, Vm.16B, Va.16B
```

### RAX1 / XAR（旋转 + 异或组合）

RAX1 Vd.2D, Vn.2D, Vm.2D ≡ Vd = (Vn ^ Vm) <<< 1（XOR + 左旋转 1 位）
XAR Vd.2D, Vn.2D, Vm.2D, #imm ≡ Vd = (Vn ^ Vm) >>> imm（XOR + 右旋转）

**检测模式**：同上述框架，检测 EOR + 旋转指令的连续序列。

### 通用检测规则

识别 N≥2 条串行同助记符按位逻辑指令 → 搜索 `arm_query.py instruction-search --keyword "<助记符前缀>" --family neon --json` 确认多输入变体 → 用 `query_tsv110.py`/`query_uarch_b.py` 量化延迟收益 → 安全验证后替换。

| 原始序列 | 替换序列 | 节省 | 条件 |
|---------|---------|------|------|
| `EOR Vd.16B, Va.16B, Vb.16B; EOR Vd.16B, Vd.16B, Vc.16B` | `EOR3 Vd.16B, Vb.16B, Vc.16B, Va.16B` | 1c + 1 指令 | FEAT_SHA3，Vd 中间无其他消费者 |
| `BIC Vt.16B, Vm.16B, Va.16B; EOR Vd.16B, Vn.16B, Vt.16B` | `BCAX Vd.16B, Vn.16B, Vm.16B, Va.16B` | 1c + 1 指令 | FEAT_SHA3，Vt 中间无其他消费者 |
