# ${stage_name}

## 任务
分析测试用例源码，提取性能画像（规模/并发/访存模式/缓存场景），检测预热偏差、用例冲突和断言充分性，指导后续优化策略选择。

## 你的角色
test case performance analysis expert

## 上下文
从 DecomposeTasks 的 per-case profiling 数据出发，结合测试源码分析：
```json
{
  "decompose_tasks": ${context.decomposeTasks},
  "prepare_project": ${context.prepareProject},
  "parse_intent": ${context.parseIntent},
  "project_path": "${context.project_path}",
  "test_cases": "${context.test_cases}",
  "test_method": "${context.test_method}",
  "detected_cases": ${context.detected_cases},
  "architecture_file": "${context.prepareProject.architecture_file}"
}
```

## 执行
使用 Skill tool，skill 名称为 `analyze-testcase`
参数：上述上下文 JSON

分析流程：
1. 收集测试用例源码（ctest/googletest/executable 三种类型分别定位）
2. 六维度分析（预热干扰 → 规模敏感性 → 共演检查 → 多 case 冲突 → 断言充分性 → 冷热缓存）
3. 生成 performance_profile（下游优化层可直接消费的性能画像）
4. 置信度评分与过滤（confidence < 0.4 降级为 advisory）
5. 生成 optimization_guidance（为下游阶段提供策略建议）

## 输出格式
返回 JSON 契约：
```json
{
  "testcase_analysis": {
    "success": true,
    "cases_analyzed": ["<case_name>"],
    "cases_skipped": [],
    "performance_profile": {
      "scale": { "level": "small|medium|large|xlarge", "data_size_bytes": 0, "dimensions": {}, "element_type": "", "fits_in": "L1|L2|L3|RAM" },
      "concurrency": { "threads": 1, "model": "single_thread|data_parallel|task_parallel", "sync_overhead_risk": "none|low|medium|high" },
      "bottleneck_type": "compute_bound|memory_bound|latency_bound|mixed",
      "cache_scenario": "cold|warm|mixed",
      "access_pattern": "streaming|strided|random|indirect",
      "precision": "fp32|fp64|int32|mixed",
      "test_intent": "max_throughput|min_latency|correctness|stress",
      "warmup_bias": { "risk": "low|medium|high", "affected_functions": [] },
      "co_evolution_alerts": [],
      "conflict_warnings": [],
      "assertion_adequacy": "adequate|borderline|insufficient"
    },
    "case_analyses": [{
      "case_name": "...",
      "source_file": "...",
      "source_available": true,
      "warmup_bias": {},
      "scale_profile": {},
      "co_evolution": {},
      "cache_scenario": {},
      "assertion_adequacy": {}
    }],
    "conflict_warnings": [],
    "optimization_guidance": [{
      "target": "all|<strategy_name>",
      "guidance": "<具体指导>",
      "reason": "<原因>",
      "confidence": 0.85
    }],
    "findings": [{
      "id": "tc_finding_1",
      "dimension": "<维度名>",
      "confidence": 0.85,
      "severity": "high|medium|low",
      "description": "<描述>",
      "implication": "<对优化方向的影响>",
      "suggestion": "<建议>"
    }],
    "skipped_dimensions": [],
    "error_message": ""
  }
}
```

## 透传路径
- `performance_profile` → AnalyzeHotspot（修正热点判断 + 策略推荐）
- `performance_profile` → DecideOptimization（参考 profile 做策略取舍）
- `performance_profile` → ApplyOptimization → 各优化 skill（附带完整性能画像，无需自行推断运行场景）
- `optimization_guidance` → DecideOptimization（策略优先级偏置）

## 引用 Skill 内容
详见 `skills/analyze-testcase/SKILL.md`
