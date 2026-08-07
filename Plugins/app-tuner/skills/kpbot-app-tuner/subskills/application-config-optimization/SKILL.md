---
name: application-config-optimization
description: 综合瓶颈分析结果输出线程数、队列、批量、缓存、连接等应用层最佳性能配置建议，作为 kpbot-app-tuner 的子 skill 使用。
---

# Application Config Optimization

当需要将平台、CPU 和热点分析结论沉淀为应用层最佳配置时，使用本子 skill。

## 数据库型工作负载专项承接

本子 skill 统一承接数据库型工作负载的专项分析。当检测到 `workload_type == database` 时，本子 skill 负责决定是否需要深入数据库内部状态分析，并按需引用：

- `subskills/database-workload-analysis/SKILL.md`

`database-workload-analysis` 提供数据库通用分析框架、MySQL/InnoDB 示例和 AHI 判断逻辑，但不作为主流程的独立阶段存在。其分析结果由本子 skill 汇总后统一输出给主 skill。

## 大数据框架专项承接

本子 skill 统一承接大数据框架工作负载的专项分析。当检测到 `workload_type` 匹配 spark/flink 或检测到相关配置文件时，按需引用：

- `ref-skills/bigdata-framework-optimization/SKILL.md`

`bigdata-framework-optimization` 提供 Spark、Flink 等大数据框架的参数推荐表格和适用条件。其分析结果由本子 skill 汇总后统一输出给主 skill。

重点关注：

- 线程数
- 队列深度
- 批量大小
- 缓存大小
- 连接池参数
- 并发模型
- 测试规范与结果可比性
- 配置项之间的协同关系

## 昇腾 NPU LLM 推理专项承接

本子 skill 统一承接昇腾 NPU 上 LLM 推理型工作负载的应用层参数推荐。当检测到 `workload_type == npu_llm_infer`，或检测到 CANN/torch_npu、`infer.sh`、Qwen/DeepSeek/GPT-OSS 等 LLM 推理入口时，按下表输出候选动作。

### 推荐参数

| 参数 | 推荐值 | 配置位置 | 作用 | 适用场景 | 风险 | 验证 | 回退 |
|---|---|---|---|---|---|---|---|
| `TASK_QUEUE_ENABLE` | `export TASK_QUEUE_ENABLE=2` | 环境变量 | 开启 task queue 流水调度，重叠 AI Core 计算与 Host→Device 算子下发，减少流水气泡 | NPU 推理（ge_graph / npugraph_ex），prefill/decode 延迟型 | 低：环境变量级，不改图结构与权重 | 重启服务后对比 prefill/decode ms（越低越好） | `unset TASK_QUEUE_ENABLE` 或设为 `0` 重启 |
| `--async-scheduling` | server 启动追加 `--async-scheduling` | vllm/server 启动命令 | 异步调度，Host 端异步下发算子，降低 decode 串行等待 | online / server 启动模式；offline 单次推理收益有限 | 中：仅 server 模式生效；须确认入口支持该 flag | 启动日志确认 flag 生效 + 复测 decode 延迟 | 移除 flag 重启 |
| `--enable-chunked-prefill` | server 启动追加 `--enable-chunked-prefill` | vllm/server 启动命令 | 分块 prefill，将长 prefill 拆为 chunk 与 decode 交替执行，降低长序列首 token 延迟、改善 prefill/decode 混排调度 | 长 input / 在线服务多请求场景；短序列离线单请求收益有限 | 中：改变 prefill 调度路径，可能影响吞吐-延迟权衡；须确认入口支持 | 复测 prefill 延迟 + 监控吞吐是否回退 | 移除 flag 重启 |
| `--block-size` | `--block-size=16` | vllm 启动命令 | KV cache block 大小，影响显存碎片与 decode 访存效率 | paged/block KV 实现的解码路径 | 中：过大浪费显存或与 max_batch 不匹配；过小访存放大 | 复测 decode 延迟 + 监控 OOM/显存占用 | 还原默认 block-size 重启 |

### 协同与冲突

