# ${stage_name}

## 任务
逐用例动态 profiling，跨用例综合排序热点函数，分解为函数级子任务。

## 你的角色
task decomposition expert

## 上下文
- prepareProject 输出：
```json
${context.prepareProject}
```
- test_cases：${context.test_cases}
- test_method：${context.test_method}
- detected_cases：${context.detected_cases}

## 可用资源
- ARCHITECTURE.md：${context.prepareProject.architecture_file}（如需了解项目结构/数据结构/头文件依赖，Read 此文件）

## 执行
使用 Skill tool，skill 名称为 `decompose-tasks`
参数：prepareProject 完整输出 + test_cases + test_method + detected_cases

**注意**：本阶段不读源码做静态分析。静态分析（SIMD 检测、数据依赖、可优化性判断）统一由下游 `analyze-hotspot` 负责。

## 输出格式
返回 JSON 契约：
```json
{
  "sub_tasks": [
    {
      "id": 1,
      "function": "<function_name>",
      "source_file": "<file_path>",
      "lines": [start_line, end_line],
      "priority": "high|medium|low",
      "cross_case_weight": 75.96,
      "cpu_percent": 35.2,
      "case_distribution": {
        "<case_name_1>": { "cpu_percent": 35.2 },
        "<case_name_2>": { "cpu_percent": 28.1 }
      },
      "coverage": 0.4,
      "reason": "跨 2/5 用例的热点函数，综合权重 75.96"
    }
  ],
  "profiling": {
    "used": true,
    "tool": "perf|flamegraph|none",
    "per_case": [
      {
        "case": "<case_name>",
        "tool": "perf",
        "duration_sec": 30,
        "hotspots_found": 3,
        "status": "ok|failed"
      }
    ],
    "cross_case_ranking_method": "weighted_cpu_percent_with_coverage",
    "fallback_reason": null
  },
  "skipped": [
    { "function": "<name>", "cpu_percent": 3.2, "reason": "<跳过原因>" }
  ],
  "status": "decomposed" | "empty"
}
```

## 引用 Skill 内容
详见 `skills/decompose-tasks/SKILL.md`
