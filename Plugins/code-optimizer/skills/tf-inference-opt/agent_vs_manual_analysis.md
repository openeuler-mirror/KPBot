# 三方优化对比与并发域分析：skill agent / noskill agent / 人工版

> 历史案例：模型名、分支和提交号仅用于区分实验对象，不是当前环境前提。`${EXPERIMENT_ROOT}` 为用户提供的历史产物根目录，`${TF_SOURCE_DIR}`、`${SERVING_BUILD_DIR}`、`${TRANSCRIPT_DIR}` 分别为源码、构建与会话记录目录；未随 skill 打包的日志不可视为已核验或可访问的证据。
> **日期**：2026-09-07
> **数据来源**：`REPORT.md` §2.1–2.8（消融实验、头对头、8/12 口径两轮）、`bench_812_round2/summary.json`（全并发梯度）、双侧 agent 报告（`results/{skill,noskill}/REPORT.md`）、round-2 server 日志的融合命中取证（本文 §2.3 新证据）
> **回答的问题**：① 两个 agent 与人工版各自的优化点有什么不同；② 为什么高并发下人工版更好、低并发下 agent 更好

---

## 1. 三方优化点全景

### 1.1 按优化层分类汇总

| 优化层 | skill agent | noskill agent | 人工版（最新代码 e40873201） |
|---|---|---|---|
| **L1 图层：稀疏 embedding 链融合** | 启用树内既有 `KPFusedSparseDynamicStitch` / `KPFusedSparseSegmentReduce`（env 开关），第一轮就命中 | 同样启用既有设施（报告初版写完后回头补测，晚 ~40 分钟） | 同类能力且更全（stitch v2、`KPFusedSparseDynamicStitchMean`/`FastMean` 变体、embedding action-id gather、target-behavior interaction），全开下已命中 |
| **L1 图层：MatMul→Bias→激活 链融合** | 启用既有 `KPFusedMatMulRewriter` + **扩展平台门控 0xd03**（+1 行）→ 每模型 2–8 个 `_FusedMatMul` 节点 | **从零手写 384 行** `KPMatMulBiasScaleFoldRewriter`（scale 折进权重的数学等价变换），逐节点命中日志 | ⚠ **全开下零命中**——树内重写器存在但平台门控 `enabled_fused_matmul_rewriters()` 仍只认 0xd06，本机 0xd03 被挡（§2.3 新证据） |
| **L1 图层：BN 常量折叠** | 修复 3 个上游缺陷后启用：Identity→Const 读链解析、双 Const 角色冲突（按 shape 区分 epsilon/variance + 角色至多分配一次）、**折叠时机挪到 session 创建之前**（原 loader 路径是死代码）→ 命中 hmv 4 / adx 6 / presort 6 | （用自写 fold 重写器部分覆盖等价收益：scale/bias 一起折进权重） | 树内 `KPFusedMatMulBiasAddBNRewriter` 可用但未修上述缺陷 → 命中 hmv 2 / adx 4 / presort 2 / cvr 0（约为 skill 的一半） |
| **L2 算子/内核层** | hmv 启用 KDNN SVE JIT（瘦小 MatMul，消除 Eigen 跨线程协调）；**adx/cvr/presort 逐模型实测后不用 KDNN**（adx 实测 −15%，归因 per-chunk 打包 + 切分粒度） | KDNN SVE 改为 HWCAP 探测默认启用 + **`TF_KDNN_NUM_THREADS=2` 线程上限**（消除 16 路 parallel_for 的 p99 肥尾）；adx/cvr 实测后弃用 | **深度最高**：KDNN NEON JIT + BA4b 权重预打包（`PackedWeightsCache`，权重一次重排终身复用）、MMoE 聚合 GEMM+softmax 融合（含 prepack 支持）、`PART_920C(0xd03)` 芯片识别修复 |
| **L3 运行时/线程层** | cvr_slave intra/inter 16→4（与并发对齐，其原协议中额外 +19pp） | KDNN 线程上限 2（presort +8pp 的关键项） | 按模型线程配置（cvr/hmv 16/16、adx 32/32、presort 1/1，8/12 验证口径） |
| **L4 基础设施：gflag env 覆盖** | 首个工作项（serving `main()` 不解析 gflags） | 同（独立发现） | 本轮补齐（此前的"够不着"问题，见 REPORT §2.4.4-1a） |
| **L5 验证/标定** | 机器 GEMM 能力标定（OpenBLAS 独立基准 → 论证 adx/presort 已达 78% 上限，避免盲目投入） | 单变量归因扫描矩阵（scan4）+ 执行图导出 + perf 调用链直指 JIT 内核 | 8/12 E2E 验证协议（本报告两轮复测的口径基础） |
| **负结果（如实记录）** | 6 项：adx KDNN −15%、parallel-in-pool 修复无效、OpenBLAS 更慢、线程上扫无益等 | 4 项：adx −11~15%、kdn4 异常、NaN 语义边界披露 | —（8/12 报告记录了 adx 高并发交叉回退） |

