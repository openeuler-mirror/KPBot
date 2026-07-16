# NEON / SVE / SME 使用入口

本文件现在只负责两件事：

- 给 `apply-vectorization` 提供 ISA 决策总则
- 指向结构化手册和查询脚本，而不是继续手写大量 per-intrinsic 说明

具体 intrinsic / attribute / rule 细项已经迁到：

- `docs/arm-intrinsics-manual/README.md`
- `scripts/query_arm_intrinsics.py`
- `references/arm_intrinsics_db/`

## 1. 先查什么

进入代码生成前，先用结构化入口定位目标项：

```bash
python3 scripts/query_arm_intrinsics.py lookup --name vld1q_f32
python3 scripts/query_arm_intrinsics.py lookup --instruction FMOPA --isa sme
python3 scripts/query_arm_intrinsics.py lookup --instruction PMULL --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group crypto --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group compression --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group inline-asm --isa sme --json
python3 scripts/query_arm_intrinsics.py search --group predicate --isa sve --json
```

如果要检查候选代码是否符合当前 skill 的规则：

```bash
python3 scripts/query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_kernel.c \
  --isa sve \
  --style intrinsics \
  --json
```

## 2. 决策总则

无论 `neon/sve/sme`，都先满足三类前提：

- 连续或可证明安全的 unit-stride 访存
- 没有跨迭代依赖、不可证明安全的别名和副作用
- 按源码形态完成 `codegen_style=auto` 分类：C/C++ 标量默认 `intrinsics`，已有 `asm/__asm__` 默认 `inline_asm`，`.S/.s/.asm` 默认 `assembly`

以下情况直接拒绝：

- gather/scatter、间接寻址、输出冲突
- 原子、锁、I/O
- 需要跨迭代保留状态
- 当前目标架构下没有清晰映射的类型或操作
- 显式 `codegen_style` 与源码形态冲突，或汇编 ABI / clobber / 符号边界无法证明

架构选择原则：

- `neon`：固定 128-bit，模板简单，适合通用 ARMv8 SIMD 优化
- `sve`：向量长度可变，优先写长度无关循环，适合谓词、尾部和部分 gather/scatter 场景
- `sme`：在 streaming 模式下扩展 SVE；普通逐元素循环优先 streaming-compatible，只有矩阵或 tile 结构足够清晰时再考虑 ZA/tile
- GEMM、卷积、filter、矩阵分解、归约和 Stencil 属于计算密集型 kernel；进入代码生成前还要读 `docs/register-accumulation-guide.md` 和 `docs/microkernel-design-guide.md`，并用 `select_register_allocation.py` 选择可验证 throughput score 最高且非 `spill-likely` 的 tile

官方使用原则摘要：

- `NEON` 通过 `<arm_neon.h>` 暴露固定宽度 Advanced SIMD intrinsics
- `SVE` 通过 `<arm_sve.h>` 暴露长度无关的 sizeless 类型和谓词化 intrinsics
- `SME` 通过 `<arm_sme.h>` 暴露 streaming、ZA、tile 相关扩展；是否使用 ZA/tile 由循环语义决定
- `SME` 的函数属性和 ZA 状态所有权属于 ABI 约束，不是代码风格问题

## 3. 按 ISA 选模板

### 3.1 NEON

- 固定 128-bit，适合主向量循环 + 显式标量尾处理
- 先查：`docs/arm-intrinsics-manual/neon.md`
- 典型条目：`vld1q_f32`、`vst1q_f32`、`vdupq_n_f32`、`vaddq_f32`、`vfmaq_f32`

常见 lane 数：

| 数据类型 | Lane 数 | 常见加载 | 常见存储 |
| --- | --- | --- | --- |
| `float32` / `int32` / `uint32` | 4 | `vld1q_f32` / `vld1q_s32` / `vld1q_u32` | `vst1q_f32` / `vst1q_s32` / `vst1q_u32` |
| `int16` / `uint16` | 8 | `vld1q_s16` / `vld1q_u16` | `vst1q_s16` / `vst1q_u16` |
| `int8` / `uint8` | 16 | `vld1q_s8` / `vld1q_u8` | `vst1q_s8` / `vst1q_u8` |

