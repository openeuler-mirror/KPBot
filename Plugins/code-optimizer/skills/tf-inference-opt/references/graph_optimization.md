# 图层优化（L1）：融合、折叠、冻结、图代数

> **何时读**：timeline 显示大量小 kernel 与中间张量搬运；想设计融合算子或写图重写 pass；想消掉推理图中本可静态化的计算（BN、只读变量、未用分支）；多分支同源结构（MoE gate 类）想聚合。

图层优化的本质：**利用可证明静态的图结构与值**；动态 shape、资源变量及控制流不能默认静态化。满足前提时，很多运行期做的事可以提前到加载期/编译期，很多算子链可以合并成一个。收益来自消除 kernel 调度开销、中间张量的分配/读写、以及纯冗余的计算。

TF 中图层优化的三个挂载时机（按介入点）：

| 时机 | 机制 | 适合做什么 |
|---|---|---|
| **SavedModel 加载期**（建 session 前/后） | 直接改写 MetaGraphDef | 变量冻结、需要读 checkpoint/已恢复变量值的折叠 |
| **grappler 管线**（首次 Run 触发） | 注册 GraphOptimizer / 加入 MetaOptimizer 顺序 | 模式重写（融合）、常量折叠、代数化简 |
| **离线工具**（部署前 CLI 转换模型） | 独立 pass 逐个跑 | 同上，产物是转换后的模型文件；可与运行期 pass 共享代码 |

设计任何图 pass 前先决定挂载点：需要真实变量值 → 加载期；只看图结构 → grappler；要离线分发/审计 → 离线工具。同一个 pass 尽量写成三种时机都能调的纯函数（输入输出都是 GraphDef），这是保持可维护性的关键。

---

本文模式是起点：可新增更大范围、多输出、跨分支和带动态守卫的融合。对候选比较消除的内存/调度成本、图并行度损失与后端效率；允许直接原型验证新算法，不要求先复现本文算子清单。

## 1. 模式重写器框架（自研融合 pass 的骨架）

已有通用重写框架时优先复用；单个探索原型可用独立 pass，确认收益后再决定是否抽象。**通用框架 + 每个融合一个 Rewriter 类**的收益是：注册/开关/遍历/重接线只写一次，新增融合只写匹配逻辑。

框架要素（以 grappler 风格为例）：

```
GraphOptimizer:
  rewriters_: List<Rewriter>          // 每个融合一个，按注册序尝试
  optimize(graph):
    for node in graph:                // 单遍扫描
      for r in rewriters_:
        if r.match_and_rewrite(node, graph, props):
          重建节点索引; 跳到融合节点位置继续
          break
```

- **anchor 锚点**：每个 Rewriter 从一个"根节点"开始回溯（如融合的终点算子），而不是全图盲搜。锚点选子图中**最不易误匹配**的节点（通常是结构最特殊的那个）。
- **链式短路校验**：匹配过程是一串条件检查（op 类型、输入个数、attr 值、常量值、shape），**任一失败立即 return false 保持原图**。用一个宏（如 `CHECK_NODE_OK(cond) || return false` 风格）让校验代码线性可读。
- **重接线**：新建融合节点（命名加统一后缀如 `/fused`，便于调试辨认）→ 把原子图各输出的消费者改接融合节点对应输出端口（框架里的 replace-all-users）→ 确认数据/控制边、外部 fanout、feed/fetch、device 与有状态语义均保留，再移除已无用节点或交给 DCE。仍有消费者的节点不能随意置 NoOp；重写失败应保持原图。
- **常量读取要防御**：匹配期读 Const 节点值时，值可能不存在/类型不符，全部走失败路径而非崩溃。
- **形状信息**：尽量接框架的静态形状推理（grappler 的 GraphProperties，开"含输入输出张量值"选项）——前序 pass 把 begin/stride/axis 折成 Const 后，你的匹配要兼容"常量已在"和"还在原节点"两种布局。**容忍中间节点（Reshape/Cast/Identity）被前序 pass 消化掉**是 pass 间兼容性的关键。

**开关设计**：总开关（如 `--enable_my_opt`）× 每融合分开关，分开关默认开、总开关默认关（灰度上线）。仅对确有硬件依赖的融合做能力门控；记录注册、拒绝原因、匹配数及产物节点数。开关为 true 不代表重写已命中，更不代表运行时选中了预期后端。