### 1.2 定性画像：三种优化哲学

| | skill agent | noskill agent | 人工版 |
|---|---|---|---|
| 改动量 | 7 文件，−20/+221 行 | 2 文件，−3/+431 行 | 数千行（内核级，8 月冲刺 + 未提交线） |
| 风格 | **外科手术式**：知道杠杆在哪，翻开关、修堵点 | **自建式**：没发现树内现成设施，自己写了一个（质量高但多花时间） | **深度改造式**：打进 KDNN 内核（JIT 生成、布局、缓存），改变数据布局本身 |
| 强项 | 图层最彻底（融合命中最多）+ 逐模型路由决策 | 线程治理（KDNN 线程上限）+ 归因纪律 | 内核稳态效率（prepack + blocked JIT） |
| 弱项 | presort 上把 adx 的 KDNN 负结果过度泛化，未逐模型验证 | 图层发现晚（ANNC 补测多花 40 分钟）；TF 线程数没扫 | **图层带伤出厂**：重写器上游缺陷未修、平台门控没扩，"全开"有隐藏缺口 |

**核心分工**：agent 的优化集中在 **L1 图层与 L3 配置层**（删算子、剪关键路径、调线程）；人工版的独占优势在 **L2 内核层**（改数据布局、消除重复工作、提高每核效率）。这个分工恰好落在两个不同的性能域上——这正是并发交叉现象的根源（§3）。

---

## 2. 关键新证据：人工"全开"的隐藏缺口（2026-09-07 取证）

对 round-2（`bench_812_round2`）server 日志逐模型统计融合命中，并与 skill 侧同协议日志对照：

| 融合项 | 人工全开（最新树） | skill 终态（旧树+修复） |
|---|---|---|
| 稀疏链融合 | ✅ hmv `KPFusedSparseDynamicStitchMean`、cvr `...FastMean` 各 1 | ✅ 同类 |
| BN 折叠命中数 | hmv 2 / adx 4 / presort 2 / cvr 0 | hmv 4 / adx 6 / presort 6 / cvr 0 |
| `_FusedMatMul` 节点数 | **0 / 0 / 0 / 0（4 模型全零）** | hmv 6 / adx 7 / presort 8 / cvr 2 |

`_FusedMatMul` 全零的原因（代码级确认，`ws-manual-latest/tf/.../graph_opt.cc:1222`）：

```cpp
bool enabled_fused_matmul_rewriters() {
  ...
  if (implementer == 0x48 && part == 0xd06) {   // 仅 950；本机 0xd03 被挡
     flag = true;
  }
```

skill agent 在消融实验中曾把这个门控扩成 `{0xd01,0xd02,0xd03,0xd06}`（其优化 D），**但该修改只存在于实验树，从未合入本仓主线**。同理，skill 修复的 3 个 BN 折叠缺陷也没有回流。即：

