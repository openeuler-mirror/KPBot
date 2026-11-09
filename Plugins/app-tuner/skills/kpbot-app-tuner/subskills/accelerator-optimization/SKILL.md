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

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`。驱动安装、固件升级、设备重置、MIG/分区调整等动作只允许在主流程 `approved_execute` 后执行。

## Knowledge Mapping

若证据指向 CPU feed、数据加载、batch、算子 fallback 或 host-device 拷贝，应先在 `../../references/knowledge-technique-routing.md` 中确认是否更适合路由到应用配置、源码/Other、硬件升级或编译优化；本 skill 只负责加速卡路径的专项判断。

## NPU+vLLM 专项引用（vllm-ascend-profiling）

当现场同时满足 NPU 与 vLLM 推理服务时，本 skill 必须引用 `subskills/vllm-ascend-profiling/SKILL.md`（相对本目录即 `../vllm-ascend-profiling/SKILL.md`）执行其完整工作流（Stage-1 环境诊断与 TTFT 分解 profiling），诊断与采集结论回流到本 skill 的 `accelerator_findings`，不得由主 agent 手写替代。

触发条件（任一命中即引用）：

1. 进程/服务证据表明 vLLM（`import vllm`、vLLM server 日志、`vllm_ascend` pip 包或 NPU 上以 python 运行的 vLLM worker）。
2. TTFT 证据未达标：`TTFT > 目标值`、首 token 延迟劣化、或对比基线存在显著首包时延回退。
3. TTFT 分解 profiling 显式需求：任务要求 kernel/host/device 各段耗时分解，或现场出现 `trace_view.json` / vLLM profiler 采集需求。

缺席条件：无 vLLM 证据且 TTFT 无信息时，不引用；仅 GPU 场景不引用（本映射针对 Ascend NPU）。

执行顺序：先按本 skill 主 workflow 完成 GPU/NPU 通用瓶颈判断，再对命中触发条件的 vLLM 场景追加引用执行；引用产物（Stage-1 环境诊断结论、TTFT 计时/trace 证据）必须带证据路径回填 `accelerator_findings`。
