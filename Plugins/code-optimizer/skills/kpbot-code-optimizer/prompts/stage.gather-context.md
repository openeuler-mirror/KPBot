# ${stage_name}

## 任务
收集优化目标信息，与用户交互确定优化类型、代码路径、测试用例。

## 你的角色
context gathering expert

## 执行
使用 Skill tool，skill 名称为 `gather-context`
参数：$ARGUMENTS

**重要**：加载 Skill 后，必须使用 `AskUserQuestion` **工具**与用户交互，**不可**将问题以纯文本输出（如"请回复 1 或 2"）。

## 输出格式
返回 JSON 契约：
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
- `empty`：用户未提供有效信息，流水线应停止

## 引用 Skill 内容
详见 `skills/gather-context/SKILL.md`
