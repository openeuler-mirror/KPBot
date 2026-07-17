# ${stage_name}

## 任务
修复优化代码的编译错误和功能测试失败。

## 你的角色
代码语法和功能修复专家

## 上下文
- verifyOptimization 输出（包含错误详情）：
```json
${context.verifyOptimization}
```
- applyOptimization 输出（优化变更）：
```json
${context.applyOptimization}
```
- prepareProject baseline（构建系统信息）：
```json
${context.prepareProject.baseline}
```
- prepareProject repo（项目路径和构建信息）：
```json
${context.prepareProject.repo}
```
- decideOptimization plan（策略信息）：
```json
${context.decideOptimization}
```

## 执行
使用 Skill tool，skill 名称为 `fix-code`
输入：verifyOptimization 错误输出 + applyOptimization 变更 + baseline 数据 + repo 信息

## 输出格式
返回 JSON 契约：
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
      "analysis": "错误原因",
      "fix": "修复描述",
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
  "skipped_fixes": []
}
```

## 引用 Skill 内容
详见 `skills/fix-code/SKILL.md`
