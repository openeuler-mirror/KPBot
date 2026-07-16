# apply-vectorization 脚本说明

本目录按“小脚本组合”组织。当前脚本链路分成两层：

- 主工作流：环境探测、request 生成、response 物化、benchmark、编译检查
- ISA 知识层：官方资料刷新、手册生成、查询和静态校验
- micro-kernel 辅助层：寄存器预算和 spill risk 预估

## 脚本列表

### 1. `detect_isa_features.sh`

职责：

- 探测本机 `neon/sve/sme` 及扩展能力位
- 输出 `--list` 和 `--json`
- 支持 `--require neon|sve|sme`

### 2. `detect_vector_capabilities.sh`

职责：

- 兼容旧入口，不作为新流程主入口
- 直接转调 `detect_isa_features.sh`

### 3. `detect_compiler_support.py`

职责：

- 检查 `cc/clang/gcc/aarch64-linux-gnu-gcc`
- 用真实头文件和编译选项探测 `neon/sve/sme`
- 输出推荐编译器、可用性和不可用原因

### 4. `preflight_benchmark_env.py`

职责：

- 汇总 ISA 与编译器探测结果
- 给 benchmark 和 smoke compile 提供统一的 `ready / skip / unsupported` 结论
- 输出编译器路径、`arch_flags`、禁用自动向量化 flags

### 5. `refresh_arm_intrinsics_db.py`

职责：

- 抓取官方 `ACLE` 和 `Neon Intrinsics Reference`
- 尝试抓取 Arm 官方 SME Introduction / SME Instructions blog；`developer.arm.com` 返回 `403` 时记录为 reference-only，不让刷新失败
- 校验 curated whitelist 仍能在官方页面中定位
- 刷新 `references/arm_intrinsics_db/` 下的 `schema.json`、`index.json` 和各类 snapshot
- 为 `SME ZA` inline asm 增加 `kind: "instruction"` 记录，覆盖 `SMSTART`、`SMSTOP`、`ZERO`、`FMOPA`、`ST1W`、`MOVA`、`WHILELO`、`PTRUE`、`LD1W`、`FADD`、`FMLA`

示例：

```bash
python3 ./refresh_arm_intrinsics_db.py
python3 ./refresh_arm_intrinsics_db.py --output-dir /tmp/arm-db
```

### 6. `generate_arm_intrinsics_manual.py`

职责：

- 从 `references/arm_intrinsics_db/` 读取快照
- 生成 `docs/arm-intrinsics-manual/` quick-reference 代码手册
- 页面结构固定为主题表格、标准 loop pattern、common pitfalls 和 compact quick lookup
- 手册覆盖 sum/dot reduction 辅助项、SME ZA Entry Checklist 和 SME ZA Inline ASM Instructions / SGEMM ZA 模板
- 输出：
  - `README.md`
  - `neon.md`
  - `sve.md`
  - `sme.md`
  - `correctness-rules.md`

示例：

```bash
python3 ./generate_arm_intrinsics_manual.py
python3 ./generate_arm_intrinsics_manual.py \
  --db-dir /tmp/arm-db \
  --output-dir /tmp/arm-manual
```

### 7. `query_arm_intrinsics.py`

职责：

- `lookup`：按 intrinsic / instruction / attribute 做精确查询
- `search`：按名称、指令、属性或 group 做模糊查询
- curated DB 未命中时，自动回退查询 `references/arm_instruction_assets/` 中的 broad ARM 指令资产
- `validate-snippet`：对候选代码做静态 ISA 规则检查

返回约定：

- `0`：查询命中或校验通过
- `1`：查询无命中或校验发现问题
- `2`：参数错误、数据库缺失或数据库损坏

示例：