> **§2.8 的"人工全开"实际 = 稀疏融合（半成品 BN 折叠）+ prepack + NEON，缺了 `_FusedMatMul` 整条腿。**
> 这不是并发域问题，而是"全开"的成色问题——它直接构成低并发差距的可识别部分（§3.2）。

（含义：如果给最新树合入 skill 的两处图层修复，人工版低并发表现会进一步逼近甚至反超 agent 终态——待办已于 2026-09-07 执行：门控打开重建 + 第三轮 8/12 口径复测，见 §2.4。）

### 2.4 后续实验：门控打开后的结果（2026-09-07/08，REPORT §2.9）

把 `enabled_fused_matmul_rewriters()` 扩到 0xd03 并重建后，`_FusedMatMul` 命中 2/6/7/8（与 skill 完全对齐）、正确性 4/4 通过。但**单变量 A/B 显示该融合在人工树上的净效应分模型**：presort +2.1~2.2%、cvr +0.5~0.8%、**hmv −8.1~8.8%（干净回退）**。2×2 消融（NEON × FM）定位：图融合本身有益（Eigen 下 +1.7%），回退全部来自 `kdnnFusedGemm` 消费路径（bias 进 epilogue + BA4b 的 fused 内核在 hmv 小 shape 上慢于 `kdnnGemm`+独立 AddV2）——skill 树上同融合走 SVE fused 路径无此问题。

**对 §3.2 论述的修正与强化**：缺失的 FM 腿确实是 cvr/presort 低并发差距的可识别部分（第三轮 cvr 反超 skill 的交叉点从 c8-16 提前到 c4，presort 对 skill 领先扩大到 +25~32%）；但 hmv 的低并发差距另有成因——**skill 树的 fused-GEMM 消费路径（SVE）健康，而人工树的（NEON+BA4b epilogue）带缺陷**，补上融合反而放大差距。这给 §3 的并发域分析加了一条新机制：**同一图层开关在不同 GEMM 后端下的净效应可以反号（hmv：Eigen +1.7% / KDNN −6.0%），图层与内核路由必须联合调优**。

---

## 3. 并发域机制分析：为什么低并发 agent 好、高并发人工好

### 3.1 两个性能域

同一 server、同一模型，并发数跨过某个阈值（≈ 线程池饱和点，本组实验中 cvr/hmv 16 线程配置约在 c8–c16）前后，性能的**绑定约束完全不同**：

| | 低并发（c=1~4，延迟域） | 高并发（c≥8~16，吞吐域） |
|---|---|---|
| 吞吐决定式 | ≈ 并发数 ÷ 单请求端到端延迟 | ≈ 有效核数 × 每核效率 ÷ 每次推理工作量 |
| 绑定资源 | **关键路径长度**：算子数、调度、逐 op 派发、池 fan-out 同步 | **不可重叠资源**：FLOPs、DRAM 带宽、每核内核效率 |
| 开销的可见性 | 一切开销都串行暴露在关键路径上（核心多数空闲） | 可重叠开销（调度、同步、派发）被其他请求的计算覆盖；只剩资源性成本 |

**Agent 的优化打的是左列，人工的 prepack 打的是右列**——所以双方各自在自己的域里赢，并在中间某处交叉。

### 3.2 低并发为什么 agent 赢（cvr −17.3~−6.6%、hmv −12.4~−5.7% @c1–c4）

