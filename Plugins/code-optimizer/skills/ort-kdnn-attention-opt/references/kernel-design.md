# Kernel 设计:布局、访存、复用、数值语义

Phase 2/3 使用。这是全部技术决策的核心文档。先读 §1 执行模型——它决定后面所有设计的形状。

## 1. 执行模型:ORT 把 batch 拆成 B 个 B1 task

ORT 的自定义算子拿到 `[B, S, ...]` 输入后,惯例是用 `concurrency::ThreadPool::TryBatchParallelFor`
沿 batch 维拆成 B 个 batch=1 的子问题,由 intra-op 线程池并行执行。这个事实推论出一整套设计约束:

1. **kernel 按 batch=1 设计**。不做整批假设;`Sq=1` 时每个 task 是 `[1, Sq=1, Sk, H, d]` 的问题。
2. **构造开销 × B 倍放大**。B=128 时每个 task 若重建 GEMM 描述符/方案查找,单次推理就是
   6 block × 128 task × 2 GEMM ≈ 1536 次构造。必须 cache(见 §5)或消除。
3. **每个 worker 独占 workspace**:`thread_local` + grow-only(只增不减);一个 worker 同一时刻
   只跑一个 callback,所以 thread_local 天然互斥,免锁。
4. **worker 内禁止嵌套并行**:外层已经是 ORT 线程池,内部再开线程必然争核。GEMV 内部固定单线程;
   KDNN 的 OMP 运行时在嵌套时直接串行(silent 性能回退)。
5. **SVE VL 是每线程属性**(Linux):primitive 若特化了 VL,构造与 Run 必须同线程;cache key 必须含 VL。

## 2. 算法空间(两维正交,按 shape 选)

**融合范围先于算法选择(消融实验修正)**:历史结论"融合边界止于投影后 QKV,不含投影"有隐含
前提——当时投影侧的转置/形状机已由独立的图 pass(TensorDot 融合、no-op Transpose 消除)处理,
重复吞进算子无益。**从零基线上那些 pass 不存在时,这个边界是错的**:把 Q/K/V 投影也吞进算子
(3 个 GEMM 合并为 2 个、K|V 权重拼接预打包、K/V 偏置代数折叠进点积、常量 mask 折叠)可以
多拿约 6% 端到端(no-skill 对照组实测 +25.8% vs 边界保守组 +19.9%,机制是投影侧转置/
形状机/BiasAdd 约占子图时间 70%)。**决策规则:先 profile 基线确认投影侧是否已被其他 pass
处理;未处理则同时评估两种融合范围,用数据决定;已处理则止于投影后 QKV。**

|  | K/V pack(PackHeads 拷贝) | K/V no-pack(stride 视图) |
|---|---|---|
| **softmax 物化**(完整 scores) | classic | classic_no_pack |
| **softmax 在线**(flash 分块) | (无意义) | Sq>1 时才考虑 |

- **Sq=1 时 flash 是负收益**:attention 矩阵只有 `[H,Sk]` 一行,没有可省的中间矩阵,online softmax
  的 rescaling 是纯开销。Sq=1 的正确目标是 **GEMV 化 + 访存连续化**。(历史上 flash/AUTO 路线在
  decode 场景全部退出,原因即此。)
- **classic 的 PackHeads**:把 K/V 从 `[B,Sk,H·d]` 拷贝成 `[B,H,Sk,d]` 以喂 batched GEMM。Sq>1 且
  Sk 大时有意义;**Sq=1 时是纯开销**——QK/PV 退化为 GEMV,GEMV 不需要 head-major 布局。
- workspace 对比(B128,Sk=200,单 worker):classic 26,624,000 B vs no-pack 3,200 B,**差 4 个数量级**。

**融合优先原则(消融实验实证,2 个独立实现 + 历史数据一致)**:端到端收益的绝对大头来自融合
本身——消除 Transpose/Reshape 链与 per-op dispatch(历史 profile 中占 24~26%)。kernel 微结构
(joint vs 逐 head vs 标量)在 Sq=1、工作集 L2-resident 时端到端差异 **<2%**(两个 agent 独立
测得 <0.6% 和 +1.78%;历史上联合 kernel 相对 classic 也只有 +1~2%)。所以正确的实施顺序是:
**先用最简单的 per-head/标量 kernel 把融合闭环做对(含全部验证),joint 联合 kernel 作为最后的
增量优化**——它的复杂度(寄存器分组、谓词尾部)在最常见场景下买不到多少端到端时间。