## 2. 融合算子设计六步套路

把一条子图融合成一个自定义算子，按这个顺序设计：

**Step 1 识别子图**：从真实模型导出图里找重复出现的链条（模型作者用什么模式写，图里就有什么）。推荐系推理的富矿：
- embedding 侧：`切片取列 → 去重(Unique) → Gather 查表 → 段归约(SparseSegmentSum/Mean) → 补零(Padding) → reshape` 长链；
- 多表分片：`id 取模/除法 → DynamicPartition 分桶 → 逐表 Gather → Stitch 缝回`；
- mask 链：`嵌套 Select(Equal/Greater(...))` 生成 0/1 权重；
- attention 交互：`BatchMatMul + bias + 激活 → 与 tile 的 ±× 拼接成 4 路特征`；
- 同源多分支：N 个 `(MatMul → Softmax)` gate（MoE 结构）。

**Step 2 设计签名**：
- 输入 = 子图的全部外部输入（权重、索引、阈值常量）；输出 = 子图全部被下游消费的输出——**多输出算子是常态**，别为单输出妥协。
- 融合点选在**信息最浓缩**的边界：往上多包一个节点可能把整个 Unique 的去重簿记都省掉（见 §3.1），少包一个则留下一截中间张量。
- 常量参数（axis、begin/end、combiner）做成 attr（编译期定死、匹配期校验），动态张量做输入。
- **匹配期校验的属性必须在匹配期查**（transpose_a/b、rank、dtype）——不能等内核运行时才报错（反模式，见 §6）。

**Step 3 内核：一遍扫描**：理想内核是单遍循环完成全部逻辑（去重+查表+归约一气呵成），不做多趟物化。用哈希表做首次出现分配槽位（`unordered_map<value, slot>`），槽位号直接写输出索引——去重与重排一次完成。

**Step 4 免中间张量**：原子图的每个中间张量都是一次分配+写+读。融合内核里用：行级 `memcpy` 抽取（查表）、就地写收尾（激活/softmax 直接写在输出张量上）、**能只算 shape 就不搬数据**——"Fast 变体"思想：若下游只消费形状（Shape/StridedSlice 取标量），输出 shape 标量、不复制数据，下游 reshape 延后。

**Step 5 并行策略**：按输出行/批次切分（`ParallelFor`，cost_per_unit 按每单元实际工作量估）；两阶段写区域不相交时免同步直接双 ParallelFor；数据量小（如纯补零）就单线程——并行本身有开销，别为并行而并行。

**Step 6 向量化数值段**：累加/缩放/归一化用目标平台向量宽度（ARM NEON `vld1q/vaddq/vmulq` 4×f32；SVE 用 `whilelt` 谓词处理尾段，宽度自适应），尾部标量收尾。激活函数等逐元素段同理。

**变体拆分**：同一融合按运行期条件拆多个注册变体——按归约语义（Sum/Mean）、按解码方式（N 为 2 的幂时用位运算 `id&(N-1)`/`id>>log2N` 代替取模除法，即 Fast 变体）。匹配期选好变体，内核里不做运行期分支。

## 3. 高价值融合模式详解

### 3.1 消除"去重簿记"：融合点上移

`SparseSegmentSum(gathered_unique, idx, seg_ids)` 数学上等价于"按 segment 直接累加 `emb(raw_id[i])`"。把融合边界从 Stitch 之后**上移到段归约处**，融合算子直接吃原始含重复 id 的输入——Unique、去重索引、重排全部消失。**启示**：融合边界每上移一层，就消掉一层簿记数据结构；先问"这段链的数学本质是什么"，再定边界。

### 3.2 多分支同源聚合（MoE gate 类）

N 个 `(MatMul(x, W_i) → Softmax)` 分支，同输入 x、同 device、float → 聚合为单算子：
1. 首次执行把 N 个权重 `[k,n_i]` 拼成 `[k, total_n]`（**双重检查锁缓存**，后续复用）；
2. 一次大 GEMM `x[m,k] × W[k,total_n]`；
3. 按 n_offset 切片 + 就地 softmax。

