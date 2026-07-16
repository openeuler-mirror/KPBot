# apply-vectorization 输入输出契约

来源依据：

- `docs/specs/detailed-design/08-specialized-optimization-skills.md`
- `docs/specs/detailed-design/02-three-layer-analysis-agents.md`
- `docs/specs/implementation-plan.md`

## 规范输入

唯一采用如下结构：

```json
{
  "target_function": "string",
  "loop_info": {
    "file_path": "string",
    "start_line": "integer",
    "end_line": "integer",
    "loop_variable": "string, optional",
    "iteration_count": "string, optional",
    "body_operations": ["string", "optional"],
    "dependencies": ["string", "optional"]
  },
  "target_arch": "neon|sve|sme",
  "vector_width": "integer, optional",
  "data_types": ["string"],
    "codegen_style": "auto|intrinsics|inline_asm|assembly, optional",
    "microkernel_hint": {
      "shape": "MxN, optional",
      "accumulation_domain": "k_loop|filter_taps|stencil_radius|reduction, optional",
      "fixed_k": "integer, optional"
    },
    "semantic_contract": {
    "aliasing": "restrict|no_overlap|unknown, optional",
    "index_properties": ["readonly|unique|in_bounds|monotonic, optional"],
    "math_mode": "strict|reassociation_allowed|fast_math_allowed, optional",
    "requires_bit_exact": "boolean, optional",
    "allows_reassociation": "boolean, optional"
  }
}
```

字段规则：

- 必须包含 `target_function`、`loop_info`、`target_arch`、`data_types`。
- `loop_info.file_path`、`loop_info.start_line`、`loop_info.end_line` 也必须存在。
- 当 `target_arch` 为 `neon` 时，`vector_width` 默认取 `128`。
- 当 `target_arch` 为 `sve` 或 `sme` 时，`vector_width` 可省略，默认采用运行时可变长度。
- `target_arch` 一旦确定，就必须按该架构返回对应的代码草案和 intrinsics 列表，不要把不同架构混写在一个结果里。
- `codegen_style` 默认是 `auto`，由源码形态决定输出目标语言。
- 旧字段 `optimization_level: "intrinsics"` 映射为 `codegen_style: "intrinsics"`；`optimization_level: "asm"` 映射为 `codegen_style: "inline_asm"`。
- `microkernel_hint` 是上游对计算密集型 kernel 的提示，不是强制结果。模型仍必须从源码确认累加域、tile shape、寄存器预算和边界处理；`microkernel_hint.shape` 只能作为寄存器分配策略器的候选之一。
- `semantic_contract` 是语义证明，不是愿望清单。未知别名、未知索引唯一性、未知浮点重排许可都必须按未知处理。
- 涉及 gather/scatter 或间接寻址时，`semantic_contract.index_properties` 至少要能证明索引只读、边界内；涉及 scatter 写时还必须证明无重复。

`codegen_style=auto` 的选择规则：

- C/C++ 标量源码默认选择 `intrinsics`。
- C/C++ 中已有 `asm/__asm__` 时选择 `inline_asm`。
- `.S/.s/.asm` standalone assembly 源码选择 `assembly`。

## 支持的数据类型

`data_types` 中优先使用以下名称：

- `float32`
- `int32`
- `uint32`
- `int16`
- `uint16`
- `int8`
- `uint8`

对于这组常见逐元素类型：

- `neon` 使用固定 lane 宽度
- `sve` 使用长度无关向量循环
- `sme` 默认使用 streaming-compatible 逐元素循环

对于首版 reduction：

- 支持 `sum` 和 `dot` 两类 reduction。
- `sum` 仅限 `acc += x[i]` 且 `acc` 只作为最终结果返回或循环后写出。
- `dot` 仅限 `acc += a[i] * b[i]` 且输入为连续 unit-stride 访问。
- `float32` reduction 必须在结果中说明向量归约会改变加法顺序，不保证 bit-exact；若 request 或 dependencies 声明需要严格顺序或 bit-exact，则拒绝。
- `int32` reduction 仅在不依赖溢出语义时允许；无法证明时拒绝。
- `out[i] = acc` 这类 prefix scan 或 running accumulator 输出不属于 reduction 支持范围，必须拒绝。

