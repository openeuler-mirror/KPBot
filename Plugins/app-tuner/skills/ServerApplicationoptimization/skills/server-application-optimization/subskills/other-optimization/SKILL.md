---
name: other-optimization
description: 承接无法归类到 CPU 亲和性、网络、性能库、应用配置、BIOS、OS、编译、GPU/NPU 或硬件升级的服务器应用优化问题，输出需要人工专项分析或新增 skill 的候选方向，作为服务器应用优化 Agent 候选优化 skill 列表中的 Other 优化 skill 使用。
---

# Other Optimization

使用本 skill 处理架构图中的“Other 优化 skill”。

## Inputs

- `bottleneck_classification`
- `hot_functions`
- `topdown_summary`
- `process_thread_summary`
- 已尝试或已停止的 skill 列表
- `unknown_bottleneck` 或未归类证据

## Workflow

1. 汇总无法归类的证据。
2. 判断是否需要补充采集、人工专项分析、创建新 skill 或转交业务代码分析。
3. 输出候选方向，但不得伪装成高置信可执行优化。
4. 若发现新稳定模式，建议沉淀为新的子 skill。

## Outputs

- `other_findings`
- `new_skill_candidate`
- `manual_analysis_needed`
- `candidate_actions`
- `required_evidence`
- `next_steps`

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。若证据不足以形成可执行动作，输出 `manual_analysis_needed=true` 或 `new_skill_candidate`，不要生成高置信动作。

## Knowledge Mapping

优先读取 `references/knowledge-technique-routing.md`。L5 源码优化类技术，如 zero copy、batching、data structure alignment/slimming、operator fusion、scheduling strategy、deep copy to shallow copy，默认先进入本 skill 做证据归类，再决定是否沉淀为新的专项子 skill。
