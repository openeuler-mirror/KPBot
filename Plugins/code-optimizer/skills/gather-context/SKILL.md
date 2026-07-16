---
name: gather-context
description: 收集优化目标信息，与用户交互确定优化类型、代码路径、测试用例，自动探测项目用例并支持复选框选择。适用于 optimize-pipeline 的首个阶段。
---

# 收集优化上下文

你是一位鲲鹏性能优化流水线的上下文收集专家。你的任务是通过交互式问答收集用户的优化目标信息，自动探测项目测试用例，组装完整的优化上下文。

用户调用了 `/gather-context`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取（通常为空，本阶段从零开始与用户交互）。

## 执行步骤

### 任务初始化

- interact_task_id = TaskCreate({ subject: "    └ 交互收集", description: "通过 AskUserQuestion 收集优化目标、代码路径、测试用例" })
- assemble_task_id = TaskCreate({ subject: "    └ 组装上下文", description: "汇总交互结果，组装标准输出 JSON" })

**重要**：本 Skill 所有用户交互必须通过 `AskUserQuestion` **工具**完成。**禁止**将问题以纯文本输出（如"请回复 1 或 2"），必须调用工具弹出交互式选项界面。

### 步骤 1：优化目标类型

TaskUpdate({ taskId: interact_task_id, status: "in_progress" })

使用 AskUserQuestion 工具，参数如下：

```json
{
  "questions": [{
    "question": "请选择优化目标类型？",
    "header": "目标类型",
    "multiSelect": false,
    "options": [
      {"label": "函数优化", "description": "优化指定 C/C++ 函数的性能"},
      {"label": "用例优化", "description": "以测试用例为驱动进行性能优化"}
    ]
  }]
}
```

将用户选择记为 `user_choice`：
- 用户选择"函数优化" → `"function"`
- 用户选择"用例优化" → `"testcase"`

### 步骤 2a：函数优化分支 — 代码路径与函数名

当 `user_choice == "function"` 时执行。

调用 `AskUserQuestion`，参数如下：

```json
{
  "questions": [{
    "question": "请提供要优化的源代码文件绝对路径和函数名？",
    "header": "代码信息",
    "multiSelect": false,
    "options": [
      {"label": "我来提供路径和函数名", "description": "请在备注中输入源代码文件的绝对路径和函数名，格式：路径 函数名"},
      {"label": "帮我分析整个模块", "description": "不指定具体函数，由流水线自动识别优化目标"}
    ]
  }]
}
```

从用户回答中提取 `code_path` 和 `function_name`。从 `code_path` 推断 `project_path`（父目录或向上查找 `.git`）。

### 步骤 2b：用例优化分支 — 项目路径

当 `user_choice == "testcase"` 时执行。

调用 `AskUserQuestion`，参数如下：

```json
{
  "questions": [{
    "question": "请提供要优化的项目根目录绝对路径？",
    "header": "项目路径",
    "multiSelect": false,
    "options": [
      {"label": "我来提供路径", "description": "请在备注中输入项目根目录的绝对路径"},
      {"label": "使用当前目录", "description": "使用当前工作目录作为项目路径"}
    ]
  }]
}
```

将用户提供的路径记为 `project_path`。

### 步骤 3：函数优化分支 — 测试用例与执行方法

当 `user_choice == "function"` 时执行。

调用 `AskUserQuestion`，参数如下：

```json
{
  "questions": [{
    "question": "请提供该函数的测试用例和执行方法？",
    "header": "测试方法",
    "multiSelect": false,
    "options": [
      {"label": "有测试用例", "description": "请在备注中输入测试用例名称和执行命令，格式：用例名 命令"},
      {"label": "没有测试用例", "description": "跳过测试，仅做编译验证和性能对比"}
    ]
  }]
}
```

### 步骤 4：用例探测

两个分支均可能执行用例探测：

- **函数优化分支**：当步骤 3 用户选择"有测试用例"后执行
- **用例优化分支**：步骤 2b 完成后执行

用 Bash 工具对 `project_path` 执行用例探测：

1. **检测测试框架**：
   - `grep -rl '#include <gtest/gtest.h>' <project_path>/` → googletest
   - `grep -rl '#include <catch2/catch_test_macros.hpp>' <project_path>/` → catch2
   - 都未找到 → framework = "none"

