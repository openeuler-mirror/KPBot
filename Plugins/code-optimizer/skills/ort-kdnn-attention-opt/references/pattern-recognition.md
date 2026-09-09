# 模式识别:在 ONNX 图中找到并评估 attention 子图

Phase 0/2 使用。目标:确定模型里有哪些 attention、它们的形状与形态、占多少耗时、哪些能融合。

## 1. 目标子图特征(tf2onnx 导出形态)

target-attention / self-attention 在 tf2onnx 导出后是**10+ 个算子的链**,不是单个节点。完整签名
(以 model4 的 target-attention 为例,Q 来自目标物品 Sq=1,K/V 来自用户行为序列):

```
embedding → [Q/K/V 三路各自的投影链]:
    Add(bias) → Reshape(3D→2D, 零拷贝) → [Transpose] → MatMul(权重) → Reshape(2D→3D)
    → Reshape(head-split, [B,S,H,d]) → Transpose(Q:[0,2,1,3], K:[0,2,3,1], V:[0,2,1,3])
→ MatMul(Q, K)            # scores [B,H,Sq,Sk]
→ Mul(c) 或 Div(c)        # scale,c 是常量 initializer;c 常见 = sqrt(d)
→ Add(mask_bias)          # 加性 pre-softmax 偏置(可能整条缺失)
→ Softmax(axis=-1 或 3)   # opset<13 时 axis=1 等价改写
→ MatMul(probs, V)        # context [B,H,Sq,d]
→ Transpose(并头) → Reshape
```

识别要点:
- **锚点是 Softmax**:自 Softmax 向前看恰好 1 个消费者且是 `MatMul(probs, V)`(softmax 输出必须在
  slot 0)。这是区分真 attention 与普通 logit-softmax 的第一信号——后者的消费者是 Slice/ReduceMean 等。
- **向后回溯**:`Add(scaled, bias)` → `Mul/Div(qk, const)` → `MatMul(Q, Kᵀ)`,且 QK 的两个输入都是
  计算值(非 initializer)。`Div(const, qk)`(反向除)是陷阱,必须拒绝。
- **mask 的构造模式**(tf2onnx 常见):`Mul(neg_const, Sub(one_const, keepmask))`,keepmask 经
  `Reshape(mask_input)` 得到,形状 `[B|1, 1, 1, Sk]`,可能再 `Tile` 到 `[B,H,1,Sk]`。融合时应从
  pre-Tile 的 keepmask 和原始常量**重建**干净的 `[B|1,1,Sk]` 加性 bias,而不是复用 Tile 之后的节点。
- **scale 折叠**:图上 `Div(qk, sqrt(d))` 与 `Mul(qk, 1/sqrt(d))` 等价;`Mul` 更常见于导出器优化后。

## 2. 接受/拒绝清单

匹配器必须**对不认识的形态拒绝融合(保持原图),绝不猜测**:

| 特征 | 接受 | 拒绝(理由) |
|---|---|---|
| softmax 消费者 | 恰 1 个,`MatMul(probs,V)` | 多消费者 / 图输出(删了会断流) |
| scale | `Mul(qk,c)`、`Div(qk,c)`、裸 MatMul | `Div(c,qk)`(语义不同) |
| mask 形状 | `[B 或 1, 1, 1, Sk]` 加性 | per-head `[B,H,Sq,Sk]`、中间维非 1、非加性(乘性/Where) |
| QK 共享输出 | — | scores 另有消费者(fuse 会静默断流) |
| head 数 | ≥ 2 | 1(单头无 head 级摊销,融合反而慢) |
| rank-4 输入 | Sq==1 | Sq>1(输出布局需显式转置,除非实现了它) |
| dtype | float32 | 其他(先做 float32) |
| 投影链 | BiasAdd→Reshape→Transpose→MatMul 可完整回溯 | 投影结构不完整(融合边界决定,见下) |

