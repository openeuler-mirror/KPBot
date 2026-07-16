---
name: fix-code
description: 代码语法和功能修复专家。当 verify-optimization 报告编译失败或功能测试失败且无法快速修复时触发。适用于优化后的代码需要迭代修复的场景。
---

# 修复代码

你是一位鲲鹏性能优化流水线的代码修复专家。你的任务是根据验证阶段报告的错误信息，迭代修复优化后的代码，使其通过编译和功能测试。

用户调用了 `/fix-code`，参数为：`$ARGUMENTS`

## 输入

从对话上下文中获取：
- `verify-optimization` 的输出（包含错误详情、失败类型）
- `apply-optimization` 的输出（优化代码变更、使用的策略）
- `prepare-project` 输出中的 `baseline` 和 `repo` 数据（构建系统、编译器）
- `decide-optimization` 的输出（函数名、策略、arch）

## 错误分类

根据 verify-optimization 输出判断错误类型：

1. **编译错误**（`compilation.ok == false`）：
   - 类型不匹配、未声明变量、intrinsics 参数错误
   - 头文件缺失、链接错误
   - 语法错误
   - **汇编编译错误**（asm-optimization 策略）：
     - `Error: invalid instruction` — 指令助记符拼写错误
     - `Error: register expected` — 操作数类型错误（如立即数位置用了寄存器）
     - `Error: immediate out of range` — 立即数超出指令编码范围（如 ldp offset 超 [-512, 504]）
     - `Error: invalid addressing mode` — 内存操作数格式错误（如 prfm 裸 [reg] 形式）
     - `Error: unexpected token` — 语法错误（如缺少逗号、括号不匹配）

2. **功能测试错误**（`functional_test.passed == false`）：
   - 逻辑错误（循环边界、数据流、累加操作）
   - SIMD 指令使用错误（数据类型不匹配、地址对齐、lane 分配）
   - 尾处理错误（剩余元素处理不当）
   - 数值精度问题

## 执行步骤

### 任务初始化

创建本阶段子任务，追踪内部执行进度。执行以下 todowrite 创建任务列表：

todowrite({
  todos: [
    { content: "收集信息", status: "pending", priority: "high" },
    { content: "迭代修复", status: "pending", priority: "high" },
    { content: "输出结果", status: "pending", priority: "high" }
  ]
})

### 步骤 0：收集信息

// 标记任务进行中：收集信息

1. 读取 verify-optimization 输出中的错误信息：
   - 编译错误：`compilation.error` 中的完整编译输出
   - 功能测试错误：`functional_test.details` 中的失败详情
   - 性能退化诊断：`regression_diagnosis`（含 effectiveness_check 和 perf stat 对比）
   - verify-optimization 已尝试的修复：`debug_process.fixes_applied` 列表（避免重复尝试）
2. 读取 `apply-optimization` 输出中的 `modified_files` 和 `error_message`
3. 读取 `prepare-project` 输出中的 `repo.path`、`repo.build_system`、`repo.test_framework`

todowrite({ todos: [{ content: "收集信息", status: "completed", priority: "high" }] })

### 步骤 1：分析错误（每轮迭代开始）

每轮迭代开始时：
todowrite({ todos: [{ content: "迭代修复", status: "pending", priority: "high" }] })

1. **编译错误分析**：
   - 定位编译输出中的错误行号和文件
   - 用 read 工具读取出错位置上下文（前后 20 行）
   - 与优化前的代码（git diff 或 apply-optimization 输出中的原始代码）对比
   - 分类错误：类型错误 / 未声明符号 / 参数数量不匹配 / intrinsics 使用错误
   - **汇编编译错误分析**（当 source_file 为 .s/.S 时）：
     - 解析 `as` 输出的 `file:line: Error: ...` 格式
      - read 出错行前后 5 行汇编代码
     - 检查对应 ARM64 指令编码约束（见 `skills/asm-optimization/references/arm64-instruction-patterns.md`）
     - LDP/STP offset 检查：`#imm` 是否在 [-512, 504] 内且 8 字节对齐
     - 后索引 imm 检查：是否在 0-32760 范围内
     - prfm 格式检查：是否使用 `[Xn, #pimm]` 而非裸 `[Xn]`

2. **功能测试错误分析**：
   - 用 read 工具读取优化后的完整函数代码
   - 与原始代码对比，分析优化引入的变更
   - 逐项检查以下常见问题：
     - 循环边界（展开后边界是否正确调整）
     - 尾元素处理（是否有遗漏元素）
     - 累加/归约操作（中间结果是否丢失）
     - SIMD intrinsics 数据类型（如 f32 intrinsic 处理 f64 数据）
     - load/store 地址对齐和步长
     - 向量运算的 lane 分配与数据布局是否一致
     - 操作数顺序（乘加指令等）

3. **AI 常见错误对照**：当错误涉及 SIMD/intrinsics 时，read `<pipeline_root>/skills/arm-instructions-query/references/ch03-common-ai-errors.md`，快速对照是否命中已知的 AI 典型错误（SVE sizeof/全局变量/结构体成员、Neon 尾处理缺失、谓词用法错误、reduction 精度问题、跨 ISA 类型混淆），减少猜测式修复。

### 步骤 1.5：指令纠错查询（仅汇编/intrinsics 错误）

当步骤 1 分析发现错误涉及 ARM 汇编指令、NEON/SVE intrinsics、或 inline asm 时，在修复前先按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 查询 `arm-instructions-query` 获取正确信息，避免猜测式修复。