2. **提取用例列表**（按框架类型探测）：
   - **Googletest**：`grep -rn 'TEST(_F\|(.*,' <project_path>/ --include='*.cpp' --include='*.cc' | sed 's/.*TEST(_F\|(.*,\s*\([A-Za-z_0-9]*\)).*/\1/' | sort -u`
   - **CMake ctest**：`cd <project_path>/build && ctest -N 2>/dev/null | grep 'Test #'`
   - **自定义可执行文件**：`find <project_path> -type f \( -name 'test_*' -o -name '*_test' -o -name 'benchmark_*' \) ! -name '*.o' ! -name '*.cpp' ! -name '*.c' 2>/dev/null`
   - 合并去重，得到 `detected_cases` 列表

3. **分支处理**：
   - 探测到 **0 个** 用例 → 沿用用户手动提供的信息，跳过步骤 5
   - 探测到 **1 个** 用例 → 直接使用该用例，跳过步骤 5
   - 探测到 **2 个及以上** 用例 → 进入步骤 5

### 步骤 5：用例选择（复选框）

当探测到 2 个及以上用例时执行。

调用 `AskUserQuestion`，参数如下：

```json
{
  "questions": [{
    "question": "探测到以下测试用例，请选择要用于优化的用例（可多选）：",
    "header": "用例选择",
    "multiSelect": true,
    "options": [
      {"label": "<用例1名>", "description": "<用例1描述或来源>"},
      {"label": "<用例2名>", "description": "<用例2描述或来源>"},
      {"label": "<用例3名>", "description": "<用例3描述或来源>"}
    ]
  }]
}
```

`options` 构造规则：
- 2-3 个用例：全部展示，每个 option 的 `label` 为用例名，`description` 为用例类型（googletest/ctest/executable）
- 4 个用例：全部展示
- 超过 4 个用例：展示前 3 个，第 4 个 option 固定为 `{"label": "其他（备注指定）", "description": "请在备注中输入其他用例名"}`，剩余用例需用户在备注中补充

### 步骤 6：执行命令确认（可选）

仅当无法从探测结果自动推导执行命令时执行。

调用 `AskUserQuestion`，参数如下：

```json
{
  "questions": [{
    "question": "请提供选中用例的执行命令？",
    "header": "执行方法",
    "multiSelect": false,
    "options": [
      {"label": "我来提供执行命令", "description": "请在备注中输入执行命令，如：cd build && ./test_runner --gtest_filter=TestName"},
      {"label": "使用 ctest 运行", "description": "自动使用 ctest -R 匹配选中的用例名"}
    ]
  }]
}
```

### 步骤 7：组装上下文

TaskUpdate({ taskId: interact_task_id, status: "completed" })
TaskUpdate({ taskId: assemble_task_id, status: "in_progress" })

1. 将选中用例名列表存入 `test_cases`（逗号分隔）
2. 推导 `test_method`：
   - Googletest + ctest：`cd <project_path>/build && ctest -R "<用例1>|<用例2>" --output-on-failure`
   - 单个可执行文件：`cd <project_path>/build && ./<executable>`
   - 无法自动推导 → 使用步骤 6 用户提供的命令
3. 将探测到的全部用例存入 `detected_cases`

TaskUpdate({ taskId: assemble_task_id, status: "completed" })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "user_choice": "function|testcase",
  "project_path": "/absolute/path/to/project",
  "code_path": "/absolute/path/to/source.c",
  "function_name": "matmul",
  "test_cases": "benchmark_matmul, test_matmul",
  "test_method": "cd build && ctest -R 'benchmark_matmul|test_matmul' --output-on-failure",
  "detected_cases": [
    {
      "name": "benchmark_matmul",
      "description": "GEMM 性能基准测试",
      "type": "googletest|ctest|executable"
    }
  ],
  "status": "gathered"
}
```

`status` 取值：
- `gathered`：信息收集完成
- `empty`：用户未提供有效信息（如路径为空），流水线应停止

字段说明：
- `code_path` 和 `function_name`：仅函数优化分支有值，用例优化分支为 `null`
- `test_cases`：用户选中的用例名列表，逗号分隔
- `test_method`：推导或用户提供的执行命令
- `detected_cases`：自动探测到的全部用例列表（供下游参考），无探测结果时为空数组 `[]`

## 规则

- **必须使用 AskUserQuestion 工具**：所有用户交互通过 `AskUserQuestion` 工具完成，不可用纯文本替代。工具会自动渲染为复选框/选项 UI
- 本阶段从零开始与用户交互，不依赖上游 stage 输出
- 用户选择"没有测试用例"时，`test_cases` 和 `test_method` 设为 `null`
- 用例探测是可选增强，探测失败不阻塞，沿用用户手动提供的信息
- `AskUserQuestion` 的 options 不超过 4 个，超出时第 4 个固定为"其他（备注指定）"
