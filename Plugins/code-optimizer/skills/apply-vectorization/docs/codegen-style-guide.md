# codegen_style 自动选择指南

## 目标

`apply-vectorization` 支持三种代码生成形态：

- `intrinsics`：C/C++ translation unit，使用 NEON/SVE/SME intrinsics 或函数属性。
- `inline_asm`：C/C++ translation unit，保留 `asm/__asm__` 形式并优化汇编块。
- `assembly`：多文件产物，通常是 C wrapper 加 standalone `.S/.s/.asm` 汇编 artifact。

request 中的 `codegen_style` 默认为 `auto`。默认选择规则是：

简写口径：`codegen_style=auto` 表示按源码形态自动选择目标语言。

| 输入源码形态 | auto 选择 | 说明 |
| --- | --- | --- |
| C/C++ 标量循环 | `intrinsics` | 默认路径，优先可读、可验证和可移植 |
| C/C++ 中已有 `asm/__asm__` | `inline_asm` | 不直接拒绝，进入 inline asm 优化和 clobber/ABI 检查 |
| `.S/.s/.asm` | `assembly` | 不退回 C intrinsics，输出 standalone assembly artifacts |

## 输入契约

可选字段：

```json
{
  "codegen_style": "auto|intrinsics|inline_asm|assembly"
}
```

兼容字段：

- `optimization_level: "intrinsics"` 等价于显式 `codegen_style: "intrinsics"`。
- `optimization_level: "asm"` 只映射到 `inline_asm`，不表示 standalone assembly。
- 如果同时提供 `codegen_style` 和 `optimization_level`，以 `codegen_style` 为准。

不匹配时必须拒绝：

- `.S/.s/.asm` 输入显式要求 `intrinsics` 或 `inline_asm`。
- 含 `asm/__asm__` 的 C/C++ 输入显式要求 `intrinsics`。
- 非 AArch64 GAS/Clang 风格汇编方言无法确认 ABI 和指令语义。

## 输出契约

`vectorized_code` 继续承载主 C/C++ translation unit。对于 `assembly`，它通常是 wrapper：

```c
void add_arrays_f32_asm(const float *a, const float *b, float *out, int n) {
    extern void add_arrays_f32_asm_kernel(const float *, const float *, float *, int);
    add_arrays_f32_asm_kernel(a, b, out, n);
}
```

standalone 汇编通过 `artifacts` 输出：

```json
{
  "path_suffix": "add_arrays_f32_asm_kernel.S",
  "language": "assembly",
  "role": "optimized_kernel",
  "content": ".text\n.globl add_arrays_f32_asm_kernel\n..."
}
```

`artifacts` 规则：

- `path_suffix` 必须是相对路径，不能包含 `..`。
- `language` 使用 `c`、`c_header`、`asm` 或 `assembly`。
- `.S` artifact 必须定义可链接符号，并遵守 AAPCS64 调用约定。
- AArch64 汇编不得把 `x18/w18` 当普通临时寄存器；它在 Darwin/Apple arm64 等平台是平台保留寄存器。
- `safety_checks` 必须记录最终选择的 `codegen_style` 和原因。

替换粒度规则：

- `replacement_kind=full_function`：`vectorized_code` 是完整目标函数。
- `replacement_kind=translation_unit`：`vectorized_code` 是完整可编译 translation unit。
- `replacement_kind=function_body`：`vectorized_code` 只替换目标函数体内部。
- `replacement_kind=loop_body`：`vectorized_code` 只替换 `loop_info.start_line/end_line` 覆盖的循环行段。

`assembly` 形态通常应使用 `full_function` 或 `translation_unit` 的 C wrapper，并把汇编内核放入 `artifacts`。下游 pipeline 必须按 `replacement_kind` 应用，不能把完整 wrapper 插入原始循环行段。

## 验证

静态检查：

```bash
python3 scripts/query_arm_intrinsics.py validate-snippet --file candidate.c --isa neon --style intrinsics --json
python3 scripts/query_arm_intrinsics.py validate-snippet --file candidate_inline.c --isa neon --style inline_asm --json
python3 scripts/query_arm_intrinsics.py validate-snippet --file candidate_kernel.S --isa neon --style assembly --json
```

物化检查：

```bash
python3 scripts/materialize_vectorization_result.py \
  --request-json request.json \
  --response-json response.json \
  --output-source generate/case_neon_generated.c
```

`materialize_vectorization_result.py` 会根据源码后缀和内容推导 `auto` 的实际形态，按 `replacement_kind` 物化源码，写出 `artifacts`，并在 summary 中列出所有输出文件。

编译检查：

- `test_compile.sh` 会编译 `generate/` 下的 `*_generated.c` 和 `.S/.s/.asm` 产物。
- `benchmark_real_source.sh` 会把 `.S` artifact 编译成对象并链接到 optimized driver。
- 对 SME ZA 路径仍必须做汇编扫描和 `nm -u` 未解析符号扫描。

## 选择原则

- C/C++ 标量输入默认生成 `intrinsics`，不主动升级到汇编。
- 已有 intrinsics 代码默认进入评审或局部改进，不退回标量。
- 已有 inline asm 进入 `inline_asm`，重点检查约束、clobber、尾处理和 ABI。
- standalone assembly 进入 `assembly`，重点检查符号、调用约定、callee-saved 寄存器、目标 ISA、尾处理和链接边界。
- 在 `inline_asm` 和 `assembly` 中优先使用 `x9-x17` 等普通 caller-saved 临时寄存器；避免 `x18/w18`，除非目标平台 ABI 明确允许且已记录理由。