FLOPs 不变，收益全在：kernel 调用 2N→1、线程池 barrier N 次→1、x 只读一遍（原来被读 N 遍）、大 GEMM 的分块填充率远高于 N 个窄 GEMM。**匹配器**：收集所有 MatMul 输入规范化后指向同一 shared_input 的 Softmax←MatMul 分支，`>1` 支才融合。

### 3.3 桥接到框架原生融合算子

自定义重写器不必产出自定义算子——产出**框架标准融合算子**（如 TF 的 `_FusedMatMul` + fused_ops=[BiasAdd, Relu]）可以直接复用官方 kernel 注册，进而吃到你在 L2 层给该算子做的库路由/JIT/prepack。图 pass 与算子层优化就这样打通。注意识别"激活函数的图上等价展开"（例如 `0.5*(a+abs(a))`；必须匹配完整表达式，`a/2` 单独不是 ReLU。即使实数代数等价，浮点溢出、NaN/Inf 与有符号零语义仍需校验）。

### 3.4 高开销控制流展开

TF1 风格控制流在推理图里留下 `Switch/Merge` 链与条件分支：变长序列的"够长则透传/不够则补零"用 4 个 Switch+Merge 表达。融合算子内核里一个 `if` 就能替代整段控制流——**控制流是比逐元素算子更肥的融合目标**。同理，相同谓词的 Switch 链可合并为谓词 and 的单 Switch（注意仅在谓词组合可复用时净收益为正）。

## 4. 常量折叠与变量冻结

推理图中"变量不是常量"是原生折叠的最大障碍——原生 pass 只折"计算仅依赖 Const"的子图，训练导出图里的 BN 参数（mean/variance/scale/offset 是变量读）永远折不掉。两个互补手段：

### 4.1 带变量源的常量折叠（BN folding）

把**变量读当常量源**：匹配推理态 BN 展开模式，用 `session->Run({}, var_names)`（或读已恢复的 checkpoint）取真实值，然后数学折进权重：

```
scale_factor = gamma / sqrt(variance + eps)
W' = W · scale_factor          （替换 MatMul 权重输入）
b' = (bias − mean) · scale_factor + offset   （替换 BiasAdd 的 bias）
```

BN 从图中完全消失。工程要点：
- **值缓存**：落盘缓存绑定模型/checkpoint 内容身份、变量名、dtype/shape 与折叠参数/格式版本；加载前校验 manifest，不匹配就重新读取并计算。仅 shape 签名会在同 shape 不同权重时误命中，这不是偶发哈希碰撞；无法证明内容身份时禁用跨加载复用。并发写缓存采用完整写入后原子发布，避免读取半成品。
- **挂载期**：在原生 ConstantFolding **之前**跑（趁 BN 模式节点结构还没被其他 pass 改形）；且必须在 **session 创建之前**对 GraphDef 生效——折叠挂在 session 创建之后属于死代码（对已编译的执行图无效），且会被 grappler 自身常量折叠提前破坏模式。折叠后 dump 产物模型供人工核对（加 dump 开关）。
- **冻结图的三个通用坑**（权重已是 Const 的 SavedModel）：
  1. 变量读形如 `Identity → Const` 链——"is_variable" 判定要沿 Identity 链解析，不能只认直接的 Const 输入；
  2. 多个 Const 算子可能竞争同一个模式角色（如 epsilon 标量 vs variance 向量）——角色分配要按 shape/dtype 区分，并保证**每个角色至多分配一次**，否则匹配失败或错配；
  3. 无 session 时取值需要沿 Identity 链解析到 Const 再读静态值；解析不到（真变量图）就整体跳过，不做部分折叠。
- **形状守卫**：左输入已知维与权重维不匹配才跳过；动态维（-1）照常折，不要一见 -1 就放弃。
- 迭代折叠（一次成功后重扫全图），设迭代上限防病态图。

### 4.2 变量冻结（通用变量 → Const）

