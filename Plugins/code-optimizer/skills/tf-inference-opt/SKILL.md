---
name: tf-inference-opt
description: Optimize TensorFlow CPU inference and serving through profiling, graph rewrites, operator-library integration, thread scheduling, serialization, and build tuning. Use for TF latency/throughput diagnosis, fusion, prepacking, or benchmark validation, especially C++ Session-based serving and recommendation models. Covers framework integration rather than GEMM microkernel implementation.
---

# TF 推理性能优化（五层方法论）

为 TensorFlow **CPU 推理**做全栈性能优化，主要经验来自 **C++ Session/定制 Serving**。先确认 TF 版本、执行模式与构建分支；TF2 eager/tf.function 场景应映射到对应 profiler、函数图和配置入口，不直接套用 Session 挂载点。本 skill 的方法论源自一套生产级 TF 定制的完整实战——自研算子库对接、静态图融合编译器、运行时线程调优、序列化与构建优化、配套基准设施——主要在 ARM（鲲鹏）服务器上验证，但**方法论本身平台中立**：图层/运行时/序列化/基准各层的套路在 x86 上同样成立，涉及 ARM 特有经验（NEON/SVE、芯片检测）处会显式标注。

## 0. 与其他 skill 的分工

本 skill 管 **TF 框架层**：图怎么执行（融合/折叠）、算子怎么路由（库接入/回退）、线程怎么调度（池治理）、请求怎么编解码（序列化）、二进制怎么编译（构建）、以及怎么证明变快了（基准）。

**不管微内核内部**：GEMM 的 tile/pack/汇编级调优、NEON/SVE 内核编写，交给专门的内核 skill（如 `arm-fp-gemm`、`cpu-gemm-optimization`、`arm-perf-optimization`，若环境里有）。本 skill 把内核当作"可调用的黑盒高性能实现"，聚焦如何把它正确地嵌进 TF。

## 1. 五层优化模型

这五层用于组织框架内优化；测量若指向网络、排队、外部依赖或机器资源争用，先处理实际瓶颈。文中的倍率、shape 门槛、缓存容量与芯片白名单均为历史案例参数，不能作为当前任务的默认收益或验收标准。层不是"重要性排序"，而是**改变的抽象层次**——先测量确定瓶颈在哪层，再去对应层动手。

| 层 | 改变什么 | 典型手段 | 收益特征 | 详细参考 |
|---|---|---|---|---|
| **L1 图层** | 执行**什么**算子序列 | 算子融合、常量折叠、变量冻结、图代数化简、死分支剪除 | 消除 kernel 调度与中间张量内存；链式小算子场景 2x~70x | `references/graph_optimization.md` |
| **L2 算子层** | 每个算子**怎么算** | 路由到高性能库、自定义融合内核、权重预打包、JIT | 单热点算子数倍；大 GEMM/softmax/稀疏乘 | `references/kernel_integration.md` |
| **L3 运行时层** | **谁**在算、几条线程 | 线程池治理、池直通、串行化、整体并行度受 CPU 预算约束 | 高并发下成倍；零代码纯配置也可能翻倍 | `references/runtime_threading.md` |
| **L4 序列化层** | 请求**怎么进出** | protobuf 热路径优化（arena/Reserve/SIMD varint） | 特征类请求（大量 repeated/map 字段）显著 | `references/serialization_build.md` |
| **L5 构建层** | 二进制**怎么编译** | -march 目标、-O3、平台 select、离线构建 | 全局百分之几到几十；决定 L2 的 SIMD 能否激活 | `references/serialization_build.md` |
| 贯穿 | 证明每一步**真的变快** | 基线、分段计时、timeline、压测、A/B | 没有它一切优化都是玄学 | `references/benchmarking.md` |

**投入产出规律**（经验之谈，用于排优先级而非替代测量）：

- **L3 配置类**最便宜：只改启动参数/环境变量就能见效，永远先检查线程配置是否已经错了（超订/欠配是常态）。
- **L1 图层**杠杆最大：改一处全模型受益，且不依赖特定硬件；但需要模型里真的存在可匹配的子图模式（embedding 链、attention 结构是富矿）。
- **L2 算子层**收益最实：热点大算子换实现直接数倍；但每条路由都要维护回退，是长期负担。
- **L4/L5** 常被遗忘：序列化在特征类请求里能占两成以上 CPU；构建 flags 决定了前面所有 SIMD 努力是否生效。

## 2. 标准工作流（先测后优）

### Step 0：建立基线，定位瓶颈层

**没有分层数据之前不要猜瓶颈。** 具体动作：