- `TASK_QUEUE_ENABLE=2` 与 `--async-scheduling` 均作用于 Host-Device 调度流水，组合后可能叠加或相互替代，须按 Synergy Detection 规则先单独测再组合测。
- `--enable-chunked-prefill` 与 `--async-scheduling` 均为 server 启动 flag 且都作用于调度路径：chunked-prefill 改 prefill 拆分调度，async-scheduling 改算子下发时序，组合须实测是否冲突或抵消。
- `--block-size=16` 与 `batch_size`、`max_new_tokens`、可用 HBM 耦合，调整时须保证 `block_size * num_blocks * kv_dim` 不超显存上限。
- 与 YAML 内 `exe_mode` 协同：`npugraph_ex` 已固化部分调度，`TASK_QUEUE_ENABLE=2` 收益可能减弱，须实测确认。
- 与 `accelerator-optimization` 边界：本节只输出应用层启动参数（环境变量 + vllm/server flag）；HBM 带宽、算子 fallback、AI Core 利用率等硬件侧证据由 `accelerator-optimization` 提供。

### 内存参数优化

来源：[昇腾 PyTorch 内存优化文档](https://www.hiascend.com/document/detail/zh/Pytorch/600/ptmoddevg/trainingmigrguide/performance_tuning_0040.html)。以下参数为 torch_npu 通用内存优化手段，适用于 NPU LLM 推理场景中显存碎片、内存复用不足、多流内存隔离或 GC 抖动导致的性能问题。

| 参数 | 推荐值 | 配置位置 | 作用 | 适用场景 | 风险 | 验证 | 回退 |
|---|---|---|---|---|---|---|---|
| `PYTORCH_NPU_ALLOC_CONF` (expandable_segments) | `export PYTORCH_NPU_ALLOC_CONF="expandable_segments:True"` | 环境变量 | 使能内存池扩展段功能，由 PyTorch 自管理虚拟地址与物理地址映射，降低内存碎片 | 动态 shape 场景（LLM 推理 prefill/decode shape 变化大）；内存碎片导致复用率低 | 低：环境变量级；**不能与 `garbage_collection_threshold` 和 `max_split_size_mb` 共用** | 对比显存碎片率（`npu-smi info` + 内存 profiling）+ 复测 prefill/decode ms | `unset PYTORCH_NPU_ALLOC_CONF` 重启 |
| `PYTORCH_NPU_ALLOC_CONF` (garbage_collection_threshold) | `export PYTORCH_NPU_ALLOC_CONF="garbage_collection_threshold:0.95"` | 环境变量 | 垃圾回收阈值，为内存上限的百分比；内存申请到阈值时触发内存池空闲块回收 | 内存使用率高、空闲块堆积场景；建议由大到小调试 | 低：环境变量级；**不能与 `expandable_segments:True` 共用** | 监控内存回收频率 + 复测性能稳定性（抖动是否减少） | `unset PYTORCH_NPU_ALLOC_CONF` 重启 |
| `PYTORCH_NPU_ALLOC_CONF` (max_split_size_mb) | `export PYTORCH_NPU_ALLOC_CONF="max_split_size_mb:50"` | 环境变量 | 内存块允许切分上限（MB），大于等于该值的块不允许切分，减少碎片 | 内存碎片严重、大块被频繁切分场景；**须先采集内存 profiling，按算子内存申请降序排列，由大到小尝试** | 中：值过小可能导致大算子申请失败 OOM；不能与 `expandable_segments:True` 共用 | 监控 OOM + 对比碎片率 + 复测性能 | `unset PYTORCH_NPU_ALLOC_CONF` 重启 |
| `MULTI_STREAM_MEMORY_REUSE` | `export MULTI_STREAM_MEMORY_REUSE=1` | 环境变量 | 使能多流内存复用，让通信流内存提前释放供计算流复用，降低多流内存隔离开销 | 多流并行推理（计算流 + 通信流）；单流场景收益有限 | 低：环境变量级 | 对比显存占用 + 复测 prefill/decode ms | `unset MULTI_STREAM_MEMORY_REUSE` 重启 |
| `set_per_process_memory_fraction` | `torch_npu.npu.set_per_process_memory_fraction(0.95)` | 代码（Python） | 设置进程申请的内存上限（0~1），避免 PyTorch 耗尽 Device 内存导致其他组件申请失败 | 显存容量紧张、多组件共存场景 | 低：代码级；值过低可能导致 OOM | 监控显存使用上限 + 无 OOM | 移除该代码行重启 |
| `HCCL_BUFFSIZE` | `export HCCL_BUFFSIZE=200` | 环境变量 | HCCL 通信缓存大小（MB），默认 200；减小可释放显存 | 分布式多卡推理（world_size>1）；单卡无收益 | 中：值过小严重劣化通信速度；仅多卡场景适用 | 监控通信延迟 + 显存释放量 + 复测整体性能 | `unset HCCL_BUFFSIZE` 重启 |
| Python GC 优化 | `gc.set_threshold(700, 10, 5)` 或 `gc.disable()` | 代码（Python） | 调整 Python GC 回收频率或关闭自动回收，减少大量对象触发频繁 GC 导致的性能抖动 | 大量 Python 对象创建/销毁、GC 频繁触发导致性能抖动 | 中：关闭 GC 可能导致内存持续增长；须监控内存泄漏 | 监控 GC 频率 + 性能抖动是否减少 + 长时间运行内存稳定性 | 移除代码恢复默认 GC 策略重启 |

### 内存参数冲突矩阵

| 参数 A | 参数 B | 能否共用 | 说明 |
|--------|--------|----------|------|
| `expandable_segments:True` | `garbage_collection_threshold` | ❌ 不能共用 | 官方明确声明不能共用 |
| `expandable_segments:True` | `max_split_size_mb` | ❌ 不能共用 | 官方明确声明不能共用 |
| `garbage_collection_threshold` | `max_split_size_mb` | ✅ 可共用 | 多参数逗号分隔：`PYTORCH_NPU_ALLOC_CONF="garbage_collection_threshold:0.95,max_split_size_mb:50"` |
| `MULTI_STREAM_MEMORY_REUSE=1` | 上述 ALLOC_CONF 参数 | ✅ 可共用 | 独立环境变量，无冲突 |
| `HCCL_BUFFSIZE` | 上述参数 | ✅ 可共用 | 独立环境变量，仅多卡场景 |

### 内存参数选型决策

1. **先判断是否为动态 shape 场景**（LLM 推理 prefill/decode shape 不同）→ 是 → 优先 `expandable_segments:True`
2. **若内存碎片严重但不适合 expandable_segments** → 用 `garbage_collection_threshold:0.95` + `max_split_size_mb:50` 组合（须先 profiling 确定切分阈值）
3. **多流并行推理** → 叠加 `MULTI_STREAM_MEMORY_REUSE=1`
4. **显存紧张、多组件共存** → 叠加 `set_per_process_memory_fraction(0.95)`（代码级）
5. **多卡分布式推理** → 按需调 `HCCL_BUFFSIZE`（单卡跳过）
6. **GC 频繁触发导致抖动** → 叠加 Python GC 优化（代码级）

> **注意**：内存参数优化的收益主要体现在显存利用率提升（减少 OOM、增大 batch_size 空间）和性能稳定性（减少抖动），不必然直接降低 prefill/decode ms。验证时须同时关注显存占用、碎片率、OOM 频次和性能稳定性，而非仅看延迟指标。

### 单变量执行顺序

**调度类参数（优先执行）：**

1. 先开 `TASK_QUEUE_ENABLE=2`（环境变量，单变量，低风险，复测 prefill/decode）
2. 再单独验证 `--async-scheduling`（仅 server 模式）
3. 单独验证 `--enable-chunked-prefill`（长序列/在线场景，重点看 prefill 延迟）
4. 最后调 `--block-size=16`（影响显存，须监控 OOM）

**内存类参数（调度类完成后执行）：**

5. 先开 `expandable_segments:True`（环境变量，低风险，动态 shape 场景优先；与步骤 6/7 互斥）
6. 若步骤 5 无收益或不适用，改用 `garbage_collection_threshold:0.95`（须先 profiling）
7. 与步骤 6 组合 `max_split_size_mb:50`（逗号分隔；须先采集内存 profiling 确定阈值）
8. 单独验证 `MULTI_STREAM_MEMORY_REUSE=1`（多流场景）
9. 单独验证 `set_per_process_memory_fraction(0.95)`（代码级，显存紧张场景）
10. 多卡场景调 `HCCL_BUFFSIZE`（单卡跳过）
11. 验证 Python GC 优化（代码级，GC 抖动场景）

**组合验证：**

12. 组合正向项与 `synergy_candidate`，按 Synergy Detection 决定最终组合

## Recommended Inputs

- `workload_type` — 工作负载类型（database、compute、rpc、npu_llm_infer 等）
- `baseline_metrics` — 基线测试结果（含 TPS、QPS、p99 延迟等）
- `target_pid` — 目标进程 PID
- `current_round` — 当前优化轮次
- `effective_config_snapshot` — 当前已生效配置快照
- `previous_round_summary` — 上一轮优化摘要
- `restart_allowed` — 是否允许重启服务
- `benchmark_sequence_mode` — 测试模式（alternating / sequential）

## Expected Outputs

- `database_findings` — 数据库专项分析结论（数据库型工作负载时）
- `tps_decay_warning` — TPS 衰减告警
- `synergy_candidate_configs` — 单独负收益但组合有效的候选项
- `recommended_test_method` — 推荐测试方法
- `current_round_summary` — 当前轮优化摘要
- `selected_optimization_actions` — 当前轮被选中的优化动作
- `rejected_optimization_actions` — 当前轮被拒绝或暂缓的动作
- `iteration_decision` — `continue` / `stop`
- `iteration_decision_reason` — 继续或停止原因

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| 目标服务启停命令 | 配置变更后重启验证 | 无法验证配置变更效果 |
| 压测工具（sysbench 等） | 基线和复测 | 无法量化配置收益 |
| `mysql` 客户端 | 数据库状态采集 | 数据库型工作负载分析降级 |

## Test Methodology

对应用配置优化，默认采用规范化测试方法，而不是简单顺序对比：

- 每项配置变更前优先重启 MySQL 或目标服务，至少在首轮绝对对比时必须这样做
- 推荐使用交替测试法，而不是 `A A A -> B B B` 的顺序测试法
- 如果连续 3 次测试结果下降超过 2%，应输出 TPS 衰减警告
- 报告只记录最终可比较的性能值，不要求保存时序性能数据

推荐交替测试模式：

```text
for config in A B A B A B; do
  restart_service_with_config $config
  run_benchmark
done
```

## Synergy Detection

当前版本要求识别“单独负收益但组合有效”的配置项。

处理规则：

1. 先单独测试每项配置，记录独立收益
2. 若独立收益为负，则标记为 `synergy_candidate`
3. 将所有正向优化与 `synergy_candidate` 组合测试
4. 若组合中该候选项有效，则保留
5. 若组合仍无效，则剔除

输出中必须明确标注：

- 哪些配置仅在组合中生效
- 是否建议单独应用
- 是否纳入下一轮串行累计验证
- 哪些配置应作为 `next_round_candidate_configs`

## Dynamic Ordering

应用配置优化顺序不是固定不变的。

- 若 `bottleneck_classification == cpu_bottleneck` 且 `workload_type == database`
  - 推荐顺序：应用配置 → 性能库 → 亲和性 → OS → BIOS → 编译
- 若 `bottleneck_classification == cpu_bottleneck` 且 `workload_type == compute`
  - 推荐顺序：编译 → 性能库 → 亲和性 → OS → BIOS → 应用配置
- 若 `bottleneck_classification == accelerator_bottleneck` 且 `workload_type == npu_llm_infer`
  - 推荐顺序：编译/exe_mode → 应用配置（task_queue/async-scheduling/block-size）→ 性能库 → 亲和性 → OS → BIOS

输出应给出建议值、适用负载、风险、复测方法、推荐测试方法，以及配置协同关系说明。

在迭代编排语义下，本子 skill 还应补充：

- 哪些配置是当前轮最值得验证的动作
- 哪些配置虽然当前不优先，但应纳入下一轮继续验证
- 哪些配置已被当前轮证据否决，应暂缓或淘汰

## Candidate Action Contract

每个 `candidate_actions[]` 或 `selected_optimization_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。需要重启服务的配置必须明确 `restart_required=true`，并在 rollback 中给出恢复原配置和重启验证步骤。