## 3. 布局与 stride 视图(no-pack 的核心技术)

投影输出是 `[B, S, H·d]` 交错布局(head 在内层连续)。no-pack 不搬数据,用 stride 描述逻辑转置:

- K 的 head h 逻辑转置:`[Sk, d]` 矩阵,行 stride = H·d(跨 token),列连续(d 个 float);
- V 的 head h:`[Sk, d]`,行 stride = H·d,列连续;
- Q(Sq=1)的 head h:`[1, d]` 一行,head 间偏移 h·d,行距 hidden。

**head-in-batch 视图**(关键技巧):把 head 维当 batch 维,4D 视图 `Q[1,H,1,d]`、`Kᵀ[1,H,d,Sk]`、
`V[1,H,Sk,d]`(head stride = d),一次 batched GEMM 完成 4 个 head——从 8 次 per-head dispatch 降到
2 次,且零 pack、零 reorder。实测:model4 QK shape,1 次 batched 3.24μs vs 4 次 per-head 4.66μs(1.44×)。

**已知陷阱**:GEMM 库的 TensorInfo stride 校验通常要求"每维 stride ≥ row-major 最小值",而交错布局
的 head 维 stride=d < d·Sk,会被拒。两条路:(a) 给 TensorInfo 加显式的
`AllowInterleavedStrides` 标志/构造器,**只**在 attention 视图上使用(不要全局放宽校验,那会波及所有
算子的合法性检查);(b) 绕过 GEMM 库,在融合算子内直接写 kernel(见 §4)。历史上先证明了 (a) 可行
(临时放宽校验跑通 maxdiff 2.6e-08、pack_bytes=0、reorder=0),再落地成受控标志。

## 4. SVE/NEON kernel 设计(Sq=1 联合 GEMV)

平台事实:KP950 类 AArch64,SVE VL=256-bit(svcntw=8 个 float),NEON 128-bit 可用,谓词 `svwhilelt`
处理尾部。目标 shape `Sq=1, H=4, d=32, hidden=128`(model4)的经验全部可推广:

**QK 联合 = 交换循环顺序**:
- 朴素 per-head:每 head 扫 K,一次只取 token row 中 32 个元素、跨过其余 96 个 → H 轮大 stride 扫描。
- 联合:**token 放外层**。每次定位一条连续的 512B(=H·d·4)K row,一次顺序扫描中依次完成 H 个
  head 的 Dot,结果写 H 条 head-major score row。计算量不变,**收益全部来自连续访问、硬件预取和
  减少外层遍历次数**——不是减少 FMA。准确说法是"H 头共用一次 token-wise 遍历",不是多套 QK
  累加器常驻寄存器。
- Dot32 用 2 个 128-bit NEON 累加器、每轮 8 个 FP32;任意 H/D 都有通用实现(D 用 svwhilelt 谓词
  处理奇数尾)。

**PV 联合 = 累加器分组常驻寄存器**:
- 输出 `[1, H·d]`;每 head 的 d 维输出拆成 d/8 个 NEON 向量累加器,在整个 Sk 循环内保持寄存器
  常驻,最后一次性写回 → H 轮 V 扫描合并成 ceil(H/组大小) 轮。
- **分组预算**:H=4,d=32 → 每 head 4 个累加器。`4+0`(4 head 同算,32 累加器)没给载入值和临时量
  留寄存器,必然 spill;`3+1`(Sk≤64,24 累加器)或 `2+2`(Sk>64,16 累加器,live set 更小)。
  选组大小时看 Sk 是因为长序列循环体执行更久,寄存器压力的代价更明显。
- **支持边界与回退**:D ≤ 32 个 SVE 向量(超限回退通用逐 head GEMV,数值语义不变);QK 对任意
  H/D 通用。fallback 必须是运行时判定 + 自动回退,不是构造期断言。

## 5. primitive 复用(cache)

**为什么**:B1 task 模型下每次 Run 都构造对象,构造(描述符、方案查找、可能的 JIT)开销被放大
B 倍;多 worker 并发构造还争共享状态。实测 cache 使六 MHA 节点累计 42.92→15.04ms(.profiling),
端到端 +1.7~4.6% 且明显降低稳态波动。