关键要求：

- 循环步长与 lane 数一致
- 标量尾处理必须可见，除非 trip count 已被上游严格证明为整除
- NEON inline asm 和 standalone assembly 参考 `docs/neon-asm-patterns.md`，assembly artifacts 必须遵守 AAPCS64

### 3.2 SVE / SVE2

- 长度无关，优先查 `docs/arm-intrinsics-manual/sve.md`
- 典型条目：`svcntw`、`svwhilelt_b32`、`svld1_f32`、`svst1_f32`、`svmla_f32_x`
- `SVE2` 常用扩展当前只收录向量化直接相关项，例如 `svdot_s32`
- 压缩/加密通算先读 `docs/sve-general-compute-guide.md`；curated DB 未命中时，查询 `references/arm_instruction_assets/` 的 broad instruction asset 只能作为 reference-only 证据

关键要求：

- 用 `svcnt*()` 驱动循环步长
- 用 `svwhilelt_*` 生成谓词
- 对 predicated load/store 和 predicated compute 保持同一谓词上下文
- `svcntb/h/w/d` 必须匹配数据类型宽度和指针步长
- gather/scatter 必须由 `semantic_contract` 证明索引只读、边界内、别名安全；scatter 或 read-modify-write scatter 还必须证明索引唯一
- 压缩 LZ match copy、变量长度 token parser、crypto 跨 block 依赖、未证明变量移位或有符号溢出的 bit-exact 路径必须拒绝
- sum/dot reduction 使用向量累加器加最终 `svaddv_*` 水平归约；浮点路径必须说明非 bit-exact

### 3.3 SME / SME2

- 先分清 `streaming` 与 `ZA/tile`
- 先查：`docs/arm-intrinsics-manual/sme.md`
- 辅助决策：`docs/sme-za-tile-guide.md`
- standalone ZA inline asm 主参考：`docs/sme-za-inline-asm-guide.md`

关键要求：

- 普通逐元素逻辑默认优先 `__arm_streaming` 或 `__arm_streaming_compatible`
- 普通逐元素、masked 逐元素、sum/dot reduction、GEMV 默认不进入 ZA/tile
- 只有 GEMM、rank-k、outer-product 语义、ZA ownership、tile/tile-slice 映射都明确时，才进入真正的 ZA/tile 路径
- standalone ZA/tile 默认用 `__arm_streaming` + inline asm 管理 `smstart za`、`zero {za}`、`fmopa`、`st1w`、`smstop za`
- `SME2` 条目只收录和 slice / ZA 写入直接相关的常用项，不扩成全量手册

## 4. 什么时候必须停

以下场景即使用户点名某个 ISA，也不能硬做：

- `NEON`：需要长度无关循环或复杂遮罩时
- `SVE`：代码仍是固定宽度心智模型，只是把名字换成了 `sv*`
- `SME`：只是普通逐元素逻辑，却强行要求 `ZA/tile`
- `SME2`：只是想“更高级一点”，但没有 slice / tile 级数据流设计
- reduction：是 prefix scan、严格 bit-exact 浮点归约，或无法证明溢出语义的整型归约

## 5. 与旧文档的关系

本文件保留为入口说明；当前权威细项顺序是：

1. `references/arm_intrinsics_db/`
2. `docs/arm-intrinsics-manual/`
3. `scripts/query_arm_intrinsics.py`
4. `docs/sme-za-tile-guide.md`
5. `docs/sme-za-inline-asm-guide.md`
6. `docs/sve-general-compute-guide.md`
7. `docs/arm-official-source-map.md`
8. `docs/codegen-style-guide.md`
9. `docs/register-accumulation-guide.md`
10. `docs/microkernel-design-guide.md`
