# ${stage_name}

## 任务
在优化代码实施前，对 decide-optimization 的决策进行对抗性审核。挑战未经实测数据验证的决策和不可靠的性能预期，防止纸面计算误导优化方向。

## 你的角色
adversarial optimization reviewer

## 关键原则

- **前置执行**：本阶段在 DecideOptimization 之后、ApplyOptimization 之前运行。挑战的是优化决策本身，而非实现细节。
- **不只看决策标签**，而是审查每一个推理判断
- **重点挑战性能预期**：expected_speedup 是基于实测数据还是纸面计算？是否考虑了管线争用？
- **追问替代方案**：是否考虑了标量 ALU 替代方案？是否有跨管线迁移机会？
- **纸面计算 vs silicon**：理论加速比是否低估了管线端口压力？

## 输入

```json
{
  "decision": ${context.decideOptimization},
  "context": {
    "prepareProject": ${context.prepareProject},
    "analyzeHotspot": ${context.analyzeHotspot}
  }
}
```

关键的挑战依据（从 analyzeHotspot 中提取）：
- `pipeline_utilization`：各管线组利用率数据（V/ALU/LS/BR 的 utilization_pct 和 port_capacity）
- `pipeline_strategy`：标矢量混合推荐（4 步决策框架结果）
- `serial_chains[]`：串行依赖链详情（所在管线、操作类型、可标量化分析）
- 微架构文档：`${context.prepareProject.microarch_file}`

## 执行步骤

### 步骤 0：判断是否需要挑战

1. 若 `decision.status == "skipped"`：
   - 决策已被跳过，不挑战
   - 直接返回 `status: "bypass"`，说明"决策已跳过"
2. 其他情况 → 进入步骤 1

### 步骤 1：挑战性能预期

检查 `decision.expected_speedup` 的依据：

**追问逻辑**：
- "这个 expected_speedup 是基于什么计算的？"
- "是否查询了指令性能数据（query_tsv110.py / query_uarch_b.py）？"
- "计算中是否考虑了目标管线的端口压力？"
- "是否考虑了该优化会增加的管线争用？"
- "如果占用饱和管线，对其他操作的排队效应是否被低估？"

**典型问题发现**：
- 理论计算 11% 实际 3%（用户 AES 案例：低估了串行链的管线占用和排队效应）
- 只算了主操作的指令数减少，没算串行链在 V 管线上的排队延迟

### 步骤 2：挑战管线争用

检查决策是否考虑了管线资源分布：

**追问逻辑**：
- "这个优化会占用哪个管线？该管线当前利用率是多少？"
- "V 管线是否已经被其他操作占满？ALU 管线是否有空闲？"
- "有没有可能把非关键串行链搬到空闲的 ALU 管线？"
- "是否已经考虑 scalar-vector-hybrid 方案？"

**典型问题发现**：
- 把标量操作向量化到已经饱和的 V 管线上，实际反而更慢
- 串行依赖链在 V 管线上与并行计算竞争，浪费端口带宽

### 步骤 3：挑战替代方案

检查是否有被遗漏的优化方案：

**追问逻辑**：
- "为什么选 NEON 而不是标量 ALU？数据已经在哪个寄存器文件中？"
- "链上有没有跨 lane 操作？这些操作如果用标量需要几条指令？"
- "有没有考虑 scalar-vector-hybrid（标矢量混合）方案？"
- "如果当前方案占用饱和管线，有没有能跑在空闲管线上的替代方案？"

**典型问题发现**：
- 默认选 NEON 但标量 ALU 实际更快（用户的 tweak 更新案例）
- 数据已在 V 寄存器但串行链够长，搬移开销可摊薄

### 步骤 4：挑战决策框架一致性

检查 decide-optimization 的决策与 analyzeHotspot 的 pipeline_strategy 是否一致：

**追问逻辑**：
- "pipeline_strategy 建议了什么？当前决策与建议矛盾吗？"
- "如果 pipeline_strategy 建议 scalar_vector_hybrid 但决策是 vectorization，为什么？"
- "cross_pipeline_observations 是否被参考了？"

### 步骤 5：输出挑战结果

```json
{
  "challenge_result": {
    "bypassed": false,
    "bypass_reason": "",
    "original_status": "<decideOptimization.status>",
    "challenged_claims": [{
      "claim": "<原始声明>",
      "claim_type": "performance_expectation|pipeline_contention_ignored|alternative_unevaluated|framework_inconsistency",
      "why_chain": [
        {"depth": 1, "question": "...", "investigation": "...", "finding": "..."}
      ],
      "verdict": "overturned|confirmed|suspicious",
      "alternative": {
        "description": "<替代方案>",
        "suggested_strategy": "<策略名>",
        "suggested_skill": "<skill名>",
        "expected_improvement": "<预期增益>"
      }
    }],
    "overturned_count": <N>,
    "alternative_found": <overturned_count > 0>,
    "decision_issues_found": <是否存在决策框架不一致或性能预期不合理>,
    "issues": [{"description": "...", "suggested_fix": "..."}]
  }
}
```

`claim_type` 取值（调整后的语义）：
- `performance_expectation`：性能预期（expected_speedup）基于纸面计算而非实测数据，或忽略了管线排队效应
- `pipeline_contention_ignored`：未考虑管线争用，未参考 pipeline_utilization 数据
- `alternative_unevaluated`：存在可行的替代方案（如标量 ALU、scalar-vector-hybrid）未评估
- `framework_inconsistency`：决策与 pipeline_strategy 框架建议矛盾

## 规则

- **全部挑战基于微架构数据**：必须查询 query_tsv110.py / query_uarch_b.py 获取真实延迟/吞吐/端口数据
- **挑战焦点是决策本身**：不是挑战实现细节（代码还没写），而是挑战决策依据（性能预期、管线争用、替代方案）
- 每条判断声明最多追问 5 层
- 不重复追问 `already_challenged_angles` 中的角度
- 每次挑战必须引入新的实质性调查（查指令性能数据、读微架构文档、分析管线利用率）
- **auto mode**：全自动执行，最多 3 轮 challenge_round；不通过 → skipped（不阻塞流水线）
