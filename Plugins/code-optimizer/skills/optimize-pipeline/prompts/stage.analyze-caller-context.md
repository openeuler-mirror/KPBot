# ${stage_name}

## 任务
从调用者视角分析热点函数的外部关系，发现函数体内部不可见的优化机会。

## 你的角色
caller context analysis expert

## 上下文
聚焦单函数的调用上下文本分析：
```json
{
  "function": "${context.current_sub_task.function}",
  "source_file": "${context.current_sub_task.source_file}",
  "lines": "${context.current_sub_task.lines}",
  "decompose_tasks": ${context.decomposeTasks},
  "project_path": "${context.project_path}",
  "analyze_hotspot": ${context.analyzeHotspot},
  "intent": ${context.parseIntent},
  "architecture_file": "${context.prepareProject.architecture_file}",
  "performance_profile": ${context.testcaseAnalysis.performance_profile}
}
```

## 执行
使用 Skill tool，skill 名称为 `analyze-caller-context`
参数：上述聚焦单函数的调用者上下文

调用上下文分析三阶段：
1. 收集调用者信息（profiling 调用栈 + grep 搜索调用点 + 读取函数签名）
2. 14 维度调用点分析（预检 → 评分 → 过滤排序）
3. 生成 caller_optimization_points（Top 5，按 dimension_score 降序）

## 输出格式
返回 JSON 契约：
```json
{
  "caller_analysis_result": {
    "success": true,
    "function": "<function_name>",
    "callers": [{ "name": "<caller>", "source_file": "<file>", "line": 42 }],
    "optimization_points": [{
      "id": "func_caller_opt1",
      "type": "special-case-optimization|operation-fusion|vectorization",
      "sub_type": "<映射后的调用点优化类型>",
      "dimension_score": 0.82,
      "confidence": 0.82,
      "priority": 1,
      "caller": "<caller_name>",
      "intermediate_lifetime": "local_only|escaped|unknown",
      "strategy_payload": { "source": "caller-context", "dimension": "<分析维度>" },
      "evidence": { "dimension": "<分析维度>", "caller_site": "<file:line>", "pattern": "<模式>", "static": "<静态证据>", "dynamic": "<动态数据>" },
      "suggestion": "<优化建议>",
      "risk_level": "low|medium|high",
      "expected_speedup": "<预估提升>"
    }],
    "skipped_dimensions": [{ "dimension": "...", "reason": "..." }],
    "error_message": ""
  }
}
```

## 引用 Skill 内容
详见 `skills/analyze-caller-context/SKILL.md`