对于 SME ZA/tile：

- 普通逐元素、masked 逐元素、sum/dot reduction、GEMV 默认不进入 ZA/tile。
- 只有 GEMM、rank-k 或 outer-product 风格的二维块累加语义明确时，才允许 ZA/tile。
- 成功的 ZA/tile response 必须在 `safety_checks` 中说明 tile 对应输出块、行/列维度、K 维累加、ZA ownership、边界 predication 和写回时机。

如果循环依赖混合类型，或类型组合无法清晰映射到目标架构下的单一向量操作，则应拒绝请求。

对于 register accumulation / micro-kernel：

- GEMM、卷积、filter、矩阵分解、归约和 Stencil 这类计算密集型 kernel，必须识别累加域（K、tap、radius 或 reduction length）。
- 成功结果应让 accumulator 跨累加域保留在寄存器中，直到 tile 或输出点最终写回。
- 不能在 hot K/tap/radius 循环中反复 store partial accumulator 再 reload；若寄存器预算不足，应缩小 tile、拆分 K 或拒绝。
- 选择 micro-kernel shape 时必须先运行 `scripts/select_register_allocation.py` 枚举候选；该策略器按 `vector/predicate/gpr/za_tile` 分别建模寄存器预算，并按可验证 throughput score 选择候选。
- `intrinsics` 默认最高选择 `medium` 风险；`inline_asm` / `assembly` 可选择 `high` 风险，但必须记录 `verification_actions`。显式传入 `--max-spill-risk high` 时才允许 intrinsics 选择 high-risk 候选。
- 不允许只因为 4 accumulator 之类候选没有 spill 就停止；若存在更高分可行候选，低 accumulator 候选必须标记 `underutilization_risk=true`。
- 成功结果必须输出 `fallback_register_allocations`，用于验证阶段发现 spill、partial accumulator memory roundtrip 或 clobber/ABI 风险时自动降级。

## 规范输出

返回：

```json
{
  "vectorization_result": {
    "success": "boolean",
    "modified_file": "string",
    "original_loop": "string",
    "vectorized_code": "string",
    "codegen_style": "intrinsics|inline_asm|assembly, optional",
    "replacement_kind": "full_function|function_body|loop_body|translation_unit, optional",
    "application_mode": "materialize_to_generate|inplace_replace, optional",
    "artifacts": [
      {
        "path_suffix": "string",
        "language": "c|c_header|asm|assembly",
        "role": "string",
        "content": "string"
      }
    ],
    "accumulation_pattern": {
      "kind": "register_accumulation|none",
      "domain": "k_loop|filter_taps|stencil_radius|reduction, optional",
      "accumulators": "integer, optional",
      "kept_live_until": "string, optional",
      "memory_roundtrip_in_inner_loop": "boolean, optional"
    },
    "microkernel_shape": {
      "m": "integer, optional",
      "n": "integer, optional",
      "k_unroll": "integer, optional",
      "vector_lanes": "integer, optional",
      "accumulator_registers": "integer, optional"
    },
    "register_budget": {
      "available_vector_registers": "integer, optional",
      "needed_vector_registers": "integer, optional",
      "temporary_registers": "integer, optional",
      "spill_risk": "low|medium|high|spill-likely, optional",
      "register_class_budgets": {
        "vector|predicate|gpr|za_tile": {
          "total": "integer",
          "reserved": "integer",
          "available": "integer",
          "needed": "integer",
          "pressure_ratio": "number",
          "spill_risk": "low|medium|high|spill-likely"
        }
      }
    },
    "spill_risk": "low|medium|high|spill-likely|null, optional",
    "register_allocation_plan": {
      "success": "boolean, optional",
      "strategy": "string, optional",
      "selected_shape": "MxN, optional",
      "max_spill_risk": "low|medium|high, optional",
      "verification_required": "boolean, optional",
      "verification_actions": ["string, optional"]
    },
    "candidate_register_allocations": [
      {
        "shape": "MxN",
        "accumulator_registers": "integer",
        "pressure_ratio": "number",
        "spill_risk": "low|medium|high|spill-likely",
        "eligible": "boolean",
        "underutilization_risk": "boolean"
      }
    ],
    "selected_register_allocation": {
      "shape": "MxN, optional",
      "accumulator_registers": "integer, optional",
      "pressure_ratio": "number, optional",
      "spill_risk": "low|medium|high|spill-likely, optional"
    },
    "fallback_register_allocations": [
      {
        "shape": "MxN",
        "accumulator_registers": "integer",
        "pressure_ratio": "number",
        "spill_risk": "low|medium|high|spill-likely",
        "eligible": "boolean",
        "verification_actions": ["string, optional"]
      }
    ],
    "underutilization_risk": "boolean, optional",
    "verification_required": "boolean, optional",
    "verification_actions": ["string, optional"],
    "intrinsics_used": ["string"],
    "epilogue_handling": "string",
    "expected_speedup": "string",
    "safety_checks": ["string"],
    "error_message": "string"
  }
}
```

