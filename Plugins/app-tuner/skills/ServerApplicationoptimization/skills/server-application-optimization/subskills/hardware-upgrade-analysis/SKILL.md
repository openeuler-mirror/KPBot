---
name: hardware-upgrade-analysis
description: 当瓶颈证据显示当前 CPU 核数、内存容量、内存带宽、磁盘 IOPS、网卡带宽、GPU/NPU 规格或其他硬件能力不足时，输出更换高规格硬件或资源规格调整建议，作为服务器应用优化 Agent 候选优化 skill 列表中的硬件规格分析 skill 使用。
---

# Hardware Upgrade Analysis

使用本 skill 处理架构图中的“更换高规格硬件”路径。

## Inputs

- `bottleneck_classification`
- 当前硬件规格和目标规格
- 基线指标、目标指标、资源利用率
- 软件优化候选收益和停止原因
- `hardware_change_allowed`

## Workflow

1. 确认当前瓶颈是否主要由硬件规格解释。
2. 区分容量不足、带宽不足、核数不足、设备 IOPS 不足、网卡带宽不足、GPU/NPU 规格不足。
3. 若软件侧仍有高置信低风险动作，先建议继续软件优化。
4. 若硬件不足证据充分，输出规格调整建议和预期改善方向。
5. 只输出建议，不直接执行硬件变更。

## Outputs

- `hardware_capacity_findings`
- `recommended_hardware_profile`
- `software_mitigation_actions`
- `candidate_actions`
- `validation_plan`
- `risk_notes`

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`expected_gain`、`risk`、`validation`、`rollback_or_reversal`、`stop_or_reject_condition` 和 `evidence_sources`。硬件升级建议必须明确哪些软件优化已完成、停止或被证据否决，不能把未验证的软件空间直接包装成硬件不足。

## Knowledge Mapping

参考 `references/knowledge-technique-routing.md` 判断硬件不足是否源自 L6 微架构、内存带宽、网卡带宽、磁盘 IOPS 或 GPU/NPU 规格。知识库案例只能用于规格候选和验证指标，当前收益必须来自用户确认的 A/B 或容量模型。
