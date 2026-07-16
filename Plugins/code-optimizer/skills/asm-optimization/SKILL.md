---
name: asm-optimization
description: ARM 汇编指令级优化，支持 LDP/STP 合并、后索引寻址、冗余 mov 消除、循环展开、流式成对加载展开、预取增强、循环计数器优化、指令惯用法优化、多向量合并测零和宏融合适配。适用于 .s/.S 纯汇编文件和 C/C++ 内联 asm 块。SVE 相关优化由 apply-vectorization 和 loop-unrolling 处理。
---

# 汇编指令级优化

你是一位鲲鹏性能优化流水线的汇编优化专家。你的任务是对 ARM64 汇编代码进行指令级优化，包括合并访存对、转换寻址模式、消除冗余指令、展开循环、流式成对加载展开和增强预取。SVE 实现由 apply-vectorization（向量化）和 loop-unrolling（循环展开）处理，不属于本 Skill 范围。

用户调用了 `/asm-optimization`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 80],
  "optimization_type": "ldp_stp_merge|post_index_addressing|redundant_move_elimination|loop_unroll|stream_pair_load_unroll|prefetch_enhancement|loop_counter|instruction_idiom|multi_vector_merge_test|macro_fusion_enablement|alu_instruction_fusion",
  "language": "pure_asm|inline_asm",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名（汇编标签名）
- `source_file`：源文件路径（.s/.S 或含内联 asm 的 .c/.cpp）
- `lines`：函数在源文件中的行范围 [start, end]
- `optimization_type`：优化子类型
- `language`：`pure_asm`（纯汇编文件 .s/.S）或 `inline_asm`（C/C++ 中的 `__asm__ volatile` 块）
- `context.prepareProject`：prepare-project 输出（包含 microarch_file 微架构文档、instruction_perf_file 指令性能数据）
  - `instruction_perf_file`：指令延迟/吞吐量/端口压力数据。根据微架构选择查询脚本：
	  - TSV110 (Kunpeng-0xd01)：207 条指令模式（LLVM 调度模型），`python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_tsv110.py <指令名>`
	  - 0xd03 (Kunpeng-0xd03/0xd06)：29 张表、754 条助记符（硬件优化手册），`python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_uarch_b.py <指令名>`
	  指令调度、端口分配、展开因子计算、uarch_substitution 替换决策均需查询此数据
- `context.analyzeHotspot`：analyze-hotspot 输出（包含 perf 数据和优化点证据）

## 执行步骤

### 步骤 0：检测代码类型

检查 `source_file` 扩展名和内容，确定处理路径：

| 条件 | language | 修改方式 |
|------|----------|---------|
| `.s` 或 `.S` 扩展名 | `pure_asm` | 直接 Edit 汇编文件 |
| `.c`/`.cpp`/`.h` 且包含 `__asm__ volatile` 或 `asm volatile` | `inline_asm` | Edit C 文件内的 asm 字符串块 |
| 其他 | — | 返回 `success=false`，`error_message="不支持的文件类型"` |

**所有优化步骤共用**：用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的代码后，按 `optimization_type` 执行对应步骤（步骤 4、6 为委托模式，其余为直接优化）。

ARM64 指令编码约束（LDP/STP offset 范围、后索引 imm 范围、Kunpeng 延迟/吞吐量数据、融合对规则、功能单元分布）详见 `references/arm64-instruction-patterns.md`。

