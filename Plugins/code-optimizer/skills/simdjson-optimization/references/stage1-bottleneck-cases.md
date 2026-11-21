# stage1 四类瓶颈案例集

> 平台：Kunpeng 920B 7260Y（implementer 0x48, part 0xd03, ARMv8.2-A, 2.2 GHz, NEON 128-bit + SVE2 256-bit）
> OS/工具链：openEuler 24.03 SP3, aarch64, GCC, Release `-O3 -DNDEBUG`
> 语料：`jsonexamples/twitter.json`，631,515 B，非 ASCII 字节 95,406（15.11%），多字节 UTF-8 字符 31,808
> 固定实现：`SIMDJSON_FORCE_IMPLEMENTATION=arm64`

## 0. 基线与热点分布

| 指标 | Run1 | Run2 | Run3 | 均值 |
|---|---|---|---|---|
| stage1 c/b | 0.575193 | 0.577779 | 0.580515 | **0.577829** |
| stage1 GB/s | 3.786969 | 3.806600 | 3.789696 | **3.794422** |
| stage2 c/b | 0.507857 | 0.507206 | 0.507351 | 0.507471 |
| all c/b | 1.088544 | 1.082985 | 1.086943 | 1.086157 |
| all GB/s | 1.994930 | 1.997839 | 1.997643 | 1.996804 |

perf stat：IPC 3.14，L1-dcache miss 4.49%，LLC miss 12.21%，branch miss 740,812。

stage1 占全解析约 **50%** cycles，stage2 约 43%。因此 stage1 提速 10% ≈ 端到端 5%。

热点指令（stage1 内，`perf annotate`）：

| 占比 | 指令 | 源码 | 判读 |
|---|---|---|---|
| 52.5% | `orr v21,v21,v15` | `utf8_lookup4_algorithm.h:175` `error \|= prev_incomplete` | **采样滑移伪影**，真停顿在上游 is_ascii 链 |
| 18.4% | `ldr q2,[sp,#160]` | `utf8_lookup4_algorithm.h:194` `prev_input_block` | 寄存器溢出 |
| 8.7% | `lsr x14,x2,x14` | `json_structural_indexer.h:47` `zero_leading_bit` | bit_indexer 串行链 |
| 4.5% | `cmeq v5,v8,v28` | `json_string_scanner.h:63` `eq('\\')` | 字符串扫描 |
| 3.8% | `mov v2.s[3],w16` | `json_structural_indexer.h:46` | bit_indexer 写向量 |
| 3.2% | `clz x16,x14` | `bitmanipulation.h:87` | bit_indexer 串行链 |
| 1.2% | `umaxv b2,v2.16b` | `arm64.cpp:96` `is_ascii` | 水平归约 |

## 1. 案例 A：is_ascii 串行归约链

源码路径：

```
json_structural_indexer.h:242  checker.check_next_input(in)
  └─ utf8_lookup4_algorithm.h:173  check_next_input()
       └─ arm64.cpp:94  if (is_ascii(input))
            └─ arm64.cpp:95   input.reduce_or()      // chunks[0..3] 3×ORR
            └─ arm64.cpp:96   bits.max_val() < 0x80  // umaxv + fmov + tbnz
```

汇编依赖链（每 64B 迭代必走）：

| 指令 | 延迟 | 来源 |
|---|---|---|
| `orr v2,v7,v8` | 1c | `simd.h:436` reduce_or |
| `orr v2,v2,v9` | 1c | 同上 |
| `orr v2,v2,v10` | 1c | 同上 |
| `umaxv b2,v2.16b` | ~5c | `simd.h:198` max_val |
| `fmov x0,d2` | ~3c | NEON→GPR 跨域 |
| `tbnz w0,#7,<non-ascii>` | ~1c | 比较+分支 |

合计约 **12c 串行停顿**，无法与其它指令重叠。

为什么不是 `orr v21`：`v21`（`this->error`）确实跨迭代 RAW，但 `orr v21` 位于循环第 116/158 条，生产到下一轮消费间隔约 50 周期，1c 依赖被完全掩盖。样本堆在这里是 ARM64 非精确采样向前滑移 2~5 条的结果。

跨架构对照：x86 `_mm_movemask_epi8` 约 1c 即可把字节高位取到 GPR；AArch64 没有等价指令，NEON 路径下 `umaxv+fmov` 已接近上限。

已试替代（均退化，A 层）：

| 方案 | c/b | 变化 |
|---|---|---|
| 基线 `reduce_or+umaxv+fmov` | 0.5778 | — |
| `vshrq + vget_low/high + vorr + vget_lane` | 0.5910 | +2.3% |
| `lteq(0x7F) + to_bitmask(vpaddq×3 + vgetq_lane)` | 0.6065 | +5.0% |

根因：指令数增加（6→10/13），NEON→GPR 跨域 ~3c 不变，临界路径只省约 2c，被端口竞争与噪声淹没。**正确方向是 ILP 掩盖（加宽 step + 交错），不是替换单条指令。**

## 2. 案例 B：寄存器溢出与交错

`step<128>` 同时装载 `in_1..in_4`。非交错顺序让 `in_1`/`block_1` 在 `scanner.next(in_2)` 时仍活跃，编译器必须同时保持 8 个输入寄存器 + 2 份 scanner 中间结果 + checker/indexer 状态，远超 32 个 v 寄存器。

三版本对比（A 层）：

