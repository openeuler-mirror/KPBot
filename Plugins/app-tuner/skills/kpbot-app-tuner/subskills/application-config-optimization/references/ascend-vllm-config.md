# Ascend NPU + vLLM 应用配置实战经验

本文件沉淀 Ascend910 + vLLM qwen2.5-1.5b 优化案例中的应用层参数调优实战经验，包含参数推荐表、实测收益记录、线程数调优实证、参数耦合互斥规则与单变量执行顺序。所有实测收益数据仅作参照，实际收益随模型/卡型/负载而异。

> **调用约定**：本 reference 由 `SKILL.md` 按需引用，不作为主流程的独立阶段。SKILL.md 已包含参数推荐表与单变量执行顺序的主表，本文件提供完整实证数据与进阶场景。

## 1. 实战收益记录

以下数据来自 Ascend910 + vLLM qwen2.5-1.5b 单流离线压测（prompt 1024, output 1024, parallel 1），仅供参考。

| 参数 / 组合 | 基线 | 优化后 | 收益 | 备注 |
|---|---|---|---|---|
| `TASK_QUEUE_ENABLE=2` | 129.89 tok/s | — | **+30.2%** | 单参数最高收益；启用流水线调度 |
| `OMP_NUM_THREADS=8` + `OMP_WAIT_POLICY=PASSIVE` + `MKL=8` + `OPENBLAS=8` | 826 线程, TTFT 高 | 82 线程 | **TTFT -68%** | 防止过提交；关键原则：等于实际并行需求而非 CPU 核数 |
| `PYTORCH_NPU_ALLOC_CONF="gc:0.95,max_split:50"` | — | — | **+3.2%** | 组合收益；须先 profiling |
| C8 应用配置合计 (TQE=2 + OMP=8 + gc:0.95 + split:50) | 129.89 tok/s | 188.41 tok/s | **+45.1%** | C8 入口基线起算的全部正向项组合 |
| `--enforce-eager` (小模型) | — | — | 启动必需 | vllm_ascend weak_ref_tensor 在非 eager 模式致命错误 |
| `--block-size` 从默认变更 | — | — | **-0.64%** (噪声) | NPU 默认已较优，保留默认 |
| `--enable-chunked-prefill` (单流离线) | — | — | **-1.63%** | 单流有害；仅高并发长 prompt 场景可开 |
| `--async-scheduling` | — | — | 被拒绝 | 与 TQE=2 冲突，NPU 场景优先 TQE=2 |

> **基线说明**：上表"C8 应用配置合计 +45.1%"以 C8 入口稳态 129.89 tok/s 为基线。案例归档（`examples.md`）中 C8 轮次相对上一轮（C5 绑核后 70.14 tok/s）的阶段增益为 +35.9%（≈+36%），两者基线不同、不冲突。SKILL.md 与 `examples.md` 中标注的"C8 +36%"指阶段增益，本表的"+45.1%"指 C8 入口基线起的组合增益。

## 2. vLLM 推理框架参数调优

所有参数均通过 vLLM server / offline 启动命令传入，变更后须重启服务复测。

### 核心启动参数