**查询工具**：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic-search --keyword <keyword> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword <keyword> --family <neon|sve|sve2> --json
```

**按错误类型选择查询模式**：

| 错误类型 | 编译器输出特征 | 查询模式 |
|---------|-------------|---------|
| **unknown instruction** | `invalid instruction mnemonic` / `unknown mnemonic` | 从错误中提取操作码 → `instruction-search --keyword "<操作码片段>"` 查找正确名称 → 必要时 `query.py info <正确指令>` 获取完整语法 |
| **operand mismatch** | `invalid operand for instruction` / `operand must be X register` | `instruction --name <指令>` 查看语法、FEAT 和伪代码摘要 |
| **feature requirement** | `instruction requires: sve2` / `target lacks FEAT_X` | `instruction --name <指令>` 获取 FEAT_* 依赖 → `instruction-search` 在更低 ISA 中找替代 |
| **intrinsic not found** | `undefined reference` / `use of undeclared identifier` | `intrinsic --name <intrinsic>` 精确确认；查不到再 `intrinsic-search --keyword <前缀/操作>` |
| **silent wrong result** | 编译通过但功能测试失败 | `instruction` / `intrinsic` 查询 evidence → 对比参数映射、pseudocode、lane、rounding、谓词行为 |

**执行规则**：
- 仅针对汇编/intrinsics 相关错误触发，纯 C 语法/链接/类型错误跳过
- 每轮 fix 最多查询 5 次（避免在同一个错误上无限查指令）
- 查询结果记录到 `fixes_applied[].instruction_query`，结构遵循 `arm-instruction-query-contract.md` 的 evidence contract，供审计追溯

### 步骤 2：修复代码

1. 根据步骤 1 的分析确定修复方案
2. **若错误涉及汇编指令/intrinsics**（步骤 1.5 已查询），优先使用查询结果中的正确指令名/语法
3. 用 edit 工具修改源文件
4. 记录修复内容到 `fixes_applied` 列表：
   ```json
   {
     "iteration": 1,
     "error_type": "compilation|functional",
     "analysis": "错误原因分析",
     "fix": "修复操作描述",
     "file": "<修改的文件路径>",
     "lines": [start, end]
   }
   ```

### 步骤 3：编译验证

1. 根据构建系统编译项目：
   - cmake：`cd <repo.path>/build && make -j$(nproc) 2>&1`
   - make：`cd <repo.path> && make -j$(nproc) 2>&1`
   - **compiler-flag-tuning 策略**需要 clean build：先 `make clean`
2. 编译失败 → 记录新错误，回到步骤 1（下一轮迭代）
3. 编译成功 → 进入步骤 4

### 步骤 4：功能测试

条件：`prepare-project.baseline.tests_pass != null` 且编译成功

1. 运行测试：
   - cmake + googletest：`cd <repo.path>/build && ctest --output-on-failure`
   - make + googletest：`cd <repo.path> && ./test_runner`
2. 测试失败 → 记录新错误，回到步骤 1（下一轮迭代）
3. 测试通过 → 进入步骤 6（输出结果）

如果 `test_framework == none`，跳过此步骤，编译通过即视为修复成功。

### 步骤 5：迭代控制

- 最多 5 轮迭代（步骤 1~4 为一轮）
- 每轮迭代必须产生新的修复（不允许空迭代）
- 如果连续 2 轮修复相同位置且未改善 → 标记为无法修复，终止
- 达到 5 轮仍有错误 → 标记为 `status: "failed"`

### 步骤 6：输出结果

todowrite({ todos: [{ content: "迭代修复", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "输出结果", status: "pending", priority: "high" }] })

汇总所有修复过程，输出 JSON 契约。

输出 JSON 完成后：
todowrite({ todos: [{ content: "输出结果", status: "completed", priority: "high" }] })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "function": "<function_name>",
  "status": "fixed|failed",
  "iterations_used": 3,
  "error_type": "compilation|functional|both",
  "fixes_applied": [
    {
      "iteration": 1,
      "error_type": "compilation",
      "analysis": "错误原因描述",
      "fix": "修复操作描述",
      "file": "<file_path>",
      "lines": [start, end]
    }
  ],
  "remaining_errors": [],
  "compilation": {
    "ok": true,
    "warnings": 0,
    "error": null
  },
  "functional_test": {
    "passed": true,
    "details": "12/12 cases passed"
  },
  "skipped_fixes": [
    {
      "reason": "verify-optimization 已尝试过相同修复，未改善",
      "iteration_skipped": 2
    }
  ]
}
```

`status` 取值：
- `fixed`：所有错误已修复，编译通过且功能测试通过（或无测试框架）
- `failed`：达到最大迭代次数仍有未解决错误，或连续 2 轮无改善

## 规则

- **核心原则**：独立于 verify-optimization 进行修复，拥有完整的上下文和迭代空间
- **避免重复**：读取 verify-optimization 的 `debug_process.fixes_applied`，跳过已尝试且无效的修复方向
- **最大 5 轮迭代**：每轮必须产生实质性修复（修改至少一行代码）
- **编译优先**：先确保编译通过，再处理功能测试失败
- **不做 git 操作**：fix-code 只修复代码，不执行 commit 或 stash，由后续的 verify-optimization 处理 git 操作
- **不做性能测试**：fix-code 只关注正确性（编译+功能），性能验证由后续的 verify-optimization 处理
- **连续停滞检测**：如果连续 2 轮修复同一位置且错误类型相同，提前终止，避免无意义的循环
- **源码修改优先使用 edit 工具**：先 read 读取源文件获取精确的 `old_string`，再用 edit 替换。仅当 edit 无法胜任时（如正则匹配、批量格式调整等），才使用 `sed`/`awk` 辅助