| 版本 | stage1 c/b | stage1 GB/s | 栈帧 | NEON spill/reload | 头号热点 |
|---|---|---|---|---|---|
| 基线 `step<64>` | 0.5778 | 3.794 | 288B | 125 / 110 | `umaxv→orr` 52.5%（滑移） |
| `step<128>` 非交错 | 0.6085 | 3.617（**-4.7%**） | 464B | 261 / 265 | `ldp q2,q4,[sp,#160]` 29% |
| `step<128>` 交错 | 0.5763 | 3.819（**+0.7%**） | 368B | **42 / 45** | `orr v25` 20%（正常 OR） |

非交错的反汇编（问题形态）：

```asm
0x40a218  umaxv  b7, v7.16b        ; 块1 is_ascii 归约
0x40a238  fmov   x0, d7            ; NEON→GPR
0x40a240  tbnz   w0, #7, ...       ; 非 ASCII 分支
0x40a244  ldp    q2, q4, [sp,#160] ; ← 29% 热点：从栈恢复块2数据
0x40a248  orr    v5, v2, v4        ; 块2 error |= prev_incomplete
```

交错后的反汇编（推荐形态）：

```asm
0x40a074  umaxv  b4, v4.16b        ; 块1 is_ascii
0x40a080  ldp    q9, q10, [x6,#64] ; 与 umaxv 并行加载块2输入
0x40a088  fmov   x1, d4            ; 块1 NEON→GPR
0x40a0a0  addp   v2, v2, v2        ; 块1 to_bitmask
0x40a0a8  tbnz   w1, #7, ...       ; 块1 非 ASCII 分支
0x40a0ac  orr    v25, v13, v25     ; 块1 error |=
0x40a0bc  rbit   x4, x19           ; 块1 bit_indexer 串行链开始
0x40a0c4  clz    x16, x4           ; ... 期间块2 cmeq/addp/umaxv 交错发射
0x40a2d8  umaxv  b2, v2.16b        ; 块2 is_ascii 被块1 bit_indexer 掩盖
```

两个机制：
1. **寄存器溢出消除（主）**：`in_1` 在 `scanner.next(in_2)` 前被消费，spill 261→42。
2. **ILP 掩盖（次）**：块1 的 bit_indexer 串行链（~30 条）与块2 的 `umaxv`（~5c）重叠。

重排正确性：`scanner.next()` 与 `this->next()` 状态集不相交，唯一耦合是按值传递的 `block`，因此交错安全。

## 3. 案例 C：缓冲区地址对齐

症状：Top-down/PMU 出现 `LD_CANCEL_MISALIGN`（`r5091`）与 load stall；`ldp q,q` 跨 64B 缓存行时指令无法及时发射。

处置（`align64.patch` 形态）：

- `padded_string-inl.h` 新增 `aligned_alloc_padded`：POSIX 用 `posix_memalign(&p, 64, size)`，无 POSIX 时用 `aligned_alloc(64, 向上取整)`，MSVC 用 `_aligned_malloc(size, 64)`。
- 新增 `free_padded_buffer` / `padded_buffer_deleter`；`padded_string` 的析构、移动赋值、`dom::parser::loaded_bytes` 全部改用新释放函数。
- 注释明确"不得用 `delete[]` 释放对齐分配"。

验证点：`LD_CANCEL_MISALIGN` 下降；`padded_string` 生命周期测试；非对齐起点解析正确。

## 4. 案例 D：store→load forwarding failure

取证路径：

1. ARM SPE 采 LD/ST，按 PC 聚合 `total_lat`。
2. 找出显著高于同函数其它 load 的 16B `ldr q`（从栈读回）。
3. `objdump -d` 对照该 PC 前面：GCC 将连续数组写（如 `bit_indexer::write` 的 `this->tail[i] = idx + lz`）批量成 `str q2,[x23,#N]`；随后从同一/相邻栈区读回 16B 时，store 与 load 大小/地址未满足前递条件 → 前递失败，load 必须等 store 提交到 L1，延迟骤增。
4. `addr2line -f -C -i` 映射回源码，确认回读的是哪个数组/局部变量。

处置优先级：
1. 消除回读（在寄存器中继续使用同一值，或调整生命周期让读不在同一窗口）。
2. 拆分批量写（阻止 GCC 把标量写合并成 NEON）或调整数组布局/对齐。
3. 把 16B 回读改成标量读，避免与批量写形成跨宽度前递。

必须用 SPE 证据支撑；没有高延迟 LD 证据时不改。

## 5. 编码索引（源码位置）

| 组件 | 文件:行 |
|---|---|
| stage1 入口 | `src/arm64.cpp:115` `index<64>` |
| step 特化 | `src/generic/stage1/json_structural_indexer.h:231`、`json_minifier.h` |
| `next()` | `src/generic/stage1/json_structural_indexer.h:239` |
| UTF-8 分叉 | `src/generic/stage1/utf8_lookup4_algorithm.h:173` |
| `is_ascii` | `src/arm64.cpp:94` |
| classify | `src/arm64.cpp:40` |
| bit_indexer | `src/generic/stage1/json_structural_indexer.h:93` |
| 对齐分配 | `include/simdjson/padded_string-inl.h` `allocate_padded_buffer` |
| SIMD 基础 | `include/simdjson/arm64/simd.h` `reduce_or`(:436)、`max_val`(:198) |
