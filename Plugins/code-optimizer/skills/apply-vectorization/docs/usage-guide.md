# apply-vectorization 使用指南

## 1. 目标

本指南说明三件事：

- 如何在 Codex、Claude Code、OpenCode 中触发 `apply-vectorization`
- 如何先校验环境和 request，再决定是否向量化
- 如何把模型生成的 `response JSON` 接到仓库脚本，完成物化和本地 benchmark

## 2. 先理解边界

### 2.1 模型测试

模型测试关心：

- request JSON 是否合理
- `$apply-vectorization` 是否按固定顺序执行：先环境探测，再 request 校验，再可优化性判定，再读 ISA 手册
- 对复杂拒绝场景是否真的拒绝

### 2.2 本地 benchmark 测试

本地 benchmark 测试关心：

- `response JSON` 是否通过 request + schema 校验
- `vectorized_code` 能否物化为可编译源码
- before/after benchmark 是否能在同一 driver、同一输入、同一口径下运行

### 2.3 模型调用 benchmark

模型调用 benchmark 现在只表示：

- 你已经有一个候选 `response JSON`
- 仓库脚本负责物化和跑统一 benchmark
- `benchmark_model_response.sh` 只输出正确性和性能结果，不再比较仓库内参考性能门槛

## 3. 触发 skill

### 3.1 Codex

在 Codex 会话中直接引用：

```text
$apply-vectorization
```

推荐把这些信息一起给模型：

- 目标函数名
- 文件路径
- 循环起止行
- `target_arch`
- `data_types`
- 可选 `codegen_style`；默认 `auto`
- 可选 `semantic_contract`；用于说明别名、索引唯一性、数学重排和 bit-exact 要求

### 3.2 subagent

如果要在 Codex 中做接近真实流程的验证，可以起 subagent：

1. 主会话先生成 `request JSON`
2. subagent 显式使用 `$apply-vectorization` 生成 canonical `response JSON`
3. 主会话保存 `response JSON`
4. 主会话再调用仓库脚本做物化和 benchmark

仓库脚本本身不直接起 subagent，这一点不变。

## 4. 生成 request JSON

推荐对用户或上游流程传入的源码使用显式参数模式：

```bash
python3 scripts/generate_vectorization_request.py \
  --source-file <source-file> \
  --target-function <function-name> \
  --start-line <loop-start-line> \
  --end-line <loop-end-line> \
  --arch neon \
  --data-type float32 \
  --aliasing no_overlap \
  --output <source-dir>/generate/<name>_neon_request.json
```

这个脚本会：

- 校验源码路径和循环行号
- 填充 `target_function`
- 保留可选的 `body_operations`、`dependencies`
- 写入可选 `semantic_contract`，例如 `--aliasing`、`--index-property`、`--math-mode`
- 继续做 request 校验

`--case` 模式仅用于仓库内部回归和兼容旧测试入口，不作为面向用户的推荐输入方式。

## 5. 生成 response JSON

模型拿到 request 后，应先：

1. 跑 `detect_isa_features.sh`
2. 跑 `detect_compiler_support.py`
3. 跑 `preflight_benchmark_env.py --arch <target_arch>`
4. 校验 request
5. 读取源码并判断能否优化
6. 按源码形态选择 `codegen_style=auto` 的实际输出目标语言
7. 通过后先查 `scripts/query_arm_intrinsics.py` 或 `docs/arm-intrinsics-manual/`
8. 再读 `docs/arm-isa-usage-guide.md`

只要 request、环境和语义安全性通过，就继续生成显式 NEON/SVE/SME 向量化结果，并用本地物化、编译、链接和 benchmark 验证。

`codegen_style=auto` 选择规则：

- C/C++ 标量输入默认生成 `intrinsics`
- C/C++ 中已有 `asm/__asm__` 时生成或改进 `inline_asm`
- `.S/.s/.asm` standalone 汇编输入生成 `assembly` artifacts

若目标是 `sme`，再读：

- `docs/sme-za-tile-guide.md`
- 如果生成 ZA/tile standalone inline asm，再读 `docs/sme-za-inline-asm-guide.md`

若目标循环是 `sum` 或 `dot` reduction，再读：

- `docs/reduction-guide.md`

若目标是 GEMM、卷积、filter、矩阵分解、归约或 Stencil 这类计算密集型 kernel，再读：

- `docs/register-accumulation-guide.md`
- `docs/microkernel-design-guide.md`

并用寄存器分配策略器选择候选 tile：

```bash
python3 scripts/select_register_allocation.py \
  --isa sve \
  --dtype float32 \
  --n 8 \
  --m-candidates 4,8,12,16,20,24 \
  --json
```

然后才输出 canonical `response JSON`。

## 6. 物化源码

```bash
python3 scripts/materialize_vectorization_result.py \
  --request-json <source-dir>/generate/<case>_neon_request.json \
  --response-json <source-dir>/generate/<case>_neon_response.json \
  --output-source <source-dir>/generate/<case>_neon_generated.c
```

该脚本会：

- 先校验 request
- 再校验 `vectorization_result`
- 要求 `success=true`
- 要求 `vectorized_code` 非空
- 根据 `replacement_kind` 区分完整函数、函数体片段、循环行段或完整 translation unit
- 自动写出 `artifacts` 中的 `.S/.s/.asm` 多文件产物
- summary 中返回实际 `codegen_style`、`replacement_kind`、`application_mode` 和所有输出文件

## 7. 跑真实源码 benchmark

### 7.1 显式外部源码

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

默认约定：

- request JSON: `<source-dir>/generate/<case>_neon_request.json`
- response JSON: `<source-dir>/generate/<case>_neon_response.json`
- generated C: `<source-dir>/generate/<case>_neon_generated.c`
- assembly artifacts: 由 response 的 `artifacts` 写入同一 generate 目录，并参与链接

`--case` 模式仅用于内部回归和兼容旧入口；外部项目应优先使用上面的显式源码模式。

### 7.2 汇总已存在的候选结果

```bash
bash scripts/benchmark_before_after.sh --arch neon
```

也可以只跑部分 case：

```bash
bash scripts/benchmark_before_after.sh \
  --arch neon \
  --cases <case-a>,<case-b>
```

汇总外部源码目录时传入：

```bash
bash scripts/benchmark_before_after.sh \
  --arch neon \
  --generate-dir <source-dir>/generate \
  --source-file <source-file> \
  --driver-file <driver-file> \
  --target-function <function-name>
```

### 7.3 直接消费候选 response JSON

```bash
bash scripts/benchmark_model_response.sh \
  --case <case-name> \
  --arch neon \
  --response-json ./candidate_response.json
```

这个入口适合“模型调用 benchmark”场景，但它现在只做统一 benchmark，不再依赖仓库内的参考优化快照或参考性能基线。

## 8. 相关文档

- `SKILL.md`
- `docs/arm-intrinsics-manual/README.md`
- `scripts/README.md`
- `scripts/query_arm_intrinsics.py`
- `docs/test-and-benchmark.md`
- `docs/local-capability-detection.md`
- `docs/skill-interaction.md`
- `docs/arm-isa-usage-guide.md`
- `docs/codegen-style-guide.md`
- `docs/register-accumulation-guide.md`
- `docs/microkernel-design-guide.md`
- `docs/sme-za-tile-guide.md`
