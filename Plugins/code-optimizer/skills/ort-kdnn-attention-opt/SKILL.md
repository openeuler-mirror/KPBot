---
name: ort-kdnn-attention-opt
description: ONNX Runtime + KDNN(ARM AArch64/NEON/SVE)上的 attention/MHA 端到端优化技能。覆盖:ONNX 图中 attention 子图的模式识别与瓶颈定位、融合算子设计(pack/no-pack 布局、联合 GEMV、SVE kernel)、ORT 图优化 pass 与自定义算子接入、KDNN primitive 复用/cache 设计、正确性验证(A/B 对比、边界用例、并发、cache 碰撞)与消融基准协议。凡是在本代码库优化 transformer/attention/softmax+MatMul 推理性能、分析 model4 类模型瓶颈、实现/评估 attention 融合收益、或排查融合算子正确性问题时,即使没有明确说出 "attention" 或 "MHA",都应使用本 skill。
---

# ORT + KDNN Attention/MHA 端到端优化

本技能把一条已验证的优化路径沉淀成可复现的流程:从"模型里有 attention、跑得慢"开始,到"融合算子 +
专用 kernel + 可复核的性能报告"结束。核心方法论:**先让正确的基线跑起来,再做每一步都能被 A/B 证伪的增量**。

## 0. 适用与不适用

适用:
- ONNX 模型含 `Q·Kᵀ → scale → mask → Softmax → ·V` 结构(自注意力/target-attention,transformer 类);
- 运行环境是 ONNX Runtime CPU EP,且链接了 KDNN 库(ARM AArch64,NEON/SVE);
- 目标是降低端到端推理时延,需要量化每一步优化的真实收益。

不适用/先想清楚:
- **prefill/训练场景(Sq 远大于 1)**:本技能的 kernel 设计以 `Sq=1`(decode/单 query)为主战场;Sq>1 时
  分块 flash-attention 思路才有意义,单 query 场景做 flash 是负收益(attention 矩阵只有一行,没有可省的
  中间矩阵)。
- **attention 计算占比 < 3% 的模型**:端到端收益上限太低,先去做占比大的部分(用 profile 确认,不要猜)。
- 需要跨请求复用 K/V 的"KV cache"功能:这是调度层功能,不是 kernel 优化,另行立项。

## 1. 总工作流(七阶段)

按顺序执行。每个阶段有明确的产出物和"不通过就停下来"的判据,防止在错误方向上加速。

### Phase 0 侦察(recon)
产出:模型结构清单 + attention 占比数据。
1. 用 `scripts/scan_attention.py` 扫描模型,列出 attention 子图数量、每个的 (B, Sq, Sk, H, d) 形状、
   mask 形态。读 `references/pattern-recognition.md` 确认哪些子图可融合、哪些必须拒绝。
2. 跑一次带 profile 的端到端基线(harness 自带 `--enable-profiling`,或 ORT profiling),确认 attention
   链(含外围 Transpose/Reshape)的真实耗时占比。**占比决定一切后续决策**——单算子 55% 的提升折算到
   端到端通常只剩 1~2%,先知道天花板在哪。

### Phase 1 基线(baseline)
产出:可复现的基线数字。
1. 按 `references/benchmarking.md` 的构建序列编译(先 KDNN 后 ORT;离线环境用
   `FETCHCONTENT_SOURCE_DIR_*` 复用依赖源码,见该文档 §1)。
2. 固定测量协议(NUMA/物理核绑定、sequential、intra/inter 线程、warmup/iter、交替 ≥3 轮、CV≤5%),
   记录基线 mean/median/p95/CV。**没有可信基线,后面所有对比都是自欺。**

### Phase 2 设计(design)
产出:一页设计决策记录(写进最终报告)。
1. 读 `references/kernel-design.md` 的决策树:融合边界(投影后 Q/K/V 还是含投影)、布局(pack vs
   no-pack)、kernel 路径(逐 head GEMV vs 联合多 head)、复用策略(primitive cache)。
2. 关键原则:**布局决定带宽,带宽决定 decode 场景的性能**。projection 输出是 `[B, S, H·d]` 交错布局;
   Sq=1 时 QK/PV 都是 GEMV,任何把 K/V 复制到 head-major 的 pack 都是纯开销。
3. 所有新开关必须默认关闭、由环境变量门控,保证一条命令回到基线行为。

### Phase 3 实现(implement)
产出:代码 + 注册点。按 `references/integration.md` 的 checklist 接入(自定义 op schema、kernel 注册、
图优化 pass、测试)。数值语义(softmax 稳定性、mask 边界、scale)以 `references/kernel-design.md` §6 为准,
那里有逐条语义表——错一条就是静默算错。

### Phase 4 验证(validate)
产出:全部通过的验证证据。按 `references/validation.md` 的清单执行:单元参考对比、边界用例(全 -inf
mask、+inf ties、NaN、奇数形状、batch 广播)、端到端 A/B(优化开 vs 关,atol/rtol 1e-4)、同 shape 异
scale 的 cache 碰撞回归、并发/重复 Run。**任何性能数字在正确性闭环之前都不许出现在报告里。**

