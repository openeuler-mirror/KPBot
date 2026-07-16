# apply-vectorization

一个面向 ARM 代码级优化的向量化 skill 目录，用于评估标量 C/C++ 循环能否安全映射到 `NEON`、`SVE`、`SME`，并输出 canonical `vectorization_result`。当前目录已经补齐“官方 HTML 抓取 -> 结构化 JSON 快照 -> 多级索引手册 -> 查询/校验 CLI”的知识链路，供 `apply-vectorization` 主流程和人工排查共同复用。

## 当前状态

- skill 工作流仍固定为：先探测本地环境，再校验 request，再判断循环能否优化，最后才查询 ISA 资料并生成结果
- `codegen_style=auto` 会按源码形态自动选择 `intrinsics`、`inline_asm` 或 `assembly`
- `replacement_kind` 和 `application_mode` 已纳入结果契约，避免完整函数、函数体片段和循环行段在 pipeline 中混用
- C/C++ 标量源码默认走 intrinsics；C/C++ 中已有 `asm/__asm__` 时进入 inline asm 优化；`.S/.s/.asm` 进入 assembly artifacts
- 官方 ISA 资料已经结构化到 `references/arm_intrinsics_db/`
- broad ARM 指令资产已经复制到 `references/arm_instruction_assets/`，用于 curated DB 未覆盖时查询 SVE 压缩/加密相关指令
- quick-reference 代码手册已经由 JSON 快照自动生成到 `docs/arm-intrinsics-manual/`
- 手册输出按 `NEON`、`SVE / SVE2`、`SME / SME2` 分页，采用类似代码手册的主题表格、loop pattern、common pitfalls 和 quick lookup 结构
- `query_arm_intrinsics.py` 可以直接查 intrinsic / instruction / attribute，并对候选代码片段做静态 ISA 规则校验
- `materialize_vectorization_result.py` 支持 `artifacts`，可物化 C wrapper + standalone `.S` 多文件产物
- `benchmark_real_source.sh` 支持显式 `--source-file --driver-file --target-function --request-json --response-json --output-dir` 外部源码模式
- `benchmark_before_after.sh` 支持 `--generate-dir` 汇总外部源码目录中的候选结果
- `refresh_arm_intrinsics_db.py` 抓取 `arm-software.github.io/acle` 的稳定 HTML，并尝试抓取 Arm 官方 SME Introduction / SME Instructions 页面；`developer.arm.com` 返回 `403` 时保留为 reference-only 来源，不让刷新失败
- 仓库不提交 request/response/generated 运行时产物；这些仍写到对应源码目录下的 `generate/`
- 待优化源码视为每次任务由用户或上游流程传入，主流程通过 request 中的 `loop_info.file_path` 定位真实源码，不把仓库内部回归样例作为面向用户的项目能力
- 首版支持 `sum` / `dot` reduction；prefix scan、严格 bit-exact 浮点归约和无法证明溢出语义的整型归约仍拒绝
- `SME ZA/tile` 只在 GEMM、rank-k 或 outer-product 风格二维块累加语义明确时进入；普通逐元素、masked 逐元素、reduction 和 GEMV 默认不进入 ZA
- `SME ZA/tile` standalone 默认路径是 `__arm_streaming` + inline asm，模板覆盖 `smstart za`、`zero {za}`、`fmopa`、`mova`、`st1w`、`smstop za`
- `SME ZA/tile` 生成结果现在要求同时通过编译、链接、汇编扫描和未解析符号扫描；standalone 代码不得默认依赖 `__arm_new("za")` 触发的 SME ABI runtime，除非目标环境已经验证提供相应 support routines
- `SVE` 通算场景现在采用保守静态规则：压缩/加密只能在固定块、独立 block、bit-exact、`svcntb/h/w/d` 宽度匹配和 feature gate 可证明时进入成功路径
- 计算密集型 kernel 支持 register accumulation / micro-kernel 设计规则，覆盖 GEMM、卷积、filter、矩阵分解、归约和 Stencil 的寄存器累加场景
- `calculate_register_budget.py` 可估算 tile shape 的 accumulator、临时寄存器需求、分类寄存器预算和 spill risk，供 micro-kernel 设计和 verify 阶段联动
- `select_register_allocation.py` 是 pre-codegen 寄存器分配策略器，会枚举候选 tile，按可验证 throughput score 选择方案，并输出 fallback chain，避免 safe but underutilized 或 intrinsics 极限压力的 micro-kernel
- 内部回归样例覆盖 BLAS 风格 L1/L2/L3 接口，用于验证 request 生成、物化和 benchmark 链路
- 外部源码算子的三路性能对比方法和典型结论收敛到 `docs/operator-patterns.md`