| 参数 | 默认值 | NPU 建议 | 适用场景 | 实战收益 / 注意事项 |
|---|---|---|---|---|
| `--gpu-memory-utilization` | 0.9 | 0.85–0.95（HBM 32GB 卡建议 0.90，64GB 卡可至 0.95；预留 CANN/runtime 开销） | 所有场景；控制 KV cache 与权重占用 HBM 比例 | 收益间接：提高可增大 `max-num-seqs`/`max-model-len` 空间；过高触发 OOM 或 CANN runtime 申请失败 |
| `--max-num-seqs` | 256 | 按 HBM 容量与 `max-model-len` 反推；小卡（32GB）建议 32–128，大卡（64GB+）可至 256 | 高并发在线服务 | 直接决定 batch 上限；过大导致 decode batch 抖动、TTFT 上升 |
| `--max-model-len` | 模型上限 | 与 KV cache 预分配耦合；按业务最长 prompt+output 设定，避免预留过多 | 长 prompt / 长输出场景 | 过大将挤占 `max-num-seqs` 空间；建议贴近 P99 实际长度 |
| `--enable-prefix-caching` | False | 开启；NPU 支持 paged KV，前缀命中场景收益明显 | 共享 system prompt / 多轮对话 / RAG | 命中率高时显著降低 prefill 计算；命中率低时管理开销可能为负 |
| `--tensor-parallel-size` (`-tp`) | 1 | 多卡时按设备数设；NPU 须配合 `ASCEND_RT_VISIBLE_DEVICES` 与 HCCL | 模型单卡装不下或需降延迟 | 受限于通信开销；小模型（<7B）单卡更快 |
| `--pipeline-parallel-size` (`-pp`) | 1 | NPU 多卡场景谨慎；与 `--tensor-parallel-size` 协同 | 极大模型跨卡拆分 | 通信气泡大，NPU 场景须实测；通常优先 `tp` |
| `--block-size` | 16 | NPU 场景需实测；默认 16 在 Ascend910 上观测为 -0.64%（噪声） | paged KV decode 路径 | 实战显示默认值在 NPU 上无明显收益；变更须监控 OOM 与 decode 访存 |
| `--enable-chunked-prefill` | False | 单流场景**有害**（实测 -1.63%）；高并发长 prompt 场景可开 | 高并发 + 长 prompt | 单流离线场景关闭；与 `--max-num-batched-tokens` 协同 |
| `--max-num-batched-tokens` | 2048 | 与 chunked-prefill 协同；单流场景设为 `max-model-len` 等价关闭 chunk | chunked prefill 启用时 | 单流 + chunked 实测 -1.63%；高并发可设 4096–8192 |
| `--async-scheduling` | False | **与 `TASK_QUEUE_ENABLE=2` 冲突**，NPU 场景优先 TQE=2 | online server 模式 | 实战中被拒绝（与 TQE=2 冲突）；非 NPU 场景可单独验证 |
| `--swap-space` | 4 (GB) | NPU 场景慎用 CPU swap，HBM↔Host 带宽受限 | 显存不足时降级 | NPU 上收益有限，建议优先优化 `gpu-memory-utilization` 与 `max-num-seqs` |
| `--cpu-offload-gb` | 0 | NPU 场景**不推荐**，Host↔Device 带宽瓶颈 | 显存极度不足 | 实战未见正向收益；优先考虑 `tp` 扩容 |
| `--enforce-eager` | False | **小模型（≤3B）开启有益**；大模型图捕获收益高则关闭 | 小模型 / shape 频变场景 | 实战 qwen2.5-1.5b 开启有益（图捕获开销 > 收益）；大模型反向 |
| `--dtype` | auto | NPU 推荐 `float16`；`bfloat16` 须确认 CANN 支持 | 所有场景 | 影响显存与精度；自动模式下须确认实际选中类型 |

### 调度与并发进阶参数

| 参数 | 默认值 | NPU 建议 | 适用场景 | 实战收益 / 注意事项 |
|---|---|---|---|---|
| `--schedule-conservativeness` | 1.0 | 长序列/显存紧张场景调高（1.2–1.5）；短序列可调低 | KV cache 调度保守度 | 影响 OOM 边界与吞吐权衡 |
| `--num-scheduler-steps` | 1 | NPU 场景可尝试 2–4（多步调度减少 Host 介入）；须实测 | decode 阶段流水 | 减少调度开销；过高可能影响响应性 |
| `--use-v2-block-manager` | True | 保持默认 | paged KV | NPU 已适配，关闭会退化 |

### NPU 场景优先验证顺序

1. `--enforce-eager`（小模型）/ 关闭 enforce-eager（大模型）— 区分图捕获收益
2. `--gpu-memory-utilization` 调至 0.90–0.95
3. `--max-num-seqs` 与 `--max-model-len` 联调（按 HBM 反推）
4. `--enable-prefix-caching`（有共享前缀时）
5. `--enable-chunked-prefill` + `--max-num-batched-tokens`（**仅高并发场景**）
6. `--block-size` 实测（默认值在 NPU 上倾向保留）
7. `--async-scheduling`（**与 TQE=2 互斥**，二选一）

## 3. Ascend NPU 专用参数