1. 固定并记录二进制/模型身份、CPU 配额与 cpuset、线程配置、batch、请求并发、目标 QPS 和输入分布；明确目标是固定负载下的延迟还是满足 SLO 的容量。详见 benchmarking.md §3–4。
2. **分段计时**：把一次请求拆成 接收 → 排队/等待 batch → 反序列化 → 预处理 → `session->Run` → 后处理 → 序列化返回，各段耗时打点。这一步直接告诉你瓶颈在 RPC 层还是计算层。
3. **timeline / trace**：在独立诊断轮采样（Session 路径可用 RunOptions FULL_TRACE + RunMetadata），看 kernel 级耗时与算子个数；正式性能轮关闭诊断采样，避免 profiler 改变结论。
4. 分别记录**冷启动**（首次请求，含 JIT/预打包/图优化的一次性开销）与**稳态**数据——两者经常一好一坏，混在一起会误判。
5. 记录资源面：CPU 时间/占用核数及按有效预算归一化的利用率、上下文切换次数（`vmstat`/`pidstat`）、RSS。

### Step 1：症状 → 层 的映射

| 症状 | 优先怀疑 | 去哪 |
|---|---|---|
| p50 尚可但 p99 很差；吞吐不随核数扩展；上下文切换极高 | **L3** 线程超订/池互相打架 | runtime_threading.md |
| timeline 里成百上千个小 kernel；相邻算子间反复小张量读写 | **L1** 融合机会 | graph_optimization.md |
| top 耗时集中在 MatMul/Conv/Softmax/稀疏乘等少数大算子 | **L2** 换实现 | kernel_integration.md |
| 分段计时里反序列化/序列化占比 >15%；请求含大量 repeated/map 字段 | **L4** 序列化 | serialization_build.md |
| 各层都不突出但整体平平；换机器/编译器表现异常；SIMD 没生效 | **L5** 构建 | serialization_build.md |
| 冷启动慢、稳态快 | 权重预打包/JIT 编译/图优化的一次性开销 | kernel_integration.md §3、graph_optimization.md |
| 只有特定 shape 慢（如小 batch、极端长宽） | 路由门槛/内核变体选择 | kernel_integration.md §1、§3 |

### Step 2：实施

去对应 reference 读模式清单，按模式落地。先用单变量实验筛选候选项；对有关联的候选项做小规模交互消融（如融合×后端、prepack×库线程数），最后按模型/batch/并发确定组合。单变量用于归因，不能替代组合验证。

### Step 3：确认实际命中

保留配置解析值 → pass 注册/平台门控 → 匹配与重写节点数 → 实际 kernel/后端 → prepack 命中与回退原因的证据。仅检查开关为 true 或产物含符号，不能证明请求走到了优化路径；节点数为零先排查启用链路。诊断日志限流，完成取证后关闭高成本日志。

### Step 4：验证收益与正确性

先做参考路径与优化路径的数值对拍（含边界及回退用例），通过后同条件交替 A/B、多轮配对，报告 p50/p99/吞吐/RSS、错误/超时/丢弃及离散度；冷启动单独报告。区分固定配置下的改动收益与双方各自调优后的最佳性能，避免混淆归因。详见 benchmarking.md §4。

## 3. 跨层核心原则

这十条来自生产实践中的真实教训（多数反面教材），每条附理由：

1. **先测量后优化。** 上面 Step 0 的全部意义。直觉在多线程+多层的系统里经常错。
2. **回退契约。** 为路由保留原生路径，能力不支持时在写输出前回退；库执行失败后的重试须确认无不可恢复副作用。图重写提供禁用开关，说明需重新建图/加载模型或重启才能生效，避免把进程启动开关描述成在线秒切。
3. **能力门控与命中证据。** 根据集成方式采用构建裁剪、运行期开关和算子守卫（dtype/shape/rank/transpose 等）；平台门控按实际能力制定。记录实际路由及拒绝原因，不能把“已编入/已开启”当成“已执行”。
4. **缓存身份与生命周期。** key 覆盖源权重身份/版本、dtype、shape、切分参数及打包格式；指针键须持有源 Tensor 所有权或具备同等生命周期保证。落盘缓存绑定 checkpoint 内容身份，shape 相同不能证明权重相同。
5. **权重不可变前提显式化。** 明确缓存有效期内权重不可变；在线更新须通过版本化、失效机制及在飞请求的所有权隔离保证一致性，否则禁用该优化。不可变不等于地址稳定，模型重载还需验证旧缓存释放与新旧版本隔离。
6. **整体并行预算。** 分别调优请求并发、RPC worker、inter-op、intra-op 和库线程上限，不要求它们等于配额。记录实际生效值、共享池关系、CPU quota/cpuset/亲和性与节流指标，再按负载扫描配置。
7. **剖析不干扰服务。** profiling 采样要限流（全局最大导出次数、每 N 请求采一次、跳过 warmup）；重活（JSON/trace 转换）离线做，不在服务进程里做。
8. **小算子设阈值。** 把算子路由到重量级实现前先判规模（元素数、维数下限）。小输入走原生实现反而更快（库的准备工作摊不平）；这也是防"优化后小 shape 变慢"投诉的第一道闸。
9. **变更同步测试。** 算子改名、拆变体、改签名后，测试与 op 注册必须同步更新。反例：算子拆成四个变体后，测试还在构建旧算子名——测试套件形同虚设。
10. **死代码及时清理。** 不再用的优化路径、只有定义没有使用点的开关、调试日志，确认后删掉。它们不是"备用"，是下一次重构时的地雷。