1. **图融合剪短关键路径，且人工版恰好缺了最有效的一段**。`_FusedMatMul` 每层消灭 4–6 个逐元素算子 + 1 个中间张量（skill 命中 6–8 节点/模型，人工 0）；BN 折叠人工只命中 skill 的一半（2/4/2/0 vs 4/6/6/0）。低并发下吞吐 ∝ 1/延迟，这些被删的算子**全部**折算成吞吐；参照 noskill 单变量扫描，此类融合在 c4 的单项贡献约 +4~7%，两项叠加即可解释低并发差距的大半。
2. **调度开销在低并发不重叠**。基线 profile 中 hmv 调度/executor 开销 ~9–12%、cvr ~18%（RunQueue::PopBack、MaybeGetTask、PropagateOutputs）。高并发时这些周期被其他请求的浮点计算覆盖（机会成本≈0），低并发时它们**逐个串行**出现在每个请求的关键路径上——图融合删算子同时也删调度，所以同一项优化在 c1 的杠杆远大于 c32。
3. **KDNN 路由的逐 op 固定开销在低并发无法摊销**。人工全开把所有 MatMul（含 cvr 的瘦小 shape）路由进 KDNN：adapter 派发、JIT 间接调用、线程池 parallel_for 切分，每个 GEMM 都付一次。noskill 实测了它的延迟形态：16 路扇出下 p99 肥尾（presort kdn16 p99 2339µs vs kdn2 1560µs，convoy 效应）。这些固定成本在 c1 全部落在关键路径；在 c32 被满负荷的池流水线覆盖。
4. **skill 的低并发专属路线**：hmv 的 KDNN SVE 选择本身就是"消除 Eigen 跨线程协调"的延迟优化；其原协议中 cvr 4/4 线程对齐（+19pp）也是纯延迟域手段（8/12 口径统一 16/16 未计入，故 round-2 的 skill 低并发数字还是保守的）。

### 3.3 高并发为什么人工赢（cvr c≥16 +4.6~+9.7%、hmv c≥8 +1.3~+3.3%；增益本身随并发扩大）

1. **prepack 消除的正是"每次调用的冗余工作"，其总量随并发线性放大**。Eigen 每次收缩都对常量权重重新打包（pack_lhs 13–15% + pack_rhs 2–3.5% cycles，双侧 profile 一致），且打包 = 读原始权重 + 写打包副本，权重 DRAM 流量翻倍。并发 N 就是 N 份重复劳动。饱和后带宽是绑定资源 → 删掉冗余直接换吞吐。实测收益随并发单调扩大：cvr 全开增益 **+4.0%@c1 → +19.2%@c32**，hmv **+17.4% → +28.3%**——这是"吞吐域收益递增"的典型形状。
2. **KDNN NEON JIT（BA4b 4-lane blocked）稳态每核效率高于 Eigen gebp（128-bit）**。noskill 标定 KDnn JIT 单核 ~35.6 GFLOP/s vs Eigen 有效 ~24.9。低并发时该优势被逐 op 开销吃掉（上节），高并发时开销被覆盖、纯效率差裸露出来。
3. **图融合的收益是常数项，不随负载放大**。算子删掉之后收益封顶——它删除的 elementwise 算子操作的是 [59,400] 级小张量（多数 L2 常驻），带宽收益有限；而权重矩阵（如 adx 首层 1668×400×4B≈2.7MB）每层都过 DRAM。饱和域里，**删小张量流量 ≠ 删大权重流量**，前者的收益随并发不变，后者（prepack）随并发线性增长——两条增益曲线必然交叉。
4. **JD 线上口径佐证**：8/12 验证报告的 adx prepack 收益就是"并发 4 +12.4%、并发 32 −4.3%"的交叉形——与本次人工版在 cvr/hmv 上的"增益随并发扩大"互为镜像（adx 例外见 §3.4）。

### 3.4 两个特例

- **presort（1/1 线程）：延迟域 = 吞吐域，人工全档第一（+24.6~+31.8%，超 skill +18~26%）**。单线程下没有调度开销可删（skill 的图层杠杆失效）、没有池扇出可调（noskill 的线程上限失效），82% 周期就是 GEMM 本身——prepack（删冗余打包）+ blocked JIT（提单核效率）恰好全打在绑定资源上。skill 在该模型未路由 KDNN（图融合路线收益有限），全档落后。
- **adx：不是并发域问题，是路由错误**。全开的"无脑 NEON=1"把 K=1668 的大 GEMM 送进 KDNN（对大 RHS 无打包缓存、每 op 仅 4–15 chunk），Eigen 快 1.8×——skill 正确地给 adx 关了 KDNN。即便在低并发，全开也输（−31.7%@c1），说明这项与并发域正交。另外 adx 全开 vs 一轮纯 prepack 仅 +0.6%，证明 ANNC 叠加救不回错误的路由。