加载期（建 session 前）把**只读小权重变量**替换成 Const 节点，比折叠更通用：
- **allowlist**：按名字后缀圈定（weight/bias/kernel/gamma/beta/mean/variance 类），**显式排除大 embedding 表**（名字含 embedding/lookup/table 的）——冻结它们会撑爆图。
- **大小上限**：单张量 MiB 上限（默认几 MiB 量级）。注意"0 = 无限制"还是"0 = 用默认值"要在 proto 注释与实现里对齐（真实踩坑：注释说无限制、实现回退 2MiB）。
- **安全检查**：确认已恢复权重并分析所有可达读写（含函数体、控制依赖、资源别名）；名字白名单不能证明只读。仅在证明初始化完成后无可达写入、无在线更新时冻结，不能把 Assign 默认当死写；不满足前提则保持变量。
- **替换保留语义**：原位替换节点，保留控制边与内部属性（输出 shape 注解等），dtype/shape 兼容性校验后再换。

冻结是下游一堆优化的**前置条件**：权重变 Const 后，因式分解和匹配器可读取静态值；预打包仍须另外保证源 Tensor 生命周期与地址身份。做 L1 优化时先冻结再跑后续 pass。

## 5. 图代数变换（不引入新算子的化简）

不写新内核，只重排图，性价比极高：

| 模式 | 变换 | 条件 | 收益 |
|---|---|---|---|
| **静态 Gather-of-Pack/Concat 剪枝** | `Gather(Pack(x1..xN), 静态indices)` → 只保留被索引输入的小 Pack | indices 是编译期已知 Const、不被 feed、axis 对齐 | 未被选中的分支（如未用 expert）变死代码，被 DCE 整支剪除 |
| **广播拼接因式分解** | `MatMul(Concat(X, Tile(Q,L)), W)` → `MatMul(X,Wx) + Broadcast(MatMul(Q,Wq), L)`（SplitV 拆 W） | 权重已是 Const（依赖冻结）、Tile 倍数 L>1 已知、盈利门槛如 `qw > L/(L-1)` | tiled 部分计算量降 **L 倍** |
| **冗余 slice 消除** | "多切片后拼接"模式中重复的切片 | 模式匹配 | 直接删除 |
| **重复计算 CSE** | 融合/聚类后仍相同的子表达式 | 框架 pass 或自研 | 消重复 |

匹配"被 Tile 重复的输入"时注意透过 `Reshape(Tile(Q,[1,L]),[-1,w])` 双层结构取到原始 Q。

## 6. 反模式（生产事故提炼）

- **匹配期不查 transpose/rank**：融合了带 `transpose_b=true` 的 MatMul，运行期内核才报 InvalidArgument。所有影响内核布局假设的属性（transpose、adj、rank、dtype）必须在匹配期逐一校验。
- **缓存键不完整**：聚合融合的权重拼接缓存只存"已初始化"布尔，不校验各段宽度/权重个数——切分变了静默复用旧拼接，输出错列。键覆盖源权重身份/版本、shape、dtype 与切分参数全集，并保证源张量所有权。
- **DCLP 内存序错误**：双检锁外层 `atomic.load(relaxed)` 后直接读非原子成员，与锁内写不建立 happens-before。外层用 acquire，或干脆 `call_once`。
- **测试不同步**：算子拆变体/改名后测试还在构建旧名——CI 绿但测试没跑。改签名必同步测试与 op 注册。
- **临界区内失败路径**：锁内构造失败 return 后初始化标志仍为 false，并发反复重试；失败要置终态或让重试幂等安全。
- **pass 间布局假设**：假设中间一定有 Cast/Reshape 节点，但前序 pass 可能已把它消化——匹配要兼容两种布局（有/无中间节点）。
- **冻结大小限制与文档矛盾**：注释"0=无限制"实现却是默认值——数值语义写测试钉死。

## 实战案例摘要

此方法论支撑过一套生产定制：9 个 embedding 侧融合算子（去重查表/变长补零/段归约/多表分片/稀疏索引重建/mask 链/双重查表）+ attention 交互融合（5 个按维度展开的 NEON 微内核版本，按 M/K/N 整除性选版，就地 scatter 到拼接布局）+ MoE gate 聚合（N×(MatMul+Softmax)→单大 GEMM，配权重拼接缓存）+ BN 折叠 + 变量冻结 + 三个图代数 pass。融合算子的 python 基准验收阈值从 1.5x 到 71x（shape-only 变体最高）；attention 融合各版本源码注释标注预期 25%~100% 提升。教训同样真实：上述反模式清单里每一条都在 review 中被指出过。