## 4. 交付形态：逐模型 × 并发域配置矩阵

没有单一配置在所有模型 × 所有并发档最优（实测：低并发图融合赢、高并发 prepack 赢、单线程模型路由决策一题定输赢）。最终交付不是"一套最优配置"，而是：

- **配置矩阵**：行 = 模型（必要时 × batch 档），列 = 低/高并发域（以实测交叉点分界），格 = 该组合下的图层/内核路由/线程三维配置 + 证据链接；
- **路由决策表**：每个模型用哪个 GEMM 后端（含"不用库"这一合法选项），附单变量实测依据——路由错误（如大 K GEMM 进库）可以吞掉全部其他优化收益；
- 每格标注命中证据与正确性验证状态；跨域交叉点显式标注。

## 5. 快速模式速查

最常用的招，详情都在 references：

- **L1**：子图模式匹配 → 单融合算子（一遍扫描、免中间张量、就地写收尾）；BN 折叠进权重；只读变量冻结成常量；静态 Gather 剪掉未用分支；N 个同输入分支聚合成一次大 GEMM。
- **L2**：算子入口三层门控 + adapter 调库；权重 blocked 布局预打包 + 原子发布的只读缓存；稀疏乘在线转 CSR + 密度自适应；全满稀疏回退稠密 GEMM。
- **L3**：库的并行任务直通框架 intra-op 池；按执行器契约评估算子内联；联合调优线程与请求并发。
- **L4**：保持对齐契约的 arena 分配优化；repeated/map 前瞻 Reserve；SIMD varint；TLS block 缓存。
- **L5**：-march 对准目标微架构；-O3 显式；非目标平台 select 掉。
- **基准**：明确负载模型与计划/实际发送速率；冷/热分开；分位数口径与丢弃率齐报。

## 6. 反模式速查（生产事故提炼）

- 融合重写器不检查 transpose/rank 属性 → 重写成功但内核运行时才报错（应在匹配期拒绝）。
- DCLP 双检锁外层用 relaxed 读 → 与锁内写不建立 happens-before，并发首调数据竞争。
- 两套分支线并行维护，修复只进了一条线 → 同一 bug 在另一条线上复活。
- 深析逻辑放服务进程内 → profiling 本身成为延迟来源。
- 缓存无上限 → 内存随模型规模膨胀（设置并验证模型/进程总预算；案例采用超限不缓存，其他逐出策略须保护在飞引用）。
- 编译参数没开到目标 ISA → 库里的 SVE2/AVX512 代码段根本没编进产物。

更多见各 reference 末尾的「反模式」节。

## 7. references 索引

| 想做什么 / 什么症状 | 读 |
|---|---|
| 设计融合算子、写图重写 pass、常量折叠、变量冻结、图代数化简 | `references/graph_optimization.md` |
| 接入算子库（路由/回退/adapter）、权重预打包、JIT、稀疏乘、给依赖库打补丁 | `references/kernel_integration.md` |
| 线程池治理、池直通、并行预算、串行执行、调度开销 | `references/runtime_threading.md` |
| protobuf 热路径、依赖版本管理、编译 flags、容器化构建 | `references/serialization_build.md` |
| 建基线、分段计时、timeline、压测工具设计、A/B 方法论、指标口径 | `references/benchmarking.md` |

需要追溯门控失效、跨后端收益反号与并发交叉时，按需读 [消融报告 §2.7–2.9](ablation_report.md) 与 [三方对比分析](agent_vs_manual_analysis.md)。其中倍率仅代表报告内对应机器、版本和负载。

每个 reference 的组织：**何时读 → 模式清单（问题/做法/为什么/适用条件/反模式）→ 实战案例摘要**。读的时候按需跳节，不必通读。