```bash
python3 ./query_arm_intrinsics.py lookup --name vld1q_f32
python3 ./query_arm_intrinsics.py lookup --instruction FMOPA --isa sme
python3 ./query_arm_intrinsics.py lookup --instruction PMULL --isa sve --json
python3 ./query_arm_intrinsics.py search --group crypto --isa sve --json
python3 ./query_arm_intrinsics.py search --group compression --isa sve --json
python3 ./query_arm_intrinsics.py search --group inline-asm --isa sme --json
python3 ./query_arm_intrinsics.py search --group predicate --isa sve --json
python3 ./query_arm_intrinsics.py validate-snippet --file ./candidate.c --isa sve --style intrinsics --json
python3 ./query_arm_intrinsics.py validate-snippet --file ./candidate_gather.c --isa sve \
  --semantic-contract '{"aliasing":"no_overlap","index_properties":["readonly","in_bounds","unique"]}' --json
python3 ./query_arm_intrinsics.py validate-snippet --style inline_asm --file ./candidate_inline.c --isa neon --json
python3 ./query_arm_intrinsics.py validate-snippet --file ./candidate_inline.c --isa neon --style inline_asm --json
python3 ./query_arm_intrinsics.py validate-snippet --style assembly --file ./candidate_kernel.S --isa neon --json
python3 ./query_arm_intrinsics.py validate-snippet --file ./candidate_kernel.S --isa neon --style assembly --json
```

### 8. `generate_vectorization_request.py`

职责：

- 生成 canonical request JSON
- 推荐使用显式 `--source-file --target-function --start-line --end-line` 模式处理外部传入源码
- 支持 `--semantic-contract-json`、`--aliasing`、`--index-property`、`--math-mode`、`--requires-bit-exact` 和 `--allows-reassociation`
- 保留 `--case <case-name>` 模式作为内部回归和旧入口兼容
- 会继续执行 schema 与路径校验

### 9. `calculate_register_budget.py`

职责：

- 根据 `--isa`、`--dtype` 和 `--shape MxN` 估算 micro-kernel 寄存器预算
- 输出可用向量寄存器、accumulator 数量、load/temporary 寄存器需求、`vector/predicate/gpr/za_tile` 分类预算和 `spill_risk`
- 支持 `--json` 供 pipeline 解析

示例：

```bash
python3 ./calculate_register_budget.py --isa neon --dtype float32 --shape 8x4
python3 ./calculate_register_budget.py --isa sve --dtype float32 --shape 6x8 --vector-bits 256 --json
```

### 10. `select_register_allocation.py`

职责：

- 在代码生成前枚举 micro-kernel MxN 候选，并按 `vector/predicate/gpr/za_tile` 分类预算评分
- 默认按可验证 throughput score 选择候选；`intrinsics` 默认最高 `medium` 风险，`inline_asm` / `assembly` 可选择 `high`
- 输出 `register_allocation_plan`、`candidate_register_allocations`、`selected_register_allocation`、`fallback_register_allocations`、`underutilization_risk`、`verification_required` 和 `verification_actions`
- 支持 `--shape-candidates`、`--n-candidates`、`--codegen-style`、`--kernel-kind`、`--addressing-mode`、`--uses-za-tile` 和 `--max-spill-risk low|medium|high`

示例：

```bash
python3 ./select_register_allocation.py --isa sve --dtype float32 --n 8 --m-candidates 4,8,12,16,20,24 --max-spill-risk medium --json
python3 ./select_register_allocation.py --isa sve --dtype float32 --shape-candidates 8x8,12x8,16x8 --json
python3 ./select_register_allocation.py --isa neon --dtype float32 --n 4 --codegen-style inline_asm --json
```

### 11. `materialize_vectorization_result.py`

职责：

- 先校验 request，再校验 `vectorization_result`
- 要求 `success=true` 且 `vectorized_code` 非空
- 将 `response JSON` 物化为可编译源码
- 支持 `codegen_style=auto|intrinsics|inline_asm|assembly`
- 支持 `replacement_kind=full_function|function_body|loop_body|translation_unit`
- 在 summary 中保留 `application_mode=materialize_to_generate|inplace_replace`
- 支持 `artifacts`，会把 C wrapper 旁边的 `.S/.s/.asm` 多文件产物写出

### 12. `benchmark.sh`

职责：

- 运行内部 fixture 级 benchmark，不代表目标项目真实收益
- 通过 `preflight_benchmark_env.py` 先判断当前机器能否跑

### 12. `benchmark_real_source.sh`

职责：