**融合边界**:`Q·Kᵀ + scale + mask + Softmax + ·V`(含拆头/并头的 Reshape/Transpose)。是否包含
Q/K/V 投影**取决于基线状态**(消融实验修正的决策规则):
- 投影侧已有独立图 pass(TensorDot 融合、no-op Transpose 消除等)处理 → **不含投影**(重复吞入
  无益,大 GEMM 留给 MLAS/KDNN);
- 从零基线(那些 pass 不存在)→ **评估"整块融合"**:3 个投影 GEMM 合并为 2 个(Q 独立 + K|V
  权重拼接)、权重预打包、K/V 偏置代数折叠进点积、常量 mask 折叠。实测可比"仅 attention 核心"
  多拿 ~6% 端到端(投影侧转置/形状机/BiasAdd 约占子图时间 70%)。用 profile 确认后决定。
拆头/并头的 Transpose/Reshape 是融合的主要收益来源之一,无论哪种范围都必须圈进来。

## 3. 扫描工具

用 `scripts/scan_attention.py`(需 python3 + onnx 包)扫描:

```bash
python3 scripts/scan_attention.py --model onnxruntime/test/alipay/models/model4.onnx
```

输出:每个 attention 块的 (B, Sq, Sk, H, d) 推断(能推则推)、mask 形态、可融合判定。无 onnx 包时
的降级手段:跑 benchmark 加 `--optimized-model-path` 落盘优化后图,再 `strings`/python 解析融合节点
数量;或开 ORT 日志看 fusion pass 的 INFO 行。

## 4. 形状语义:先分类,再设计

拿到 (B, Sq, Sk, H, d) 后第一件事:

| 判定 | 含义 | kernel 设计方向 |
|---|---|---|
| **Sq == 1** | decode/单 query(target-attention 推荐场景) | attention 矩阵只有一行 `[H,Sk]` → 纯 GEMV 问题;布局与访存连续性决定性能;flash/online-softmax 无意义 |
| Sq > 1 且 Sk 大 | prefill/批量 | 真矩阵乘;分块 + online softmax(flash 思路)才可能省带宽;workspace 与数值稳定性设计不同 |
| H·d == hidden 且 hidden 可整除 | 标准拆头 | rank-3 `[B,S,hidden]` 与 rank-4 `[B,S,H,d]` 内存等价,kernel 可统一按 `[B,S,hidden]` 处理 |
| K/V batch 与 Q 不同(=1 或 =B) | 广播 | 逐 batch 指针 stride 处理(stride=0 即共享) |

## 5. 瓶颈定位:用数据决定做不做

融合前先 profile(ORT `--enable-profiling` 或 session opt),按算子类聚合。model4 的真实案例
(B=64, 16C32T,融合 OFF,10 次推理累计 2554.5ms):

| 计算类型 | 耗时 | 占比 |
|---|---:|---:|
| QKV 投影 MatMul(大 GEMM) | 405.7ms | 15.9% |
| **attention 拆头 Transpose(真转置)** | 386.2ms | **15.1%** |
| **投影 Transpose(恒等排列仍真拷贝)** | 289.2ms | **11.3%** |
| 投影 Reshape(零拷贝,纯 dispatch) | 264.8ms | 10.4% |
| 其他 MatMul(CGC dense) | 300.0ms | 11.7% |
| Concat / LayerNorm | 191.5 / 155.4ms | 7.5% / 6.1% |
| attention 其他(Add/Softmax/Div) | 83.0ms | 3.2% |
| **attention MatMul(QK+PV 本体)** | 39.1ms | **1.5%** |

结论模式:**attention 的 FLOPs 只占 1.5%,瓶颈在数据搬动(Transpose 合计 26.4%)和 dispatch**。
所以优化主线是"融合 + 布局",不是"写更快的 GEMM kernel"。如果你的模型 profile 显示 attention
本体占比高(如 >20%),kernel 质量才是主线——先看数据再选路线。

同时确认端到端占比上限:attention 全链占比 P%,kernel 就算无限快,端到端收益上限也是 P%。
model4 的 6 个 MHA 融合后占端到端 ~11%(B128),这就是为什么单算子 +55% 只折算成端到端 ~2%。