### 3.5 交叉点总表（round-2，infer/s，人工全开 vs skill 终态）

| 模型 | c1 | c2 | c4 | c8 | c16 | c32 | 交叉点 |
|---|---:|---:|---:|---:|---:|---:|---|
| cvr_slave | −17.3% | −15.2% | −6.6% | −0.7% | **+4.6%** | **+9.7%** | c8–c16 之间 |
| hmv | −12.4% | −9.6% | −5.7% | **+1.3%** | **+3.3%** | **+1.4%** | c4–c8 之间 |
| adx | −31.7% | −31.8% | −26.1% | −14.3% | −9.6% | −9.5% | 无（路由错误，全档输） |
| presort | **+18.4%** | **+24.7%** | **+23.3%** | **+23.7%** | **+23.7%** | **+26.2%** | 无（单线程域，全档赢） |

（交叉点位置 ≈ 线程池饱和点：cvr/hmv 16 线程 → c8–16；presort 1 线程 → 永不进入"多请求重叠"域。）

---

## 4. 结论与最优组合

1. **三方的差异本质是优化落点不同**：agent 集中在图层/配置层（删关键路径、治理线程），人工版集中在内核层（改数据布局、消除每调用冗余、提每核效率）。两层优化**正交且可叠加**（§2.8 已实测：ANNC + prepack 叠加 vs 纯 prepack，cvr +11.6%、hmv +16.4%）。
2. **并发交叉不是谁强谁弱，而是两个性能域的绑定约束不同**：低并发绑延迟（图融合/调度/逐 op 开销的天下），高并发绑吞吐资源（prepack/每核效率的天下）。任何"X 比 Y 快"的结论必须带并发档位，否则无意义。
3. **人工版的真实短板是图层成色而非内核能力**：`_FusedMatMul` 平台门控未扩 + BN 折叠三缺陷未修，使"全开"缺了一条腿（§2）。skill 实验树上的两处修复合入主线即可补齐——这可能是当前**性价比最高的一笔待办**。
4. **理论最优配置**（逐模型）：presort = 全开；cvr/hmv 高并发 = 全开（补齐 FusedMatMul 后低并发亦应反超）；cvr 低并发 = 图融合 + Eigen（或 skill 的 4/4 线程对齐）；hmv 低并发 = 图融合 + SVE；adx = 图融合 + Eigen（永远关 KDNN）。没有任何单一配置 4 模型全档最优。

---

## 附：证据索引

- 并发梯度原始数据：`bench_812_round2/{results,summary.json}`；口径与配置：`manual_version_bench/run_812_round2.sh`
- 融合命中取证：`bench_812_round2/logs/<model>_{fullopen,skill}_r*_server.log`（`Add node: [_FusedMatMul]` / `matched <matmul+biasadd+bn>` 计数）
- 平台门控代码：`ws-manual-latest/tf/tensorflow/core/grappler/optimizers/graph_optimizer/graph_opt.cc:1222`（`enabled_fused_matmul_rewriters`，仅 0xd06）
- profile 数据：`results/{skill,noskill}/REPORT.md` §1/§3（pack 占比、调度占比、GEMM 能力标定）
- 线程扫描（convoy 肥尾）：`results/noskill/REPORT.md` §3（kdn16/8/4/2 矩阵）
- 8/12 验证报告（adx 交叉形参照）：`${EXPERIMENT_ROOT}/validation/NEON_PREPACK_E2E_VALIDATION_20260812.md`
