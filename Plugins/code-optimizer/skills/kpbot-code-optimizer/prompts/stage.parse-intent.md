# ${stage_name}

## 任务
收集用户优化偏好（优化目标、风险容忍度），结合函数名/用例名语义推断，输出结构化优化意图。

## 你的角色
intent parsing expert

## 上下文
gatherContext 完整输出（用于提取函数名、用例名进行语义推断）：
```json
${context.gatherContext}
```

## 执行
使用 Skill tool，skill 名称为 `parse-intent`
参数：上述 gatherContext JSON

**重要**：加载 Skill 后，必须使用 `AskUserQuestion` **工具**与用户交互，**不可**将问题以纯文本输出。

意图收集完成后，执行语义推断（函数名/用例名关键词匹配），合成最终意图并输出。

## 输出格式
返回 JSON 契约：
```json
{
  "optimization_goal": "throughput",
  "risk_tolerance": "moderate",
  "platform_constraint": "kunpeng-only",
  "performance_target": "significant",
  "inferred_goal": "throughput",
  "inference_confidence": 0.8,
  "evidence": [
    "用户选择：吞吐量优先",
    "用户选择：适中风险",
    "函数名 'gemm_neon_kernel' 匹配关键词 'gemm'+'neon'，推断为计算密集型"
  ],
  "status": "analyzed"
}
```

## 引用 Skill 内容
详见 `skills/parse-intent/SKILL.md`
