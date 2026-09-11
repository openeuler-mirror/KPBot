---
name: ort-kdnn-attention-opt
description: Optimize ONNX Runtime CPU attention and surrounding subgraphs through bottleneck analysis, aggressive fusion, custom kernels, layout and scheduling changes, and benchmark validation. Includes optional KDNN/AArch64 integration examples; useful with other CPU backends as well.
---

# ORT CPU Attention 与周边子图优化

从实际计算和数据流寻找优化机会，产出可验证的实现与收益证据。KDNN/AArch64 是详细集成案例，不是使用前提；可使用 MLAS、其他 CPU 库或自包含 kernel。保留现有 skill 名称以兼容已有调用。

## 范围与使用方式

适用于 attention、投影、布局转换和相邻算子链的 CPU 推理优化，包括 decode、prefill、不同 mask、dtype 与动态 shape。历史 Sq=1 路径只是起点。GPU/训练需采用相应后端的方法；若 CPU 热点落在 attention 之外，跟随证据处理实际瓶颈，不强行改写成 attention 问题。

用户只要诊断、方案或单点修复时，完成相应部分即可；不自动扩展为全模型优化、重建后端或完整压测项目。端到端任务可按下面流程迭代，不要求每次加载全部参考或执行固定阶段。

先从当前环境确认模型/opset、执行提供程序、版本、CPU 能力与可用构建和测量方法。详细参考中的 `kdnn_*` 文件、`ORT_KDNN_*` 开关及 benchmark flags 是定制分支示例；用当前源码和工具帮助定位等价入口。源码、构建、模型和结果路径沿用用户配置，不推断个人目录或机器编号。

## 工作方法

1. **建立足够的证据**：复现目标负载，区分冷启动与稳态，结合图、profile、调用栈和内存成本判断瓶颈。沿生产者/消费者统计候选边界的搬运、分配、dispatch、pack 与计算；不要只按 attention 本体占比提前排除。读 [模式识别](references/pattern-recognition.md)。扫描脚本仅给候选，未发现模式不代表没有机会。
2. **生成与比较候选**：从当前数据推导方案，参考下面探索方向。记录预计消除的成本、前提和最小验证实验。旧实现不支持的形态可通过新 schema/kernel/布局扩展；历史失败需要核对条件，不作为禁用列表。
3. **实现最小原型**：可从简单 kernel 起步，也可直接实现依赖联合布局或流水的整体方案。复用现有后端或编写专用算子都可；不用为了符合案例先完成不需要的 cache、adapter 或中间版本。读 [kernel 设计](references/kernel-design.md)；接入 KDNN 时再读 [集成示例](references/integration.md)。
4. **验证并迭代**：确认实际路径命中，按改动选择数值、图重写、回退、内存及并发测试。探索性能可先记录并标明验证缺口；可交付收益结论须有对应正确性证据。读 [验证](references/validation.md)。单变量用于归因；存在依赖时先验证组合，再消融可分离部分。
5. **报告任务所需结论**：固定条件、多轮配对或随机化顺序，报告指标定义、波动与适用范围。服务收益用服务测量支持，微基准结论限于被测算子；无需为局部问题强制搭建服务。读 [测量](references/benchmarking.md)。证据不足时明确保留结论，有效或无效的实验都可反馈下一轮候选。

## 主动探索方向（开放的候选种子）

- **整块融合**：Q/K/V 投影、bias、拆头、attention、并头，进一步探索输出投影、residual 和归一化。比较联合投影、融合 epilogue 与跨阶段流水；已有独立融合不排除继续跨边界复用。
- **数据布局与专用 kernel**：生产者直写消费者布局、NEON/SVE 或目标 CPU 的专用实现、多 head/query 联合计算、整批 adapter、pack 复用、分块/在线 softmax。按工作集和访存成本选方案，不按 Sq 单一条件决定。
- **扩展支持范围**：单头、多 query、per-head/causal mask、动态 shape、其他 dtype；共享结果可用多输出融合或保留支路处理。旧 matcher 的拒绝条件不等于新算子的永久限制。
- **计算复用与调度**：消除重复投影/转换，比较 session 复用、有界缓存与无状态实现，联合设计 batch/head/query 任务粒度。任务涉及增量推理时可探索 K/V 复用，明确序列身份、失效和内存预算。

候选可以超出以上清单，也可以最终选择较小融合或不融合。更大的边界可能降低图并行度、扩大工作集或失去成熟 GEMM 路径；用原型比较这些代价。低精度或近似数学须符合任务已有精度契约，不静默放宽误差。

## 必须保留的工程语义

- **原图契约**：保留运算顺序要求、scale、mask、广播、布局、外部消费者和图输出。无 scale 运算的倍率为 1；全 -inf、+inf、NaN 按目标原图参考验证，不能直接套用案例自定义语义。尚未实现的组合保留原图。
- **生命周期与并发**：缓存 key 覆盖实际语义和源数据身份，并保护在飞引用；workspace 匹配算法、大小、对齐和调用生命周期。依赖 SVE VL 的路径须保证当前线程配置兼容。内存改动使用适用的 sanitizer 或等价诊断，无法执行时标明验证缺口。
- **可复现对照**：保留参考路径或参考构建，不强制每项一个 env 开关。env 是进程级状态，不能通过并发修改它隔离 session。明确自定义算子模型的目标运行时兼容性及回退方式。

## 按需参考与工具

- [模式识别](references/pattern-recognition.md)：tf2onnx 示例与可扩展匹配边界。
- [kernel 设计](references/kernel-design.md)：布局、调度、cache、workspace 和案例算子契约。
- [KDNN 集成](references/integration.md)：仅在相应后端接入时使用。
- [验证](references/validation.md)、[测量](references/benchmarking.md)：按目标选择测试与指标。
- [历史实验](references/history-lessons.md)：需要理解某条旧路径的失败条件时再读，不作为预期收益或否决依据。
- `scripts/scan_attention.py`：顶层标准模式的启发式扫描，不能证明可安全融合。
- `scripts/run_ab_benchmark.sh`、`scripts/aggregate_results.py`：适配特定 harness 的可选 A/B 工具；固定 sequential、至少三轮及默认跨轮 CV 门槛属于工具协议，其他负载可使用更合适的测量工具。
