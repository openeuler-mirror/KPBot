---
name: accelerator-optimization
description: 分析 GPU、NPU 或其他计算卡相关瓶颈，检查设备利用率、显存/内存、host-device 拷贝、batch size、并发流、算子 fallback 和 CPU feed 能力，作为服务器应用优化 Agent 候选优化 skill 列表中的 GPU/NPU 等计算卡优化 skill 使用。
---

# Accelerator Optimization

使用本 skill 处理架构图中的“GPU/NPU 等计算卡”路径。

## Inputs

- `bottleneck_classification=gpu_npu_bottleneck`
- GPU/NPU 设备清单和驱动信息
- 设备利用率、显存、带宽、错误计数
- host-device 拷贝和数据加载证据
- 模型或计算任务的 batch、stream、算子信息
- `agent_action_mode`

## Workflow

1. 确认是否存在 GPU/NPU；不存在时输出 `status=degraded`、`accelerator_status=not_present`。
2. 判断瓶颈位于设备计算、显存容量、显存带宽、host-device 拷贝、CPU feed 还是算子 fallback。
3. 输出 batch、并发流、数据加载、算子替换、CPU feed 优化或硬件升级候选。
4. 当设备规格不足时，将后续路由交给 `hardware-upgrade-analysis`。
5. 不直接安装驱动、不修改生产设备配置。

## Outputs

- `accelerator_status`
- `accelerator_findings`
- `candidate_actions`
- `hardware_capacity_recommendation`
- `validation_plan`
- `rollback`
- `fallback_notes`

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。驱动安装、固件升级、设备重置、MIG/分区调整等动作只允许在主流程 `approved_execute` 后执行。

## Knowledge Mapping

若证据指向 CPU feed、数据加载、batch、算子 fallback 或 host-device 拷贝，应先在 `references/knowledge-technique-routing.md` 中确认是否更适合路由到应用配置、源码/Other、硬件升级或编译优化；本 skill 只负责加速卡路径的专项判断。