结果规则：

- 顶层必须始终返回 `vectorization_result`。
- 无论成功还是失败，内部 9 个字段都必须完整填充。
- 拒绝改写时，使用 `vectorized_code: ""`。
- 即使没有产出改写代码，`modified_file` 也应指向被分析的源文件路径。
- `intrinsics_used` 只列出当前目标架构对应的 intrinsics 或关键构造，例如 `__arm_streaming`。
- `codegen_style` 成功时建议填充，并且必须与源码形态和 request 约束一致。
- `replacement_kind` 明确 `vectorized_code` 的替换粒度：
  - `full_function`：完整目标函数定义，函数名和签名必须与 `target_function` 一致。
  - `function_body`：只包含目标函数体内部代码，由物化脚本复用原始函数签名。
  - `loop_body`：替换 `loop_info.start_line/end_line` 指向的循环行段。
  - `translation_unit`：完整可编译 translation unit，必须包含目标函数。
- `application_mode` 明确应用方式：
  - `materialize_to_generate`：默认路径，写入源码同级 `generate/` 目录，不覆盖原文件。
  - `inplace_replace`：只供上层 pipeline 在明确需要原地替换时使用；必须结合 `replacement_kind` 选择正确边界。
- 旧响应未填 `replacement_kind` 时，若 `vectorized_code` 看起来是完整目标函数，物化脚本按 `translation_unit` 处理；否则按 `function_body` 处理。
- `artifacts` 用于多文件产物。`assembly` 形态必须提供至少一个 `.S/.s/.asm` artifact；C wrapper 仍放在 `vectorized_code`。
- `artifacts[].path_suffix` 必须是相对路径，不能包含 `..`。
- `safety_checks` 必须说明实际选择的 `codegen_style` 和选择原因。
- `accumulation_pattern`、`microkernel_shape`、`register_budget`、`spill_risk`、`register_allocation_plan`、`candidate_register_allocations`、`selected_register_allocation`、`fallback_register_allocations`、`underutilization_risk`、`verification_required` 和 `verification_actions` 是可选扩展字段；旧消费者可忽略。
- 当目标是 GEMM、卷积、filter、矩阵分解、归约或 Stencil micro-kernel 时，成功结果应填充这些字段，或在 `safety_checks` 中解释为什么未使用 micro-kernel。

## 旧字段映射

推理前先把旧文档中的术语归一化到规范结构：

- `vectorize-loop` -> `apply-vectorization`
- `code_location.file` -> `loop_info.file_path`
- `code_location.start_line` -> `loop_info.start_line`
- `code_location.end_line` -> `loop_info.end_line`
- `function_code` -> 以 `loop_info.file_path` 为准读取文件，脱离上下文的片段仅作辅助参考
- `optimization_level: "asm"` -> `codegen_style: "inline_asm"`，不是 standalone assembly

## 校验清单

- 循环行段能从磁盘读取。
- 目标架构已明确提供，且是 `neon/sve/sme` 之一。
- `data_types` 能真实反映循环中的操作类型。
- `codegen_style` 若出现，必须是 `auto/intrinsics/inline_asm/assembly` 之一。
- 若输入是 `.S/.s/.asm`，不得显式要求 `intrinsics` 或 `inline_asm`。
- 返回 JSON 含有规范包装和全部必需字段。