## 当前目录

```text
apply-vectorization/
├── SKILL.md
├── README.md
├── assets/
├── docs/
│   └── arm-intrinsics-manual/
├── references/
│   ├── arm_intrinsics_db/
│   └── arm_instruction_assets/
└── scripts/
```

## 分发版说明

本目录是可复制到目标项目 `.claude/skills/apply-vectorization/` 的完整 skill 分发版。它保留运行和使用所需的：

- `SKILL.md`
- `README.md`
- `docs/`
- `references/`
- `scripts/`
- `assets/`

它不包含 `tests/`、`evals/`、`.agents/`、`.claude/`、`.opencode/` 这些仓库回归或兼容入口材料。使用时直接复制整个 `apply-vectorization` 目录到目标项目的 `.claude/skills/` 下即可，脚本和文档都按本目录相对路径工作。

## 结构化知识库

固定快照目录：

- `references/arm_intrinsics_db/schema.json`
- `references/arm_intrinsics_db/index.json`
- `references/arm_intrinsics_db/neon.json`
- `references/arm_intrinsics_db/sve.json`
- `references/arm_intrinsics_db/sme.json`
- `references/arm_intrinsics_db/attributes.json`

当前快照覆盖：

- `NEON` 常用固定宽度 load/store/add/fma/broadcast
- `NEON` horizontal reduction，例如 `vaddvq_f32` / `vaddvq_s32`
- `SVE` 常用长度无关 loop-step / predicate / load/store / add / fma / reduction
- `SVE2` 常用 dot-product
- broad ARM instruction assets 作为 fallback，可查询 PMULL、AES、SM4、EOR、BSL、TBL、XAR、HISTCNT 等 SVE 通算相关指令
- `SME` streaming、ZA ownership、`svzero_za()`、`svmopa_*`
- `SME` inline asm instruction records，例如 `SMSTART`、`SMSTOP`、`ZERO`、`FMOPA`、`ST1W`、`MOVA`、`WHILELO`、`PTRUE`、`LD1W`、`FADD`、`FMLA`
- `SME2` 常用 ZA slice write/add
- 静态校验规则：尾处理、长度无关、谓词覆盖、头文件、streaming、ZA ownership、`svzero_za()`、SME ZA inline asm 完整性、SME ABI stub 禁止项、跨 ISA 混用、`codegen_style` 形态匹配、inline asm clobber 和 standalone assembly shape

## 快速开始

### 1. 检查本机能力

```bash
bash scripts/detect_isa_features.sh --list
python3 scripts/detect_compiler_support.py --json
python3 scripts/preflight_benchmark_env.py --arch neon --json
```

### 2. 查询官方 ISA 条目

```bash
python3 scripts/query_arm_intrinsics.py lookup --name vld1q_f32
python3 scripts/query_arm_intrinsics.py lookup --instruction FMOPA --isa sme
python3 scripts/query_arm_intrinsics.py lookup --instruction SMSTART --isa sme --json
python3 scripts/query_arm_intrinsics.py lookup --instruction ST1W --isa sme --json
python3 scripts/query_arm_intrinsics.py lookup --instruction PMULL --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group crypto --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group compression --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group inline-asm --isa sme --json
python3 scripts/query_arm_intrinsics.py search --group za --isa sme --json
```