### Phase 5 消融(ablate)
产出:消融矩阵。只切换一个变量(algorithm/gemv 模式/cache/batch),其余全部固定,交替 ≥3 轮。协议见
`references/benchmarking.md`。用 `scripts/run_ab_benchmark.sh` 和 `scripts/aggregate_results.py` 保证
口径一致。

### Phase 6 报告(report)
按 `references/benchmarking.md` §5 的模板写:配置、数字、CV、结论边界(哪些结论只在本机本 shape 成立)。

## 2. 黄金法则(违反任何一条,结果不可信)

1. **正确性优先于性能**。每个优化增量都要有"开 vs 关输出一致(atol/rtol 1e-4)"的证据,再谈时延。
   写参考实现时注意:双精度累加、逐 (batch, head, row) 独立计算、mask 的 batch 维广播要显式处理——
   参考实现自己的 bug 会浪费一整天排查(实测发生过:参考实现按 batch 索引共享 mask 越界,表现为
   "batch 0 对、batch 1 错"的假阳性)。
2. **fail, don't guess**。图匹配器遇到不认识的 mask/scale/布局组合必须拒绝融合(保持原图),绝不
   "差不多就融合"。静默算错比不优化糟糕得多。
3. **A/B 隔离**。对比两组配置时,除被测变量外一切固定——包括其他优化开关。测量要交替进行
   (A,B,A,B...),不要先跑完 A 再跑 B(热身状态、频率漂移会系统性偏置)。
4. **CV>5% 的轮次作废**。单轮数字没有意义;median-of-medians + 报告 CV。CPU 频率、NUMA、SMT、
   后台负载都会毁掉测量。
5. **构造开销 × batch 次**。ORT 把 batch 维拆成 B 个 B1 task 分别执行;任何"每次调用都构造"的对象
   (GEMM 描述符、pack 后的权重、查找方案)都会被放大 B 倍。要么消除构造,要么 cache(见
   `references/kernel-design.md` §5 cache key 完备性清单——漏一个字段就是静默算错)。
6. **不外推单算子比例**。单算子 +55% 端到端可能只有 +2%(attention 占比 ~11% 时)。报告必须两端
   数字都有。
7. **改内存/workspace 相关代码后至少跑一次 ASan**(KDNN 与测试程序同一 sanitizer 配置),受限容器用
   `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1`。

## 3. 关键决策速查

| 问题 | 判据 | 推荐 |
|---|---|---|
| Sq==1? | 模型形状 | 是 → GEMV 路径 + no-pack;否 → 考虑分块/flash 思路 |
| K/V 布局 | projection 输出 `[B,S,H·d]` 交错 | Sq=1 用 stride view 直读,不 pack |
| mask 形态 | `[B or 1, 1, 1, Sk]` 加性 | 其他形态(per-head、非加性)拒绝融合 |
| head 数/维度 | H、d 与 D≤32 个 SVE 向量 | 超限自动回退通用逐 head GEMV,数值语义不变 |
| 复用 | 每次构造有可测开销 | 两级 cache(全局 LRU + 线程热 entry),key 含全部语义参数 |
| 线程 | 外层已由 ORT 线程池并行 | worker 内单线程,禁止嵌套并行 |
| workspace | 尺寸随算法/线程数剧变 | 每次构造后重查 size,thread_local grow-only |

## 4. 已知陷阱(全部来自真实踩坑)

- 参考实现的 mask batch 广播默认值 bug(见法则 1);
- cache key 漏 scale/算法/SVE VL → 同 shape 不同参数静默复用错误 primitive;
- workspace 尺寸依赖算法(classic 与 no-pack 相差 3~4 个数量级),切算法不重查 → 越界写;
- SVE VL 是**每线程**属性,构造与 Run 必须同线程同 VL;
- 全 -inf mask 行的输出是全 0(不是 NaN),+inf 并列时概率均分,NaN 传播——三条语义两套实现都要一致;
- ORT format(.ort)模型会把融合算子烧进文件:非对应 build 加载即失败,且没有回退路径。保存 .ort 前
  想清楚目标环境。

## 5. 参考资料索引

按需加载,不必全读:

| 文件 | 什么时候读 |
|---|---|
| `references/pattern-recognition.md` | Phase 0:识别/拒绝 attention 子图,形状与占比分析 |
| `references/kernel-design.md` | Phase 2/3:布局、kernel、cache、数值语义的核心设计知识 |
| `references/integration.md` | Phase 3:ORT 侧注册 checklist、环境变量门控模式、构建 |
| `references/validation.md` | Phase 4:验证清单与测试写法 |
| `references/benchmarking.md` | Phase 1/5/6:构建序列、测量协议、消融设计、报告模板 |
| `references/history-lessons.md` | 想知道"为什么不用 flash / 整批 adapter / dispatch counter"时 |
| `scripts/scan_attention.py` | Phase 0:模型扫描 |
| `scripts/run_ab_benchmark.sh` | Phase 1/5:协议化 A/B 测量 |
| `scripts/aggregate_results.py` | Phase 5/6:多轮结果聚合与 CV 判定 |