- 运行单个 case 的 before/after benchmark
- 主入口可以是 `--case <case-name> --arch <neon|sve|sme>`，也可以是显式外部源码模式
- 显式外部源码模式使用 `--source-file --driver-file --target-function --request-json --response-json --output-dir`
- 自动生成并校验 request JSON
- 默认从 `generate/<case>_<arch>_response.json` 读取候选响应
- 默认把物化源码写到 `generate/<case>_<arch>_generated.c`
- 如果 response 带 assembly artifacts，会把 `.S/.s/.asm` 编译成对象并链接进 optimized driver

### 13. `benchmark_before_after.sh`

职责：

- 汇总 `generate/` 目录中已有的 case 响应
- 支持 `--arch`、`--cases case1,case2` 和 `--generate-dir <dir>`
- 搭配 `--source-file --driver-file --target-function` 可汇总外部源码目录下已有 response
- 不会凭空生成候选结果；没有 response JSON 就不跑

### 14. `benchmark_model_response.sh`

职责：

- 便捷消费某个候选 `response JSON`
- 内部仍复用 `benchmark_real_source.sh`
- 不再做参考性能门槛比较，只输出正确性和性能结果

### 15. `test_compile.sh`

职责：

- 对 scalar、driver 和共享头做 host-side smoke compile
- 如果 `generate/` 中存在某架构的运行时产物，再结合 preflight 对这些产物做该架构下的 smoke compile
- `--compile-only` 使用编译器支持探测得到的架构 flags，不要求本机可运行该 ISA，适合 SVE 语法级检查
- 同时检查 `*_generated.c` 和 `.S/.s/.asm` artifacts

## 推荐调用顺序

### A. 先看官方结构化资料

```bash
python3 ./query_arm_intrinsics.py lookup --name svwhilelt_b32
python3 ./query_arm_intrinsics.py search --group za --isa sme
python3 ./query_arm_intrinsics.py search --group inline-asm --isa sme --json
python3 ./query_arm_intrinsics.py search --group crypto --isa sve --json
```

### B. 如需更新知识库和手册

```bash
python3 ./refresh_arm_intrinsics_db.py
python3 ./generate_arm_intrinsics_manual.py
```

### C. 只做本地环境检查

```bash
./detect_isa_features.sh --list
python3 ./detect_compiler_support.py --json
python3 ./preflight_benchmark_env.py --arch neon --json
```

### C2. 估算 micro-kernel 寄存器预算

```bash
python3 ./calculate_register_budget.py --isa neon --dtype float32 --shape 8x4
```

### D. 生成 request 并等待模型产出 response

```bash
python3 ./generate_vectorization_request.py \
  --source-file ./kernel.c \
  --target-function kernel \
  --start-line 10 \
  --end-line 18 \
  --arch neon \
  --data-type float32 \
  --aliasing no_overlap \
  --output ./generate/kernel_neon_request.json
# 在 Codex / subagent 中用 $apply-vectorization 生成 response JSON
```

### E. 校验候选代码片段

```bash
python3 ./query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_kernel.c \
  --isa sve \
  --style intrinsics \
  --json

python3 ./query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_inline.c \
  --isa neon \
  --style inline_asm \
  --json

python3 ./query_arm_intrinsics.py \
  validate-snippet \
  --file ./candidate_kernel.S \
  --isa neon \
  --style assembly \
  --json
```

SVE 机器不可用但编译器支持 SVE 时，可只做编译级检查：

```bash
./test_compile.sh --arch sve --compile-only --source ./candidate_kernel.c
```

### F. 物化并跑单个 case 或外部源码

```bash
./benchmark_real_source.sh --case <case-name> --arch neon

./benchmark_real_source.sh \
  --arch neon \
  --source-file ./kernel.c \
  --driver-file ./kernel_driver.c \
  --target-function kernel \
  --request-json ./generate/kernel_neon_request.json \
  --response-json ./generate/kernel_neon_response.json \
  --output-dir ./generate
```

### G. 汇总 `generate/` 中已有的候选结果

```bash
./benchmark_before_after.sh --arch neon

./benchmark_before_after.sh \
  --arch neon \
  --generate-dir ./generate \
  --source-file ./kernel.c \
  --driver-file ./kernel_driver.c \
  --target-function kernel
```

### H. 直接消费一个候选 response JSON

```bash
./benchmark_model_response.sh \
  --case <case-name> \
  --arch neon \
  --response-json ./candidate_response.json
```