### 3. 校验候选代码片段

```bash
python3 scripts/query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_kernel.c \
  --isa sve \
  --style intrinsics \
  --json
```

SVE gather/scatter 或压缩/加密候选可以带语义证明做静态校验：

```bash
python3 scripts/query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_sve.c \
  --isa sve \
  --semantic-contract '{"aliasing":"no_overlap","index_properties":["readonly","in_bounds","unique"]}' \
  --json
```

只做编译级 SVE 检查时使用 compile-only；这不代表本机可运行 SVE benchmark：

```bash
bash scripts/test_compile.sh \
  --arch sve --compile-only --source ./candidate_sve.c
```

也可以显式检查 inline asm 或 standalone assembly：

```bash
python3 scripts/query_arm_intrinsics.py \
  validate-snippet --file ./candidate_inline.c --isa neon --style inline_asm --json
python3 scripts/query_arm_intrinsics.py \
  validate-snippet --file ./candidate_kernel.S --isa neon --style assembly --json
```

### 4. 刷新快照并重建 quick-reference 手册

```bash
python3 scripts/refresh_arm_intrinsics_db.py
python3 scripts/generate_arm_intrinsics_manual.py
```

### 5. 为外部源码生成 request JSON

```bash
python3 scripts/generate_vectorization_request.py \
  --source-file <source-file> \
  --target-function <function-name> \
  --start-line <loop-start-line> \
  --end-line <loop-end-line> \
  --arch neon \
  --data-type float32 \
  --aliasing no_overlap \
  --output <output-path>
```

### 6. 物化并跑单个候选响应

```bash
bash scripts/benchmark_real_source.sh \
  --arch neon \
  --source-file <source-file> \
  --driver-file <driver-file> \
  --target-function <function-name> \
  --request-json <source-dir>/generate/<name>_neon_request.json \
  --response-json <source-dir>/generate/<name>_neon_response.json \
  --output-dir <source-dir>/generate
```

## 官方来源策略

当前固定来源基线：

- [Arm C Language Extensions (ACLE)](https://arm-software.github.io/acle/main/acle.html)
- [Arm Neon Intrinsics Reference](https://arm-software.github.io/acle/neon_intrinsics/advsimd.html)
- [Arm SIMD: Optimize, Migrate, and Accelerate C/C++ for Peak Performance](https://developer.arm.com/servers-and-cloud-computing/arm-simd)
- [Scalable Matrix Extension: Expanding the Arm Intrinsics Search Engine](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/scalable-matrix-extension-expanding-the-arm-intrinsics-search-engine)
- [Part 1: Arm Scalable Matrix Extension (SME) Introduction](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction)
- [Part 2: Arm Scalable Matrix Extension (SME) Instructions](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction-p2)
- [DDI0602 SME Instructions](https://developer.arm.com/documentation/ddi0602/latest/SME-Instructions)
- [DDI0602 SVE Instructions](https://developer.arm.com/documentation/ddi0602/latest/SVE-Instructions)

抓取约束：

- 自动化刷新解析 `ACLE` 和 `Neon Intrinsics Reference`
- Arm 官方 SME blog 页面按可选来源抓取；如果 `developer.arm.com` 返回 `403`，保留链接和引用元数据
- DDI0602 指令页作为 authoritative reference-only URL 保留

## 文档

- `SKILL.md`
- `scripts/README.md`
- `docs/arm-isa-usage-guide.md`
- `docs/sve-general-compute-guide.md`
- `docs/arm-official-source-map.md`
- `docs/sme-za-inline-asm-guide.md`
- `docs/codegen-style-guide.md`
- `docs/arm-intrinsics-manual/README.md`
- `docs/reduction-guide.md`
- `docs/test-and-benchmark.md`
- `docs/local-capability-detection.md`
- `docs/skill-interaction.md`
- `docs/sme-za-tile-guide.md`
- `docs/operator-patterns.md`
- `docs/register-accumulation-guide.md`
- `docs/microkernel-design-guide.md`