**Pipeline 指令查询契约**：任何指令替换、inline asm 修复或 NEON/SVE 指令事实判断，都必须先按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 查询并记录 evidence。优先使用稳定入口：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword "<semantic keyword>" --family <neon|sve|sve2> --json
```

查询结果只证明语法、FEAT 依赖和语义存在；是否替换仍必须结合 `query_tsv110.py` / `query_uarch_b.py` 的延迟、吞吐和端口数据。

### 步骤 1：LDP/STP 合并（ldp_stp_merge）

将相邻的单寄存器访存指令合并为寄存器对访存指令。

**合并条件**（全部满足才合并）：
- 两条指令类型相同（同是 ldr 或同是 str），基址寄存器相同
- 偏移差 = 8（64 位 X 寄存器）或 4（32 位 W 寄存器）或 16（128 位 Q 寄存器）
- 目标/源寄存器不同（`Xn != Xn+1`）
- 两条指令之间无分支标签、无对基址寄存器的修改
- LDP/STP offset 在有效范围内（见 reference 文件）

**示例**：
```assembly
# 合并前（2 条指令）→ 合并后（1 条指令）
ldr Xn,   [Xbase, #off]       ldp Xn, Xn+1, [Xbase, #off]
ldr Xn+1, [Xbase, #off+8]
```

**Q/D 寄存器同理**：`ldr Dn/Dn+1` → `ldp Dn, Dn+1`，`ldr Qn/Qn+1` → `ldp Qn, Qn+1`。

记录 `pairs_merged` 计数。

### 步骤 2：后索引寻址（post_index_addressing）

将"加载/存储 + 指针递增"合并为后索引指令。

**合并条件**（全部满足才合并）：
- ldr/str 后紧跟 add（基址寄存器相同），中间最多间隔 1 条指令（中间指令不修改 base 或目标寄存器）
- add 的操作数中一个是基址寄存器，另一个是立即数
- 后索引立即数在有效范围内（见 reference 文件）
- 基址寄存器在 ldr/str 与 add 之间未被其他指令读取

**示例**：
```assembly
# 合并前（2 条指令）             # 合并后（1 条指令）
ldr Xn, [Xbase]                 ldr Xn, [Xbase], #imm
add Xbase, Xbase, #imm
```

NEON 同理：`ld1 {v0.4s}, [x0]; add x0, x0, #16` → `ld1 {v0.4s}, [x0], #16`。

记录 `post_index_count` 计数。

### 步骤 3：冗余 mov 消除（redundant_move_elimination）

在 5 指令窗口内对 mov 指令构建临时 def-use 链，消除不必要移动。

**消除规则**：
- mov 源和目标相同 → 删除
- mov 的目标在下次被覆盖前未被读取 → 删除
- `mov X, xzr` 且 X 仅作为零源操作数 → 替换为 xzr/wzr 直用

记录 `moves_eliminated` 计数。

### 步骤 4：循环展开（loop_unroll）

委托 `loop-unrolling` skill，使用微架构感知的展开系数计算、寄存器重命名和累加器拆分。

#### 4.1 检测 SIMD 类型

读取循环体代码，判断 `simd_type`：
- 包含 NEON 寄存器（v0-v31 的 q/d/s 形式）→ `"neon"`
- 包含 SVE 寄存器（z0-z31/p0-p15）→ `"sve"`
- 无以上指令 → `"scalar"`

#### 4.2 估算当前并行度

统计循环体内独立的 SIMD 操作数量（同一寄存器上的连续操作算 1 个独立操作链，不同寄存器的操作可并行），填入 `recommended_parallelism`。

#### 4.3 构造委托请求

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "simd_type": "<4.1 检测结果>",
  "recommended_parallelism": <4.2 估算值>,
  "language": "<input.language>",
  "context": {
    "prepareProject": "<input.context.prepareProject>"
  }
}
```

#### 4.4 委托调用

使用 Skill tool 调用 `loop-unrolling`，传入上述 JSON。

#### 4.5 提取结果

从 `unrolling_result` 提取：
- `unrolled_code` → 设置 `optimized_code`
- `unroll_factor` → 填入 `details.unroll_factor`
- `accumulator_split` → 填入 `details.accumulator_split`（新增字段）
- `parallelism_before` / `parallelism_after` → 记录到 `details`
- 设置 `techniques_used: ["loop_unroll"]`

若 `unrolling_result.success == false`，返回 `success=false`，`error_message` 取自 `unrolling_result.error_message`。

### 步骤 4b：流式成对加载 + 2x 展开（stream_pair_load_unroll）

将同一热循环内多个独立 stream 状态链的 indexed load 改为 per-chain pointer stream，并用 `ldp ..., [ptr], #imm` 同时完成成对加载和指针推进。该子类型是 `ldp_stp_merge`、`post_index_addressing`、`loop_unroll_2x` 的组合应用，不做算法重写。

**适用代码**：
- `.s/.S` 纯汇编热循环。
- C/C++ 中已有 `__asm__ volatile` / `asm volatile` 的热循环，且源码 load 与 asm 消费链在同一循环内。

**必须全部满足**：
- 独立状态链数量 >= 2；每条链有自己的 accumulator/state，链间无 RAW 依赖。
- 每条链读取固定 stride 的连续 stream 元素，2 个相邻元素可组成合法 `ldp` pair。
- indexed load 可安全改写为循环前初始化的 per-chain pointer，循环内只用后索引推进。
- 默认只做 2x 展开；若迭代数不是偶数，必须保留原语义的尾处理。
- register-pressure-analysis 或手工预算显示新增 pointer 和 pair temporary 后仍非 high/severe。
- C/C++ inline asm 路径必须使用约束分配寄存器，不硬编码通用寄存器；新增输出 temporary 使用 early-clobber，clobber 至少包含 `"memory"`；不得使用 `x18`。

**拒绝条件**：
- 单一循环携带依赖链、链间需要顺序合并、间接索引、未知 stride、volatile/atomic/I/O、循环体写同一 stream、函数调用副作用、尾处理不明确、寄存器压力高。
- 需要改变算法分块、合并公式或跨块组合语义才能正确时，返回 `success=false`，建议人工算法优化。

**C/C++ inline asm 生成要求**：
1. 在循环前建立每条链的 `const T *ptrN`，指向原 indexed load 的起点。
2. 循环体改为每轮处理 2 个元素；用一段 inline asm 发出每条链的 `ldp` 后索引加载。使用 named operands；pair-load 目标 temporary 必须使用 early-clobber（如 `"=&r"`），base pointer 使用 `"+r"`，避免 AArch64 writeback base 与目标寄存器重叠。
   - AArch64 GNU inline asm 后索引模板必须写成 `ldp %x[val_a], %x[val_b], [%[ptr]], #16`（按元素宽度调整立即数）。
   - 禁止写成 `ldp ..., [%[ptr], #16]` 或 `ldp ..., [%x[ptr], #16]`；这是 unsigned-offset addressing，不会推进 pointer，不能满足 `post_index_addressing`。
3. 按原有独立状态链顺序消费 loaded value，并保持链内顺序。若原循环通过多条独立状态链交错隐藏单链延迟，展开后采用 round-robin 调度：先消费所有链的 `*_a`，再消费所有链的 `*_b`；不得把同一 carried-dependency 链的两个操作连续放在一起，除非已有指令延迟证据证明这样更优。
4. 尾部元素使用原有标量/单次 load 路径处理。只改写当前 optimization point 覆盖的热循环；除非 evidence 单独覆盖其他循环，不要顺手改写后续 remainder/cleanup loop。
5. 输出 `details.expected_instruction_delta`，至少包含 `ldp_post_index_expected=true` 和 `indexed_ldr_reduced=true`。

记录：
- `details.stream_pair_load_unroll.chains`
- `details.stream_pair_load_unroll.unroll_factor = 2`
- `details.stream_pair_load_unroll.schedule_policy = "preserve_interchain_round_robin"`
- `details.stream_pair_load_unroll.asm_constraint_policy = "early_clobber_pair_load_outputs"`
- `details.stream_pair_load_unroll.combined_transforms = ["ldp_stp_merge", "post_index_addressing", "loop_unroll_2x"]`
- `techniques_applied: ["stream_pair_load_unroll"]`

### 步骤 5：预取增强（prefetch_enhancement）

委托 `prefetch-optimization` skill 的汇编路径，使用更精确的距离计算公式。

#### 6.1 构造委托请求

```json
{
  "function": "<input.function>",
  "source_file": "<input.source_file>",
  "lines": [<input.lines[0]>, <input.lines[1]>],
  "access_pattern": "stream",
  "context": {
    "prepareProject": "<input.context.prepareProject>",
    "analyzeHotspot": "<input.context.analyzeHotspot>"
  }
}
```

#### 6.2 委托调用

使用 Skill tool 调用 `prefetch-optimization`，传入上述 JSON。

#### 6.3 提取结果

从 `prefetch_result` 提取 `optimized_code`，设置 `techniques_used: ["prefetch_enhancement"]`。

### 步骤 6：循环计数器优化（loop_counter）

利用 ARM64 `subs`/`adds` 自带置标志位的特性，消除冗余 `cmp` 指令。

**模式 A：subs 折叠 cmp**

`sub Xn, Xn, #imm; cmp Xn, #0; b.ne` → `subs Xn, Xn, #imm; b.ne`（2 条指令消除 1 条）

检测条件：sub 后紧跟 `cmp Xd, #0`（中间无其他指令或仅标签），sub 的 imm 在 0-4095 范围，cmp 操作数与 sub 目标相同。

**模式 B：倒计数转换**

`add pos, pos, #step; cmp pos, end; b.lt` → `subs len, len, #step; b.gt`（倒计数替代正计数，subs 自带置标志）

适用条件：step 为编译期常量，pos 在循环后无其他消费者。

**不优化**：pos 循环外有消费者、step 非常量、cmp 比较对象非 #0。

记录 `loop_counters_optimized` 计数。

### 步骤 7：指令惯用法优化（instruction_idiom）

用 ARM64 指令惯用法替换次优序列。

**模式 A：零化** — `mov Xn, #0` → 若 Xn 被多次读取用 `eor Xn, Xn, Xn`（打破依赖链）；若仅作为零源操作数出现一次用 `xzr`/`wzr` 直用。

**模式 B：mov→add 折叠** — `mov Xd, Xn; add Xd, Xd, #imm` → `add Xd, Xn, #imm`（1 指令消除）。条件：mov 目标仅被 add 使用一次。

**模式 C：mov→扩展寄存器 add 折叠** — `mov Xd, Xn; add Xd, Xd, Xm, lsl #N` → `add Xd, Xn, Xm, lsl #N`。条件同 B。

**模式 D：mov→orr 替代** — `mov Xd, #imm` 且 imm 在 orr 位掩码范围内 → `orr Xd, xzr, #imm`（编码可能更优）。

记录 `idioms_applied` 计数。

### 步骤 8：多向量合并测零（multi_vector_merge_test）

将循环内多个独立的向量零测试+分支合并为单次 OR 归约+单分支。

**NEON 路径**：将 N 个 `cmeq vN; umaxv sN; fmov wN; cbnz wN, .fail` 合并为 `cmeq` 们 + `orr v0.16b, v0.16b, vN.16b` 级联 + 单次 `umaxv; fmov; cbnz`。

**SVE 路径**：同理，N 个 `cmpeq pN; brkas pN; b.ne` 合并为 `cmpeq` 们 + `orrs p0.b, p0/z, p0.b, pN.b` 级联 + 单次 `brkas; b.ne`。

**合并条件**：N ≥ 2 个连续 test+branch，所有分支目标相同，测试间无 label，中间指令不修改参与合并的向量寄存器。

记录 `vectors_merged` 计数。

### 步骤 9：宏融合适配（macro_fusion_enablement）

重排循环内指令，使 `subs/adds/cmp` 与 `b.cond` 相邻以启用 Kunpeng-0xd01/0xd03/0xd06 微操作融合（融合为 1 uop）。

**安全条件**（全部满足才重排）：
- 被移动的指令不依赖 flags（非 `adc`/`sbc`/`csel`/`cset` 等）
- 被移动的指令不修改 flags（非 `subs`/`adds`/`cmp`/`tst` 等）
- 被移动的指令不修改 `subs` 使用的寄存器
- 移动后不改变 RAW/WAR/WAW 依赖顺序

融合对和详细约束见 `references/arm64-instruction-patterns.md`（Kunpeng-0xd01/0xd03/0xd06 微操作融合规则）。

**执行**：将 `subs`/`cmp` 移到循环底部紧邻 `b.cond` 之前，中间指令按原顺序插入分支之前。记录 `fusions_enabled` 计数。

### 步骤 10：微架构感知指令替换（uarch_substitution）

通过查询指令性能数据（`query_tsv110.py` / `query_uarch_b.py`），用延迟/吞吐数据驱动指令替换决策。

#### 10.1 识别微架构

从 `input.context.prepareProject` 中读取 `microarch_file`（如 `kunpeng920-microarchitecture.md` 或 `kunpeng_uarch_b-microarchitecture.md`），提取微架构代号：

| CPU | 微架构 | 查询脚本 |
|-----|--------|---------|
| Kunpeng-0xd01 | TSV110 | `python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_tsv110.py <指令>` |
| Kunpeng-0xd03/0xd06 | 0xd03 | `python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_uarch_b.py <指令>` |
| 未知/非鲲鹏 | — | 使用 arm64-instruction-patterns.md 的通用参考表作为 fallback |

#### 10.2 候选替换检测与量化评估

对循环体/热点区域的每条指令，按两阶段决策：

**阶段 1：指令发现**（避免"不知道有这条指令"的知识盲区）

在对关键操作（乘加、绝对值、点积、查表等）做替换决策前，先用 `arm-instructions-query` 发现候选指令家族：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword "<semantic keyword>" --family <neon|sve|sve2> --json
```

- 按功能发现：`instruction-search --keyword "<功能关键词>"`（如 "multiply accumulate"、"absolute value"、"dot product"）
- 校验存在性：`instruction --name <指令名> --family <neon|sve|sve2>` 确认目标 ISA 是否支持
- 枚举家族：`instruction-search --keyword "<操作码前缀>"`（如搜索 FMLA → 发现 FMLA/FMLS/FMLAL/FMLSL）
- 记录 `arm-instruction-query-contract.md` 定义的 `isa_instruction` evidence

**阶段 2：量化评估**（用 query_tsv110/query_uarch_b 查询延迟/吞吐）

仅对阶段 1 发现的候选指令做性能对比。**每次替换前必须查询真实指令延迟**：

**模式 A：分离乘加 → 融合乘加**
- 检测：`fmul vT, vA, vB` + `fadd vD, vD, vT` 相邻（vT 后续无使用）
- 查询：`python3 query_<uarch>.py FMUL` 和 `python3 query_<uarch>.py FMLA`
- 决策：若 `FMLA_latency < FMUL_latency + FADD_latency`（且吞吐不更差）→ 替换为 `fmla vD, vA, vB`
- 示例：TSV110 上 FMUL=5c + FADD=4c = 9c，FMLA=7c → 节省 2c；0xd03 上 FMUL=3c + FADD=3c = 6c，FMLA=4c → 节省 2c。两个微架构都受益但节省量不同

**模式 B：dup+pmull → pmull2**
- 检测：`dup vH.2d, vX.d[1]; dup vL.2d, vX.d[0]; pmull ...` 模式
- 查询：`python3 query_<uarch>.py DUP` 和 `python3 query_<uarch>.py PMULL`
- 决策：若 pmull2 延迟 ≤ dup 延迟 + pmull 延迟 → 替换（消除 2 条 dup）
- 条件：寄存器分配允许，pmull2 的高半部分结果与预期一致

**模式 C：mov #0 + add → xzr 直用**
- 检测：`mov Xn, #0; add Xd, Xm, Xn`（Xn 仅此一处使用）
- 查询：`python3 query_<uarch>.py MOV` 和 `python3 query_<uarch>.py ADD`
- 决策：若 MOV 延迟 ≥ 1c → 替换为 `add Xd, Xm, xzr`（打破 WAW 依赖链 + 省 1 指令）

**模式 D：连续 ldr/str → ldp/stp**（委托步骤 1，但用量化数据验证）
- 查询：`python3 query_<uarch>.py LDR` 和 `python3 query_<uarch>.py LDP`
- 验证：LDP 吞吐 ≥ LDR 吞吐 × 2 → 合并有收益；若 LDP 吞吐 = LDR 吞吐（单端口限制）→ 跳过合并

**模式 E：相邻同类型 ALU 指令多输入融合（EOR→EOR3 / ORR→ORR3 等）**

将 N 条串行同类型按位逻辑指令融合为一条多输入指令，缩短关键路径并减少指令数。

- 检测：循环体内存在连续 N≥2 条相同助记符的按位逻辑指令（`EOR`/`ORR`/`AND`/`BIC`），第一条的目的寄存器在第二条中被消费为源操作数，中间无其他消费者
- 典型模式：`EOR Vd.16B, Va.16B, Vb.16B; EOR Vd.16B, Vd.16B, Vc.16B`（临时结果 `Vd` 仅作为中间值，Va/Vb/Vc 为三个独立输入）
- ISA 前置：执行 `arm_query.py instruction --name <MNEMONIC>3 --family neon --json`（如 `EOR3`、`BCAX`）确认目标 ISA 支持对应的多输入指令；`arm_query.py instruction --name <MNEMONIC>3 --family sve --json` 确认 SVE 变体
- 若 `arm_query.py` 返回 `not_found` → 跳过此替换，在 `alu_fusion_data` 中记录 `"skipped: isa_not_supported"`
- 查询：`python3 query_<uarch>.py <MNEMONIC>` 和 `python3 query_<uarch>.py <MNEMONIC>3`（如 EOR vs EOR3）
- 决策：若 `fused_latency < N × single_latency`（或至少等价但减少 N-1 条指令）→ 替换为多输入指令
- 示例（0xd03）：EOR latency=1c × 2 = 2c 串行 vs EOR3 latency=1c，节省 1c 关键路径 + 1 指令
- 安全验证：(1) 中间寄存器在两条指令间无其他消费者；(2) 源操作数互不相同且均不同于最终目标寄存器；(3) 不跨标签/分支边界；(4) 不破坏调用约定
- 编码约束：EOR3 Vd.16B, Vn.16B, Vm.16B, Va.16B — Vd 不可为 SP，四个寄存器均为 SIMD 寄存器；XOR 满足交换律和结合律，操作数顺序可任意排列
- BCAX 变体：`AND Vt.16B, Vm.16B, NOT(Va.16B); EOR Vd.16B, Vn.16B, Vt.16B` → `BCAX Vd.16B, Vn.16B, Vm.16B, Va.16B`
- 记录 `alu_fusions` 计数和 `alu_fusion_data.fusions[]`（含 mnemonic、saving_cycles、saving_instructions）

#### 10.3 安全验证与替换执行

对每个候选替换：
1. 验证临时寄存器无后续使用（Read 替换点前后 5 条指令的上下文）
2. 验证语义等价（操作数和结果寄存器的数据流一致）
3. 用查询结果验证延迟/吞吐确实更优（至少节省 1c 或 1 条指令）
4. 执行 Edit 替换

记录 `uarch_substitutions` 计数（按替换类型 A/B/C/D/E 细分），将查询到的具体延迟数据写入 `details.uarch_data`：
```json
{
  "microarch": "TSV110",
  "substitutions": [
    {"type": "A", "before": "FMUL+FADD=9c", "after": "FMLA=7c", "saving": "2c + 1 instr"}
  ]
}
```

### 步骤 11：指令交错调度（instruction_interleaving）

通过查询指令性能数据获取每条指令的真实执行端口，基于端口冲突检测（而非硬编码分类）重排指令顺序。

#### 11.1 提取循环体指令列表

从目标函数的循环体中逐条提取指令（跳过标签和伪指令），记录每条指令的操作码和操作数寄存器。

#### 11.2 查询端口分配

对每条指令查询其执行端口：

- TSV110：`python3 query_tsv110.py <操作码>` → 从 `ports` 字段获取端口列表（如 `["FSU1", "FSU2"]`、`["Ld0St", "Ld1"]`、`["ALUAB"]`）
- 0xd03：`python3 query_uarch_b.py <操作码>` → 从 `pipeline` 字段获取流水线（如 `"V"`、`"LD"`、`"ALU14"`）

TSV110 端口分组（供参考）：
| 组 | 端口 | 容量 | 典型指令 |
|----|------|------|---------|
| INT | ALU, AB, ALUAB (P0-P2) | 3 条/周期 | add/sub/mov/eor/and/orr/lsl |
| MDU | MDU (P3) | 1 条/周期 | mul/sdiv/udiv/crc |
| FP0 | FSU1 (P4) | 1 条/周期 | fmul/fadd/fmla/fmax/fmin（FP32 ADD 专有）|
| FP1 | FSU2 (P5) | 1 条/周期 | fmul/fadd/fmla/fmax/fmin（FP32 MUL 专有）|
| LS0 | Ld1 (P6) | 1 条/周期 | ldr/ldp/ld1/prfm（Load）|
| LS1 | Ld0St (P7) | 1 条/周期 | ldr/str/ldp/stp/st1（Load+Store）|
| BR | AB (P1/P2) | 1 条/周期 Taken | b.cond/b/ret/cbz/cbnz |

#### 11.3 瓶颈检测

不依赖硬编码阈值，而是基于端口占用计算：

1. 对连续 N 条同端口组指令，计算 `port_pressure = N / port_capacity`
2. 拥塞判定：`port_pressure > 2.0`（即同端口连续指令超过端口容量 2 倍）→ 该段存在拥塞
3. 示例：TSV110 FP0(FSU1) 容量=1，连续 3 条 FSU1 指令 → pressure=3.0 → 拥塞；连续 2 条 → pressure=2.0 → 边缘

#### 11.4 交错重排

在数据依赖允许范围内，将拥塞段的指令分散到空闲端口：

1. 构建指令间的 RAW/WAR/WAW 依赖图
2. 在不破坏依赖的前提下，将同端口组指令与不同端口组指令交替排列
3. 理想交错：INT → FP0 → LS0 → INT → FP1 → LS1 → BR（每周期尽可能占用不同端口）
4. 融合对保持相邻：`subs`/`adds`/`cmp` 与 `b.cond` 不分离

**安全条件**：不破坏依赖、不跨 `cbz`/`ret`/label 重排、融合对保持相邻。

**不优化**：循环体 ≤ 4 条指令、紧密依赖链无可交错空间、循环体内有间接跳转、所有指令共用同一端口组（无可交错资源）。

记录 `interleaved_loops` 计数，将瓶颈分析写入 `details.interleave_data`：
```json
{
  "microarch": "TSV110",
  "bottlenecks": [{"port_group": "FP0", "pressure": 3.0, "instructions": ["fmul v0", "fmla v1", "fadd v2"]}],
  "interleaved": true
}
```

### 步骤 12：谓词模式选择（predication_mode）

将 SVE 代码中的 `ptrue`（全谓词）+ 手动尾循环改为 `whilelt`（尾谓词）自动处理，消除跨迭代假依赖。

**前置检查**：确认为 SVE 代码，循环内有 `ptrue pN.s` 全谓词设置。

**替换步骤**：
1. `ptrue p0.s` → `whilelt p0.s, x_idx, xN`（自动尾谓词）
2. load/store 地址计算改为 `[base, idx, lsl #shift]` 索引寻址
3. 删除手动尾处理循环代码块
4. loop counter 从 `subs + b.ge` 改为 `incw + whilelt + b.first`
5. `x_idx` 从 0 开始初始化

**收益**：消除手动尾循环代码、消除尾 lane 无效 load（不活跃 lane 不发起内存访问）、消除 `ptrue` 跨迭代假依赖。

记录 `predication_optimized: true`。

### 步骤 13：失败返回指南

在设置 `success=false` 前：
- 在 `error_message` 中具体说明拒绝或失败原因
- **不要做 inline self-challenge**：共享上下文的自挑战不够严苛。挑战由编排器在 apply-optimization 之后通过独立的 AdversarialReview 阶段执行，对所有优化点进行全面审核

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "asm_optimization_result": {
    "success": true,
    "original_code": "<原始代码文本>",
    "optimized_code": "<优化后的代码文本>",
    "optimization_type": "ldp_stp_merge|post_index_addressing|redundant_move_elimination|loop_unroll|stream_pair_load_unroll|prefetch_enhancement|loop_counter|instruction_idiom|multi_vector_merge_test|macro_fusion_enablement|uarch_substitution|instruction_interleaving|predication_mode|alu_instruction_fusion",
    "language": "pure_asm|inline_asm",
    "techniques_applied": ["ldp_stp_merge", "post_index_addressing"],
    "instructions_before": 42,
    "instructions_after": 35,
    "details": {
      "pairs_merged": 3,
      "post_index_count": 2,
      "stream_pair_load_unroll": {
        "chains": 3,
        "unroll_factor": 2,
        "schedule_policy": "preserve_interchain_round_robin",
        "asm_constraint_policy": "early_clobber_pair_load_outputs",
        "combined_transforms": ["ldp_stp_merge", "post_index_addressing", "loop_unroll_2x"],
        "tail_policy": "preserve_original_tail",
        "expected_instruction_delta": {
          "ldp_post_index_expected": true,
          "indexed_ldr_reduced": true
        }
      },
      "moves_eliminated": 1,
      "unroll_factor": 2,
      "accumulator_split": false,
      "prefetch_count": 0,
      "loop_counters_optimized": 0,
      "idioms_applied": 0,
      "vectors_merged": 0,
      "fusions_enabled": 0,
      "uarch_substitutions": 0,
      "uarch_data": { "microarch": "TSV110|0xd03|null", "substitutions": [] },
      "alu_fusions": 0,
      "alu_fusion_data": { "microarch": "TSV110|0xd03|null", "fusions": [] },
      "interleaved_loops": 0,
      "interleave_data": { "microarch": "TSV110|0xd03|null", "bottlenecks": [], "interleaved": false },
      "predication_optimized": false
    },
    "modified_file": "<source_file_path>",
    "error_message": ""
  }
}
```

失败时：
- `success=false`
- `optimized_code=""`
- `error_message` 具体说明拒绝或失败原因。挑战由编排器的 AdversarialReview 阶段独立执行

`optimization_type` 与输入一致。`techniques_applied` 列出实际应用的技术。

## 规则

- **不改变程序语义**：所有优化必须保持功能等价性
- **失败返回**：返回 `success=false` 时说明具体原因。不做 inline self-challenge（由编排器的 AdversarialReview 阶段独立执行）
- **LDP/STP offset 范围检查**：立即数字段在 [-512, 504] 内，8 字节对齐
- **后索引 imm 范围检查**：0-32760，8 字节对齐
- **不跨标签合并**：LDP/STP 和后索引的合并不能跨越汇编标签
- **不破坏调用约定**：不修改 callee-saved 寄存器（x19-x29）的保存/恢复
- **SVE 实现由 apply-vectorization 和 loop-unrolling 处理**：本 Skill 不做 NEON→SVE 转换。SVE 谓词化消除尾循环属于 vectorization_deepen，SVE 宽度扩展属于 throughput-enhancement
- **循环展开需尾处理**：迭代数不整除展开因子时必须添加 epilogue
- **源码替换由上游 `apply-optimization` 统一执行**：本 Skill 只返回代码文本
- **`prefetch_enhancement` 委托 `prefetch-optimization`**：不重复实现预取逻辑
- **`loop_unroll` 委托 `loop-unrolling`**：不重复实现展开逻辑，使用其微架构感知的展开系数计算、寄存器重命名和累加器拆分
- **内联 asm 同样适用**：`language: "inline_asm"` 时，`optimized_code` 为修改后的 asm 块字符串内容（不含外围 C 代码）
