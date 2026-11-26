# ${stage_name}

## 任务
对单个优化点进行确认/跳过门控，设定架构和风险参数。

## 你的角色
optimization strategy expert

## 上下文
当前优化点（来自 analyzeHotspot.optimization_points[]，协调者逐个传入）：
```json
${context.current_optimization_point}
```

analyzeHotspot 完整输出（用于交叉验证）：
```json
${context.analyzeHotspot}
```

prepareProject 输出（用于获取编译器和硬件信息）：
```json
${context.prepareProject}
```

函数信息：
- function: ${context.current_sub_task.function}
- source_file: ${context.current_sub_task.source_file}
- lines: ${context.current_sub_task.lines}

parseIntent（用户优化意图，用于确认门控条件判断）：
```json
${context.parseIntent}
```

## 实验证据上下文
- 当前设计/假设：${context.designExperiment}
- 经 CLI 校验的完整反馈：${context.validateExperiment}
- 持久实验预算：${context.diagnosticAttempts}

只有当前 supported + evidence_checked + target_checked 才可 confirmed。证据变化时输出 status=diagnose、next_stage=AnalyzeHotspot 或 DesignExperiment，并给 reason。保留假设/实验/record_path 引用；未验证预测只标 estimate，不能保证 expected_speedup。

## 可用资源
- ARCHITECTURE.md：Read `${context.prepareProject.architecture_file}`（项目结构/数据结构/现有优化位置，辅助策略判断）
- 微架构文档：Read `${context.prepareProject.microarch_file}`（鲲鹏型号对应的指令延迟/端口分配/cache 层次/SVE 支持情况，辅助门控判断）

## 执行
使用 Skill tool，skill 名称为 `decide-optimization`
参数：单个 optimization_point + analyzeHotspot + prepareProject + designExperiment + validateExperiment（均完整输出）

**注意**：策略发现和优先级排序已由 `analyze-hotspot` 完成，本阶段不重复做策略选择。仅做确认门控（confidence 检查、风险预判、架构确认）。

## 输出格式
返回 JSON 契约：
```json
{
  "function": "<function_name>",
  "optimization_point_id": "func_opt1",
  "strategy": "vectorization|vectorization_deepen|autovec-source-transform|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|throughput-enhancement|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|math-rewrite|algorithm-substitution|variant-selection|code_hoisting|special-case-optimization|operation-fusion|precision-transform",
  "skill": "apply-vectorization",
  "arch": "neon",
  "confidence": 0.9,
  "expected_speedup": null,
  "prediction_basis": "estimate: 未完成候选系统验收",
  "risk": "low",
  "input": {
    "source_file": "<file_path>",
    "function": "<function_name>",
    "lines": [start_line, end_line],
    "target_arch": "neon"
  },
  "throughput_enhancement": null,
  "hypothesis_id": "<支持的假设 ID>",
  "experiment_id": "<支持的实验 ID>",
  "record_path": "<记录路径>",
  "reason": "<依据或转向原因>",
  "next_stage": "AdversarialReview|AnalyzeHotspot|DesignExperiment",
  "status": "confirmed" | "skipped" | "diagnose"
}
```

## 引用 Skill 内容
详见 `skills/decide-optimization/SKILL.md`