来源：昇腾官方文档与 Ascend910 + vLLM qwen2.5-1.5b 实战优化（C8 阶段应用配置优化合计 +36%，TQE=2 单参数 +30.2%）。

| 参数 | 默认值 | NPU 建议 | 适用场景 | 实战收益 / 注意事项 |
|---|---|---|---|---|
| `TASK_QUEUE_ENABLE` | 0 | **`2`**（最高收益） | NPU 推理（ge_graph / npugraph_ex），prefill/decode 延迟型 | **实战 +30.2%**；启用流水线调度，重叠 AICore 计算与 Host→Device 算子下发。`0`=关闭，`1`=仅计算流，`2`=完整流水 |
| `PYTORCH_NPU_ALLOC_CONF` (garbage_collection_threshold) | 无 | `0.95` | 内存碎片严重 | **实战 +3.2%**（与 `max_split_size_mb:50` 组合）；不能与 `expandable_segments:True` 共用 |
| `PYTORCH_NPU_ALLOC_CONF` (max_split_size_mb) | 无 | `50`（须先 profiling） | 大块频繁切分 | **实战 +3.2%**（组合收益）；值过小 OOM |
| `PYTORCH_NPU_ALLOC_CONF` (expandable_segments) | False | `True` | 动态 shape 场景 | 与 gc_threshold/max_split 互斥；二选一 |
| `ASCEND_RT_VISIBLE_DEVICES` | 全部 | 按需指定，如 `0` 或 `0,1` | 多卡选择 / tp 场景 | 等价于 CUDA_VISIBLE_DEVICES；错误配置导致 tp 失败 |
| `MULTI_STREAM_MEMORY_REUSE` | 0 | `1` | 多流并行（计算+通信流） | 释放通信流内存供计算流复用；单流无收益 |
| `HCCL_BUFFSIZE` | 200 (MB) | 多卡显存紧张时调小（100–150）；通信敏感时保持或调大 | 分布式多卡（world_size>1） | 单卡无收益；过小劣化通信 |
| `OMP_NUM_THREADS` | CPU 核数 | **等于实际并行需求**（实战 8） | NPU 推理 Host 侧算子下发 / CPU 密集算子 | **实战 -68% TTFT**（826→82 线程）；过提交导致上下文切换爆炸 |
| `OMP_WAIT_POLICY` | PASSIVE | `PASSIVE` | 配合 OMP_NUM_THREADS | PASSIVE 让等待线程不占 CPU，减少争用 |
| `MKL_NUM_THREADS` | OMP 值 | 与 `OMP_NUM_THREADS` 一致（实战 8） | MKL 算子 | 防止 MKL 单独过提交 |
| `OPENBLAS_NUM_THREADS` | OMP 值 | 与 `OMP_NUM_THREADS` 一致（实战 8） | OpenBLAS 算子 | 防止 OpenBLAS 单独过提交 |
| `PYTHON GC` (gc.set_threshold) | (700,10,10) | `(700, 10, 5)` 或 `gc.disable()` | 大量 Python 对象 / GC 抖动 | 减少频繁 GC 抖动；关闭须监控内存泄漏 |
| `set_per_process_memory_fraction` | 1.0 | `0.95`（代码级） | 显存紧张 / 多组件共存 | 限制 PyTorch 申请上限，留余量给 runtime |

### 线程数调优实证

Ascend910 + vLLM qwen2.5-1.5b 实战：

- 默认 `OMP_NUM_THREADS=CPU核数` → 826 线程 → 大量上下文切换 → TTFT 高
- 设 `OMP_NUM_THREADS=8` + `OMP_WAIT_POLICY=PASSIVE` + `MKL_NUM_THREADS=8` + `OPENBLAS_NUM_THREADS=8` → 82 线程 → **TTFT -68%**
- 关键原则：`OMP_NUM_THREADS` 应等于**实际并行需求**（NPU 推理 Host 侧算子下发并发度），**而非 CPU 核数**
- 配套参数必须统一：MKL/OpenBLAS 若单独保留默认（=CPU 核数）仍会过提交

### NPU 内存参数选型决策