## 关键规则（模型必须遵守）

### 1. target_function 必须是源码中的真实函数名

`target_function` 字段必须是 `loop_info.file_path` 中真实定义的函数名，不能使用带后缀的描述性名称。

错误示例：
- `cblas_sgemv_row_dot`（错误：这是对循环的描述，不是真实函数名）
- `cblas_sgemm_inner_col`（错误：这是内层循环的描述）

正确示例：
- `cblas_sgemv`（正确：源码中真实存在的函数）
- `cblas_sgemm`（正确：源码中真实存在的函数）

### 2. vectorized_code 必须使用原始函数名和完整签名

当 `replacement_kind` 是 `full_function` 或 `translation_unit` 时，`vectorized_code` 字段中的函数定义必须：

1. 使用与 `target_function` 完全相同的函数名（不能添加 `_vectorized`、`_optimized` 等后缀）
2. 使用与源码完全相同的函数签名（参数类型、顺序、名称必须一致）
3. 包含完整的函数体，不仅是循环片段

错误示例：
```c
void cblas_saxpy_vectorized(int n, float alpha, const float *x, float *y) {
    // 缺少 incx/incy 参数，函数名错误
}
```

正确示例：
```c
void cblas_saxpy(int n, float alpha, const float *x, int incx, float *y, int incy) {
    // 完整签名，原始函数名
}
```

### 3. include 路径必须考虑 generate/ 子目录位置

生成的代码会写入源码目录的 `generate/` 子目录。如果原始源码使用 `#include "xxx.h"`，生成代码需要调整路径：

- 原始源码目录：`/path/to/cases/`
- 头文件位置：`/path/to/cases/blas_case_common.h`
- 生成的代码位置：`/path/to/cases/generate/cblas_saxpy_neon_generated.c`

因此生成代码中必须使用：
```c
#include "../blas_case_common.h"  // 相对路径指向上层目录
```

而不是：
```c
#include "blas_case_common.h"  // 错误：找不到头文件
```

### 4. `--case` 模式会自动识别目标循环

使用 `--case` 模式时，`generate_vectorization_request.py` 会结合：

- `target_function`
- `loop_variable`
- `iteration_count`
- `body_operations`

从对应 scalar 源码中自动定位目标循环，不要求源文件额外添加 marker。

如果是自定义源码或夹具，仍然使用显式模式传入：

- `--source-file`
- `--target-function`
- `--start-line`
- `--end-line`

### 5. 物化脚本对 response 的校验

`materialize_vectorization_result.py` 会执行以下校验：

1. 检查 `vectorization_result` 顶层包装是否存在
2. 检查 9 个必需字段是否完整
3. 检查 `success` 是否为 `true`
4. 检查 `vectorized_code` 是否非空
5. 检查 `replacement_kind` 与 `vectorized_code` 形态是否一致
6. 检查 `application_mode` 是否属于允许集合

物化有四种粒度：
- `translation_unit`：`vectorized_code` 包含完整函数定义，直接写入文件
- `full_function`：`vectorized_code` 包含完整目标函数，直接写入文件
- `wrapped_function_body`：`vectorized_code` 是片段，需要从源码提取函数签名并包裹
- `wrapped_loop_body`：`vectorized_code` 替换请求中的循环行段

模型应优先生成 `full_function` 或 `translation_unit` 粒度的代码；如果只生成片段，必须明确 `replacement_kind`，避免上层 pipeline 把完整函数误插入循环行段。

### 6. 多文件 assembly 产物

当实际选择 `codegen_style: "assembly"` 时：

1. `vectorized_code` 放 C wrapper 或主入口 C translation unit。
2. `artifacts` 至少包含一个 `language: "assembly"` 的 `.S/.s/.asm` 文件。
3. wrapper 中声明并调用 assembly artifact 暴露的 kernel 符号。
4. assembly artifact 必须遵守 AAPCS64 参数寄存器、返回值和 callee-saved 寄存器规则。
5. 物化和 benchmark 脚本会把 artifact 编译成对象并参与链接。
