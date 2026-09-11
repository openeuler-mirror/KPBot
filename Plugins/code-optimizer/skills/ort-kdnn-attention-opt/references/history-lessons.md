# 历史实验：当时的结果与适用条件

> 以下数字、失败方向和生态比较仅描述历史实验，不代表当前平台或最新依赖的能力。新任务按实际版本与测量重新判断。

按需用于解释历史失败。以下是特定实现的实验记录；当 shape、布局、调度、后端或融合边界改变时，鼓励重试并记录新证据，不据此建立禁止探索清单。

## 1. flash / online-softmax 系

| 方向 | 结果 | 原因 |
|---|---|---|
| flash 用于 Sq=1(decode) | 当时未获益 | 当时工作集下，减少 scores 物化未抵消 rescaling 成本；长 Sk 或新分块方案可重测 |
| flash_pack(PackHeads + head-in-batch generic GEMM) | **负优化**:微基准 1.09ms vs no_pack 0.90;端到端 B64 135.8 vs 124.1ms | generic GEMM 对 M=1 比 skinny GEMV 慢 + PackHeads 拷贝又回来了 |
| flash prefill(Sq>1) | 比 classic 慢 2~8×(B1/Sq1024:139 vs 73.8ms);1T→8T 几乎不扩展(141→139) | block GEMM 单线程 + generic 实现对 128×512×64 小块利用率低、权重 pack 开销;未调优即放弃,回退 classic |
| bp4x4vl 微内核用于 decode | 无用 | 要求 m≥4,decode 的 M=1 永远进不去(prefill 块上实测 7.4×,但那是另一场景) |
| AUTO 算法选择、q/kv_block_size 参数 | 从 API 撤下 | 实测无 prefill 胜出 shape;block size 只配当 debug 配置不进用户接口 |

**flash 迁移的真实留存价值**:workspace 结构性下降(decode 270MB→4.4KB;prefill 84MB→296KB)、
pack/reorder=0 的确定性、在线 softmax 的算法正确性基础(classic_no_pack 本质是 flash 在 br=1/bc=Sk
的特例)。也就是说:**做 no-pack 拿到了 flash 的内存收益,避开了它的调度代价**——这是当时工作集下的选择。

## 2. 并行/线程系

| 方向 | 结果 | 原因 |
|---|---|---|
| H 维(head 维)并行 | 负优化 | 每 task 只有 ~3200 FLOPs,调度开销盖过收益;batch 维并行才够粗 |
| `SetNumThreadsLocal(1)` 压 GEMM 线程 | 不可靠 | THREADPOOL 构建下线程数来自 active threadpool;要显式策略/RAII scope |
| `ORT_PARALLEL` 作核心缩放配置 | 只作单独实验 | inter/intra 两个 pool 同时争核;性能口径固定 sequential + inter=1 |
| per-head GEMV 的 no-pack v1 | 比 classic 慢 12%(MHA 节点 37.5 vs 33.6ms) | batch 并行下 4 次 per-head dispatch > 1 次 batched;后来 head-in-batch 视图修复(-5~6%) |
| 嵌套并行(worker 内再开线程) | silent 回退 | KDNN OMP 运行时嵌套层直接串行 |

## 3. 集成/工具系

| 方向 | 结果 | 原因 |
|---|---|---|
| 依赖 ORT Level1 DCE 清死节点 | 不可靠 | 死 4D Transpose 实测 ~5.7ms/call 留在图里;pass 内必须显式删 |
| `strings` 估算融合节点数 | 禁止 | 用 ONNX parser 精确计数(如 6 个融合节点替换 12 个 MatMul) |
| 单头(num_heads=1)融合 | 反而慢 | 无 head 级摊销,pack/workspace/dispatch 成本无处摊 |
| 调试计数器进生产 | 撤下 | 可观测性先行是方法论(先证明路径命中),但 dispatch counter 不进交付 |
| 首轮就上 SVE FastExp | 推迟 | 算法变化与近似误差不得混在一起;std::exp 先行,FastExp 单独定容差(≤1e-3)后才能默认 |
| TF 参考实现的 `ReducedMax` 用 -1e9 初始化 | 不采纳 | 用 -inf + 显式 old==new==-inf 处理,-1e9 会与合法大值混淆 |

## 4. 当时未实现的功能（可作为后续候选）

- causal 枚举:调用方在 mask 里填 -inf,算子不感知语义;
- KV cache 内置:由上层把 past+current 拼成 `[B,Sk,hidden]` 再进来;(评估结论:attention-model-A 场景 KV cache
  上限 ~10%,vLLM 不可用,未做)
- fp16/bf16、logsumexp 输出、dropout:与首次迁移解耦;
- K/V projection 换 GEMM 后端:绝对值无进步(K=128 无优势 + 失去 PrePack;先前的 5~7% "收益"是
  基线退步假象)。

## 5. 微基准与端到端脱节的根因(最重要的一条)

曾出现:微基准 no-pack 提升明显,端到端 A/B 却在噪声内。根因三连:
1. **口径不一致**:历史端到端数据用 execution-mode=parallel(inter/intra 竞争),微基准单线程;
2. **占比**:attention 本体只占端到端 22.6%(融合后口径更低),M=1 GEMV 的 FLOPs <0.1%;
3. **路径未证明命中**:没有 dispatch 计数证明 no-pack 真的跑在 GEMV 上(pack_bytes=0/reorder=0)。

当时的复测方案（非所有任务的固定配置）:可观测性先行(dispatch/路径计数)→ sequential+inter=1 固定口径 →
交替多轮 + CV 门槛 → 单算子与端到端双数字并报。

## 6. KDNN MHA API 的生态对标(设计新算子接口时参考)

- 选择接口前核实当前目标库的 attention/SDPA、线程与布局能力；不沿用历史版本“缺少某 primitive”的结论。
- 语义对齐 PyTorch SDPA / cuDNN:Q/K/V + scale(默认 1/sqrt(d)) + additive pre-softmax mask;
- 形态对齐底层计算库:pImpl 单指针 ABI、裸指针 + 构造期标量 shape、显式 workspace 协议、Options
  枚举;scale/mask 是构造参数而非 Run 参数(Run 保持 6 参热路径)。