**设计**:
- 两级:全局 L1(容量受限近似 LRU,默认 1024,可 env 关闭;同 key 并发 miss 只构造一次,构造在
  全局锁外)+ 每线程 L0 热 entry(Gemm/MHA 各 8 个;L1 存 canonical primitive,线程首命中生成轻量
  replica,后续临时 handle 只改本线程 replica 的引用计数,**避免多 worker 争同一 cache line**)。
- 侵入式引用计数:淘汰/清空 cache 不使仍存活的句柄失效。
- **cache key 完备性清单(漏一项就是静默算错)**:shape 全维、**scale(位级,如 FloatBits)**、
  算法、gemv 模式、内层线程数、外层 worker 数、**SVE VL(每线程)**、mask 广播模式、kv_merged、
  (Gemm 侧:完整 shape/stride/layout/dtype/attributes/有效线程数)。
- 回归测试模式:同 shape 不同 scale/算法交替构造 + 同 session 双 Run(SetNumRunCalls(2)),断言每次
  结果都对参考——专门防"key 漏字段串台"。

**替代方案:无状态自包含 kernel(消融实验中两个独立实现都选了它)**。如果 kernel 不经过
GEMM 库(不构造描述符、不查找方案、无 JIT),每次 Compute 直接从输入指针算出一切,则
"构造 × B"问题**结构性消失**——没有可缓存的对象,cache key 完备性/碰撞回归/引用计数/驱逐
这一整类缺陷面随之消失,scale 退化为节点属性而非共享运行时状态。代价是放弃 GEMM 库的
通用优化(对 Sq=1 的 GEMV 场景这个代价接近零)。**决策规则**:kernel 是纯 GEMV/小
逐元素计算 → 自包含无状态;需要复用重型 GEMM 描述符/pack 后权重 → 才引入 cache。

## 6. workspace 协议

- 大小依赖算法与线程数(classic vs no-pack 差 4 个数量级;no-pack ≈ min(workers, B·Sq) × H·Sk×4B)。
- **调用方每次构造 primitive 后必须重查 `GetWorkspaceSize()`**,grow-only resize;切算法不重查 =
  越界写。
- 传 nullptr 是合法的(库内部分配),但每次 Run 多一次 aligned_alloc/free——decode 热路径上这是
  一等性能税,集成方应传 workspace。
- 每算法只构造自己实际用的成员(lazy/optional 构造),避免切换算法时重建无关对象。

## 7. 数值语义表(两套实现路径都必须一致)

| 输入情形 | 正确输出 | 说明 |
|---|---|---|
| 常规 scores | softmax(max-subtraction)·V | 必须减 max,否则 exp 溢出 |
| mask 全 -inf 行 | 输出全 **0**(不是 NaN) | softmax 分母无意义时定义为 0 |
| scores 含 +inf 并列 | 概率在 +inf 列**均分** | 1/列数 × V |
| mask 含 NaN | NaN 传播到该 batch 输出 | 不吞 NaN |
| scale 缺省 | 1/sqrt(d),运行时由 d 计算 | 融合侧:图上 scale==1/sqrt(d)(相对误差<1e-6)时省略属性,由 kernel 推导,保证 bit-exact |
| `Div(qk,c)` vs `Mul(qk,1/c)` | ≤1ulp 差异 | 非正确性问题,A/B 对比容差内 |
| K/V batch=1 或 B | 逐 batch 指针 stride(0=共享) | ONNX 多向广播语义 |

## 8. 输入/输出布局约定(算子接口)

- 全部原生 row-major float;`[B,S,hidden]` 与 `[B,S,H,d]` 内存等价,底层只按 `[B,S,H·d]` 处理
  (rank-4 时要求 Sq==1,输出布局才与原图逐字节一致;Sq>1 需要显式输出转置,没实现就拒绝融合)。
- mask = `[B 或 1, 1, 1, Sk]` 加性 pre-softmax 偏置;共享 mask 用 stride=0 表达;causal 之类语义由
  调用方把 -inf 写进 mask,算子不感知 mask 语义。
- 可选输入(nullptr 容忍);输出 shape 在 Compute 里按广播规则推导。