1. 动态 shape 场景 → `expandable_segments:True`
2. 内存碎片严重（不适合 expandable）→ `garbage_collection_threshold:0.95,max_split_size_mb:50`（实战组合）
3. 多流并行 → 叠加 `MULTI_STREAM_MEMORY_REUSE=1`
4. 显存紧张 → 叠加 `set_per_process_memory_fraction(0.95)`
5. 多卡 → 按需 `HCCL_BUFFSIZE`
6. GC 抖动 → Python GC 优化

## 4. 参数耦合与互斥

### 互斥规则（不可同时启用）

| 参数 A | 参数 B | 互斥原因 | 处置 |
|---|---|---|---|
| `TASK_QUEUE_ENABLE=2` | `--async-scheduling` | 均作用于 Host-Device 调度流水，图捕获路径冲突 | NPU 场景**优先 TQE=2**；非 NPU 单独验证 async-scheduling |
| `PYTORCH_NPU_ALLOC_CONF=expandable_segments:True` | `garbage_collection_threshold` / `max_split_size_mb` | 官方明确声明不能共用 | 二选一：动态 shape 用 expandable；碎片严重用 gc+split 组合 |
| `--enforce-eager` | 图捕获模式（CUDA graph / npugraph） | enforce-eager 强制禁用图捕获 | 二选一：小模型用 enforce-eager；大模型用图捕获 |
| `--cpu-offload-gb > 0` | 高 `--gpu-memory-utilization` | offload 与高利用率争用显存 | NPU 场景不推荐 offload；优先扩容 tp |

### 单向有害规则（特定场景有害）

| 参数 | 有害场景 | 实证 | 处置 |
|---|---|---|---|
| `--enable-chunked-prefill` + `--max-num-batched-tokens=8192` | 单流离线场景 | 实测 -1.63% | 单流关闭；高并发再开 |
| `--block-size=16`（从默认调整） | NPU 默认已较优 | 实测 -0.64%（噪声） | NPU 场景保留默认，需实测再调 |
| `OMP_NUM_THREADS=CPU核数` | NPU 推理 Host 侧 | 实测 826 线程 → TTFT 高 | 设为实际并行需求（实战 8） |
| `--cpu-offload-gb` | NPU 场景 | Host↔Device 带宽瓶颈 | 不推荐；优先 tp 扩容 |

### 协同规则（建议组合）

| 参数组合 | 协同原因 | 实证收益 |
|---|---|---|
| `OMP_NUM_THREADS=8` + `OMP_WAIT_POLICY=PASSIVE` + `MKL_NUM_THREADS=8` + `OPENBLAS_NUM_THREADS=8` | 统一线程数，防止单独过提交 | **-68% TTFT**（826→82 线程） |
| `PYTORCH_NPU_ALLOC_CONF="garbage_collection_threshold:0.95,max_split_size_mb:50"` | gc + split 组合减少碎片 | **+3.2%** |
| `TASK_QUEUE_ENABLE=2` + `--enforce-eager`（小模型） | TQE 流水 + 禁用图捕获（小模型图捕获开销大） | C8 应用配置合计 **+36%**（阶段增益，见 §1 基线说明） |
| `--enable-prefix-caching` + `--schedule-conservativeness=1.2` | 前缀缓存 + 保守调度防 OOM | 共享前缀场景叠加收益 |

### 单变量执行顺序（含互斥规则）

1. `TASK_QUEUE_ENABLE=2`（**不与 `--async-scheduling` 共测**）
2. OMP/MKL/OpenBLAS 线程数统一（低风险，可与 1 同轮）
3. `PYTORCH_NPU_ALLOC_CONF` 二选一（expandable 或 gc+split）
4. `--enforce-eager`（小模型）或图捕获模式（大模型，**二选一**）
5. `--gpu-memory-utilization` + `--max-num-seqs` + `--max-model-len` 联调
6. `--enable-prefix-caching`（有共享前缀时）
7. `--enable-chunked-prefill`（**仅高并发**，单流跳过）
8. `--block-size` 实测（NPU 默认倾向保留）
9. 多流 / 多卡 / GC 进阶参数

> **关键提醒**：任何涉及调度路径的参数（TQE / async-scheduling / chunked-prefill / enforce-eager / num-scheduler-steps）变更后，须完整复测 TTFT + TPOT + throughput，不可仅看单一指标。
