# 优化策略模式百科全书

> 用途：interactive-optimizer 发现代码特征后，快速查找对应的优化策略和实施方案。本文件是 optimizer 的**决策引擎**——告诉代理"看到什么特征该做什么优化"。
> 配合 `02-instruction-reference.md`（指令延迟数据）和 `04-safety-and-gotchas.md`（安全约束）使用。

---

## 1. 策略优先级

优化策略按以下顺序执行。优先级反映**风险收益比**：低风险高收益的策略优先，高侵入性策略靠后。

> **与 CLAUDE.md 的关系**：本表列出了 13 种策略类型，比 `CLAUDE.md` 文档的 8 种更多。`CLAUDE.md` 反映的是早期 pipeline 版本，当前 `analyze-hotspot`、`decide-optimization` 和 `apply-optimization` 已支持全部 13 种。本文件以当前 pipeline 实际实现为准。

| Priority | Strategy (type) | Risk Level | Typical Speedup | 代码变更范围 |
|----------|----------------|-----------|----------------|------------|
| 1 | `compiler-flag-tuning` | 低（仅编译参数） | 5-15% | 构建配置文件 |
| 2 | `code_hoisting` | 零（循环不变量外提） | 5-10% | 单函数 |
| 3 | `bulk-memory-opt` | 零（libc 调用替换） | 1.5-3x | 单循环 |
| 4 | `asm-optimization` | 低（机械等价变换） | 5-20% | 单函数（仅 .s/.S） |
| 5 | `vectorization` | 中（语义等价需验证） | 2-4x | 单循环 |
| 6 | `vectorization_deepen` | 中（已有 SIMD 改进） | 1.2-2x | 单循环 |
| 7 | `memory-access-optimization` | 中-高（可能改接口） | 1.5-3x | 数据结构+函数 |
| 8 | `scalar-vector-hybrid` | 中（管线迁移） | 1.1-1.5x | 单函数 |
| 9 | `branch-elimination` | 低（条件选择替换） | 1.1-1.3x | 单循环 |
| 10 | `prefetch-optimization` | 低（仅插入预取） | 1.1-1.5x | 单循环 |
| 11 | `throughput-enhancement` | 中（循环展开+尾处理） | 1.2-2x | 单循环 |
| 12 | `variant-selection` | 低（仅实测对比） | 变动大 | 调用点分派逻辑 |
| 13 | `algorithm-substitution` | 高（语义变更） | 变动大 | 算法实现 |

> **意图偏置**：用户 `intent.optimization_goal` 会调整优先级。`throughput` 目标提升 asm-optimization/vectorization/throughput-enhancement/scalar-vector-hybrid 优先级；`latency` 目标提升 branch-elimination/prefetch-optimization；`memory` 目标提升 prefetch-optimization/memory-access-optimization。

---

## 2. 代码特征 → 策略映射

### 2.1 主映射表

| 代码特征 | 策略 (type) | 触发条件 | 默认 Priority |
|---------|------------|---------|--------------|
| 标量循环 + 连续访存 + 无跨迭代依赖 | `vectorization` | 无 NEON/SVE/SME intrinsics；无 I/O/锁/原子副作用 | 1 (CPU≥20%且IPC<0.7) / 2 (CPU≥10%或IPC<0.9) / 3 (其他) |
| 已有 SIMD 但 lane 未用满（64-bit为主） | `vectorization_deepen` (lane_width_partial) | 存在 vadd_f32 但无 vaddq_f32 | 1 (64-bit为主) / 2 (混合) |
| 向量主循环后跟标量尾循环 | `vectorization_deepen` (remainder_scalar) | 主循环 NEON intrinsics + 后续标量 for | 1 (remainder>25%) / 2 (其他) |
| 连续 2+ 个独立 `vld1q` 但无 `_x2/_x3/_x4` | `vectorization_deepen` (load_pair_missing) | 相邻 vld1q 加载连续地址 | 1 (3+个独立load) / 2 (2个) |
| 循环体 ≤4 个 NEON 寄存器（共32可用） | `vectorization_deepen` (register_underutilized) | 统计不同向量寄存器数 | 1 (≤4) / 2 (5-8) / skip (>8) |
| reduction 多条 fmla 写同一目标寄存器 | `vectorization_deepen` (accumulator_serial) | fmla/fadd 均写同一 vN | 1 (单acc) / 2 (双acc) / skip (≥4) |
| 同类型指令连续排列未交错 | `vectorization_deepen` (interleave_missing) | load-load-compute-compute-store 模式 | 2 (连续3+同类型) |
| 已有 SIMD + 128-bit 并行度 < 4 + 可拆分累加器 | `throughput-enhancement` | current_parallelism < 4；无循环携带依赖 | 2-3 |
| 循环内不可预测 if/else | `branch-elimination` | 条件依赖输入数据；分支体≤5-6行；无副作用 | 2 (miss rate>10%) / 3 (其他) |
| 循环内 switch（小范围连续值） | `branch-elimination` | 无副作用 + 值域≤256 | 3 |
| 连续/固定步长访存 + 工作集>L1d + 迭代≥32 | `prefetch-optimization` | stream 或 strided(步长≤16×element_size)；无间接索引 | 2 (L1d miss>8%) / 3 (其他) |
| AoS 结构体逐字段访问 | `memory-access-optimization` (aos_to_soa) | 仅访问部分字段 | 3（接口变更） |
| 大矩阵跨步遍历 + 工作集>L1d | `memory-access-optimization` (tiling) | 嵌套循环，内层不连续 | 2 |
| 内层循环访问不连续 | `memory-access-optimization` (reordering) | 交换循环顺序可改善连续性 | 2 |
| 数组 malloc 分配无对齐 + 向量化需要 | `memory-access-optimization` (cache_alignment) | NEON 需要 16 字节对齐 | 2 |
| 结构体内冷热字段混排 | `memory-access-optimization` (field_reorder) | perf annotate 显示访问频率不均 | 2（safe 模式跳过） |
| -O2 或更低 / 未指定 -march / 缺 -ftree-vectorize | `compiler-flag-tuning` | 构建系统可修改 | 始终 2 |
| 纯汇编文件 (.s/.S) + 任何可优化模式 | `asm-optimization` | file_type == "assembly" | 按子类型：1（机械变换）/2（需尾处理） |
| V 管线拥挤 + ALU 空闲 + 串行链在 V 管线 | `scalar-vector-hybrid` | V utilization 高，ALU utilization 低，串行链≥3条指令 | 1（V饱和+ALU空闲+链≥5）/2-3（其他） |
| 循环内 ≥2 个循环不变量 | `code_hoisting` | 不依赖迭代变量且无跨迭代修改 | 1（零风险） |
| 循环体单 store + 写值恒定或来自连续 load | `bulk-memory-opt` | 迭代≥32；无分支/函数调用 | 1（零风险） |
| 同函数多 ISA 变体 + 调用点分派 | `variant-selection` | 后缀匹配 _sve/_neon/_base 等 | 3（需实测） |
| ≥3轮连续位运算 / ≥5级数值区间分支链 | `algorithm-substitution` | LUT候选≤4096字节 或 闭式公式可用 | 3（仅提示，不自动执行） |

### 2.2 意图偏置规则

从用户 `intent` 读取偏好，调整策略优先级：

| intent.optimization_goal | asm-opt | vect | throughput | branch-elim | prefetch | mem-access | scalar-vector-hybrid |
|--------------------------|---------|------|-----------|------------|---------|-----------|---------------------|
| `throughput` | **+1** | **+1** | **+1** | 0 | 0 | 0 | **+1** |
| `latency` | 0 | 0 | 0 | **+1** | **+1** | 0 | 0 |
| `memory` | 0 | 0 | 0 | 0 | **+1** | **+1** | 0 |
| `balanced` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

- `+1` = 提升一级优先级（如 2→1）；`-1` = 降低一级
- `compiler-flag-tuning` 不受偏置影响（始终受益）
- `cross-platform` 约束下 vectorization/throughput-enhancement 直接进 skipped_points

---

## 3. 每种策略的实施方案

### 3.1 vectorization（标量→SIMD 向量化）

**分析清单**：
- [ ] 确认循环内无 NEON/SVE/SME intrinsics（`has_simd == false`）
- [ ] 确认无跨迭代数据依赖（前缀和/递推 → 不可向量化；累加型 → 可拆分累加器）
- [ ] 确认访存为连续（`a[i]` 模式，stride = sizeof(element)）
- [ ] 确认无 I/O/锁/原子/全局副作用
- [ ] 确认数据类型有 ARM 向量化映射（float/int32_t/int16_t/int8_t 均可）
- [ ] 查询 `01-kunpeng-hardware.md` 确认目标 CPU 的 ISA 特性（SVE 是否支持）

**实施步骤**：
1. **选择目标 ISA**：Kunpeng-0xd01 基础版 → NEON；0xd01 高配/0xd03/0xd06 → SVE（若有 gather/scatter/predication 需求）或 NEON（若无特殊需求）
2. **确定宽度**：NEON = 128-bit（4×float, 4×int32, 8×int16, 16×int8）；SVE = 256-bit（8×float）
3. **生成主循环**：用 `vld1q_f32`/`svld1_f32` 加载 → 向量运算 → `vst1q_f32`/`svst1_f32` 存储
4. **尾处理**：余数元素用标量循环处理（`i < N - N%4` 主循环 + 剩余 ≤3 元素标量）
5. **累加器拆分**：归约操作至少拆 2 个独立累加器（避免串行依赖阻塞流水线）

**常见陷阱 / Gotchas**：
- 指针别名（`restrict` 缺失）阻止编译器自动向量化，需手动加 `__restrict`
- 浮点归约顺序改变可能影响精度（`-ffast-math` 允许结合律）
- NEON load 要求 16 字节对齐，未对齐虽不崩溃但更慢
- 循环边界非编译期常量时，尾处理需正确计算 `N - N%4`

**路由**：委托 `apply-vectorization` skill。

---

### 3.2 vectorization_deepen（已有 SIMD 深挖质量）

当 `has_simd == true` 时，不跳过向量化方向，而是检查以下 6 个子类型。

| Sub-type | 检测 | 优化动作 | Priority |
|----------|------|---------|---------|
| `lane_width_partial` | 存在 `vadd_f32` 但无 `vaddq_f32`，或 SVE 只用 128-bit | 将 64-bit 操作升级为 128-bit；SVE 固定宽度为 256-bit | 1（64-bit为主）/ 2（混合） |
| `remainder_scalar` | 向量主循环后紧跟标量 for 处理余数 | 将余数处理改为向量化尾循环（load 最后 4 元素 + 掩码合并） | 1（余数>25%）/ 2 |
| `load_pair_missing` | 连续 vld1q 加载 2+ 个独立向量，但未用 `_x2/_x3/_x4` | 改为 `vld1q_f32_x2`/`_x3`/`_x4` 合并加载 | 1（3+个）/ 2（2个） |
| `register_underutilized` | 循环体 ≤4 个 NEON 寄存器（共 32 可用） | 增加展开因子或交错更多独立操作 | 1（≤4）/ 2（5-8） |
| `accumulator_serial` | reduction 中多条 fmla/fadd 写同一目标寄存器 | 拆分为多组独立累加器（如 v16/v17/v18/v19），循环结束后合并 | 1（单acc）/ 2（双acc） |
| `interleave_missing` | load-load-compute-compute-store 连续排列 | 交错排布：load → compute → load → compute → store | 2（3+同类型连续） |

**分析清单**：
- [ ] 统计 64-bit/128-bit/256-bit 指令比例
- [ ] 检查主循环后是否有标量尾循环
- [ ] 检查相邻 vld1q 指令数量和地址关系
- [ ] 统计循环体内不同 NEON 寄存器数量
- [ ] 检查 fmla/fadd 目标寄存器是否重复
- [ ] 检查指令类型排列模式（load/compute/store 是否交错）

**常见陷阱 / Gotchas**：
- Load pair 要求寄存器编号连续（如 `v0/v1` 或 `v16/v17`），需检查当前寄存器分配
- 累加器拆分后合并阶段需注意浮点结合律（结果可能与原始顺序差 1 ULP）
- 文件已是手写汇编 (.s/.S) → 跳过所有 vectorization_deepen，由 asm-optimization 处理
- 循环体 < 5 行的 `register_underutilized` → 跳过（太短不值得展开）

---

### 3.3 throughput-enhancement（循环展开）

**分析清单**：
- [ ] 确认已有 SIMD intrinsics（NEON/SVE/SME）
- [ ] 计算 `current_128bit_lanes`：NEON = 独立操作数×1，SVE = 独立操作数×2
- [ ] 确认 `current_128bit_lanes < 4`（Kunpeng 最大 4 通道）
- [ ] 确认无循环携带依赖（累加链可拆分 → 仍可展开）
- [ ] 检查寄存器预算：优先 caller-saved（NEON: v0-v7, v16-v31；通用: x0-x17）；不足时扩展到 callee-saved（NEON d8-d15 仅低64位；通用 x19-x29 需栈帧保护）

**展开因子公式**：
```
available_lanes = 4 - current_128bit_lanes
unroll_factor = available_lanes / current_128bit_lanes
向下取最近的 2 的幂（2, 4, 8, 16），最大 16
```

**实施步骤**：
1. 计算展开因子（参考上公式；0xd03 上 FMLA=4c → 至少展开 4 次隐藏延迟）
2. 展开循环体：每份使用独立临时变量/寄存器
3. 累加器拆分：归约操作分多组独立累加器，循环结束后合并
4. 尾处理：迭代数不整除展开因子时添加 epilogue 循环
5. 更新 clobber 列表（内联 asm 场景）

**常见陷阱 / Gotchas**：
- 寄存器溢出（spill）：展开后寄存器不足 → 降级 unroll_factor
- L1d < 32KB 时展开因子 ×0.5（避免代码膨胀驱逐热数据）
- Micro-kernel 内部 K 循环可 full-unroll（编译期 K 常量，寄存器预算通过）
- Full-unroll 需记录 `code_size_risk`（low/medium/high）

**路由**：委托 `loop-unrolling` skill。

---

### 3.4 branch-elimination（分支消除）

**分支分类与转换方法**：

| 分支模式 | 可消除性 | 转换方法 | 适用条件 |
|---------|---------|---------|---------|
| 简单条件赋值 `if(c) x=a; else x=b;` | 高 | `csel` / `vbslq` / `svsel` | 分支体 ≤5-6 行赋值 |
| 三元运算 `x = c ? a : b` | 中 | 编译器通常已生成 csel，需反汇编确认 | — |
| 条件累加 `if(c) sum += val` | 高 | 掩码 + 条件乘加（`vcgtq` → `vandq` → `vaddq`） | val 可向量化 |
| 条件调用 `if(c) func()` | 低 | **不可消除**（有副作用） | — |
| Switch 小范围连续值 | 中 | LUT 函数指针表（仅当分支预测失败代价 > 间接调用开销） | 值域 ≤256，分支体为赋值 |
| Switch 非连续值 | 低 | 条件选择链（收益有限） | — |
| 数值区间分支链 ≥5 级 | 中 | 闭式公式（clz/除法）+ 条件选择 | 区间呈等比/等差数列 |

**拒绝条件**（命中任一即跳过）：
- 分支体涉及函数调用 / I/O / 全局状态修改
- 分支体代码量 > 6 行（条件选择不如分支高效）
- 分支模式高度可预测（循环不变条件、`i%2` 等 → CPU 已处理）
- 三元运算符已在源码中使用（编译器通常已生成 `csel`）

**实施步骤（NEON 路径）**：
1. 用 `vcgtq_f32`/`vcltq_f32`/`vceqq_f32` 生成比较掩码
2. 计算两个分支的结果到独立向量寄存器
3. 用 `vbslq_f32` 按掩码选择结果
4. 验证语义等价（含边界条件、NaN/Inf 行为）

**常见陷阱 / Gotchas**：
- `vbslq` 是位选择（逐 bit），不是值选择；确保掩码为全0或全1
- Kunpeng-0xd01 分支误预测惩罚 ~15-20 cycles，CSEL 仅 1 cycle
- 函数指针 LUT 无法内联，仅当分支预测失败代价 > 间接调用开销时使用

**路由**：委托 `branch-elimination` skill。

---

### 3.5 prefetch-optimization（软件预取）

**决策树**：
```
访问模式分析
├── 连续访问 (stream, stride = sizeof(element))
│   ├── L1d miss rate > 5% → 插入预取（距离 8-32 元素）
│   └── L1d miss rate < 5% → 拒绝（数据已缓存）
├── 固定步长 (strided, stride ≤ 16×element_size)
│   └── 手动插入 __builtin_prefetch / prfm
├── 间接索引 (indirect, a[index[i]])
│   └── 拒绝（访存地址不可预测）
└── 不规则 (pointer-chase, 链表/树)
    └── 拒绝，建议数据布局重构
```

**距离计算公式**：
```
prefetch_distance = ceil(memory_latency / iteration_time)
```
- Kunpeng-0xd01 典型值：L2 命中 ~10ns, L3 ~30ns, DDR ~100ns
- C/C++ 路径：距离取 8-32 元素，向下取 2 的幂
- 汇编路径：距离 = 元素数 × sizeof(element)，如 16 floats = 64 字节偏移

**实施步骤**：
1. 确定目标缓存层级：L2（pldl2keep，避免 L1 污染）或 L1（pldl1keep，多次访问）
2. 计算预取距离，在循环开头插入 `__builtin_prefetch(&arr[i + DIST])` 或 `prfm pldl2keep, [x0, #64]`
3. 条件预取拆分：循环剩余数据不足 `DIST + step` 时，拆为有预取版 + 无预取版循环
4. 验证预取指令不改变程序语义

**常见陷阱 / Gotchas**：
- 过度预取：距离 > 32 元素（C/C++）或 > 256 字节偏移（汇编）会 evict 正在使用的缓存行
- 无效预取消耗带宽：`prfm` 不产生异常，但仍消耗内存带宽；必须做条件拆分
- 工作集 < L1d 时预取无收益，反而增加指令开销
- 已存在手写 `prfm` 指令 → 跳过

**路由**：委托 `prefetch-optimization` skill。

---

### 3.6 memory-access-optimization（访存模式优化）

5 种子类型，按侵入性从低到高排列：

| Sub-type | 侵入性 | 风险 | 典型收益 | 适用场景 |
|----------|-------|------|---------|---------|
| `cache_alignment` | 低（仅改分配方式） | 低 | 5-10% | NEON load 未对齐 / false sharing |
| `reordering` | 中（循环顺序变更） | 中（可能改变归约顺序） | 1.2-2x | 内层不连续遍历 |
| `field_reorder` | 中（结构体定义变更） | 中（ABI 兼容性） | 5-15% | 冷热字段混排 |
| `tiling` | 中（新增嵌套层级） | 中 | 1.5-3x | 工作集 > L1d/L2 |
| `aos_to_soa` | 高（数据布局重构） | 高（接口变更） | 2-4x | AoS 逐字段访问 |

**分析清单**：
- [ ] 识别数据布局（AoS/SoA/混合）
- [ ] 计算工作集大小，与缓存层级比较
- [ ] 检查访问步长和连续模式
- [ ] 确认结构体字段访问频率（perf annotate 交叉引用）
- [ ] 评估接口变更影响范围（AoS→SoA）
- [ ] 检查是否存在 `offsetof()` 依赖或序列化协议依赖（field_reorder）

**实施步骤（以 tiling 为例）**：
1. 计算分块大小：`3 × BLOCK² × sizeof(float) ≤ L1d_size`。L1d=64KB → BLOCK ≤ 74，取 64
2. 添加外层分块循环（ii/jj/kk）
3. 内层循环边界用 `min(ii+BLOCK, M)` 处理余数
4. 保留原循环逻辑不变，仅改迭代范围

**常见陷阱 / Gotchas**：
- AoS→SoA 修改数据布局影响全局接口，必须在 `risk_tolerance != "safe"` 时才进入
- 循环重排可能改变浮点归约顺序（非结合律），需标注
- 字段重排改变 ABI，确认无序列化/网络协议依赖
- Tiling 的 BLOCK 大小需按缓存层级计算，过大反而增加 miss

**路由**：委托 `memory-access-optimization` skill。

---

### 3.7 compiler-flag-tuning（编译选项调优）

**分析清单**：
- [ ] 读取当前编译选项（cflags/cxxflags/ldflags）
- [ ] 确定 CPU Part ID（`grep 'CPU part' /proc/cpuinfo`）
- [ ] 验证 ISA 特性（从 `01-kunpeng-hardware.md` ISAs 速查表）
- [ ] 检查优化级别（`-O2` 以下 → 升级到 `-O3`）
- [ ] 检查 `-march` 是否与硬件匹配

**TSV110 关键警告**：`-mcpu=tsv110` / `-mtune=tsv110` **仅适用于 Kunpeng-0xd01**。对 Kunpeng-0xd03/0xd06 (0xd03/0xd06) 使用会生成错误调度代码，**性能下降**。不确定型号时用 `-mcpu=native`。

**CPU→Flag 映射（正确选项）**：

| CPU | `-mcpu` | `-march` | 可用扩展 |
|-----|---------|----------|---------|
| 0xd01 基础版 | `-mcpu=tsv110` | `-march=armv8.2-a` | `+crypto` |
| 0xd01 高配 | `-mcpu=tsv110+sve` | `-march=armv8.2-a+sve` | `+crypto+sve` |
| 0xd03 | `-mcpu=native` | `-march=armv9-a` | `+crypto+sve+sve2+bf16` |
| 0xd06 | `-mcpu=native` | `-march=armv8.5-a` | `+crypto+sve+sve2` |
| 未知 | `-mcpu=native` | 不动 | 仅加 `-O3`/`-flto` |

**实施步骤**：
1. 从 `prepareProject` 读取当前编译选项
2. 确定 CPU 型号，定位正确的 `-march`/`-mcpu`
3. 按安全顺序推荐：架构选项 > 优化级别 > 循环优化 > LTO > `-ffast-math` > PGO
4. 修改构建配置文件（CMakeLists.txt/Makefile）
5. `-ffast-math` 和 PGO 需用户确认

**常见陷阱 / Gotchas**：
- 盲加 `-mcpu=tsv110` 到非 0xd01 机器 → 性能下降（调度不匹配）
- `-march` 扩展 isa_features 中不存在的特性 → SIGILL
- `-ffast-math` 影响浮点精度（次正规数 flush-to-zero、不设 errno）
- LTO 增加 2-3x 链接时间，某些项目可能不兼容
- PGO 需代表性负载 + 两轮构建，不自动执行

**路由**：委托 `compiler-flag-tuning` skill。

---

### 3.8 asm-optimization（汇编指令级优化）

仅适用于 `.s`/`.S` 纯汇编文件和 C/C++ 内联 asm 块。12 种子类型：

| # | Sub-type | 说明 | Priority | 风险 |
|---|-----------|-----|---------|------|
| 1 | `ldp_stp_merge` | 连续 ldr/str 对合并为 ldp/stp | 1 | 零（机械变换） |
| 2 | `post_index_addressing` | ldr/str + add → 后索引寻址 | 1 | 零（机械变换） |
| 3 | `redundant_move_elimination` | 消除冗余 mov 指令 | 1 | 零（def-use 分析保证） |
| 4 | `loop_counter` | subs 折叠 cmp（模式A）/ 倒计数转换（模式B） | 1 | 零（语义等价） |
| 5 | `instruction_idiom` | mov#0→eor/xzr；mov+add→add 折叠 | 1 | 零（编码等价） |
| 6 | `multi_vector_merge_test` | N 个 test+branch → OR 归约+单分支 | 1 | 零（语义等价） |
| 7 | `loop_unroll` | 循环展开（asm 路径） | 2 | 中（需尾处理） |
| 8 | `prefetch_enhancement` | 汇编 prfm 插入 | 2 | 低（委托 prefetch-optimization） |
| 9 | `macro_fusion_enablement` | 指令重排使 subs/cmp 与 b.cond 相邻 | 2 | 中（需指令重排） |
| 10 | `uarch_substitution` | 微架构感知指令替换（分离乘加→fmla等） | 1-2 | 低（需 query_tsv110/0xd03 验证） |
| 11 | `instruction_interleaving` | 基于端口压力检测的交错调度 | 2 | 中（依赖分析） |
| 12 | `predication_mode` | SVE ptrue→whilelt 尾谓词消除尾循环 | 2 | 中（SVE only） |

**分析清单**：
- [ ] 确认 file_type == "assembly"（`.s`/`.S` 或内联 asm）
- [ ] 统计 ldr/str 对数量和间距
- [ ] 检测 post-index 候选（ldr/str + add 模式）
- [ ] 在 5 指令窗口内构建 mov def-use 链
- [ ] 检测 sub+cmp+b.cond 三连模式
- [ ] 检测连续 test+branch 合并候选
- [ ] 检测 subs/cmp 与 b.cond 之间的指令间隔

**实施步骤（以 ldp_stp_merge 为例）**：
1. 扫描连续两条 ldr/str，检查基址相同、偏移差 = 8/4/16
2. 确认两指令间无 label 或基址修改
3. 验证 LDP/STP offset 在有效范围（[-512, 504]，8 字节对齐）
4. 合并为单条 ldp/stp 指令

**常见陷阱 / Gotchas**：
- LDP/STP 不可跨越汇编 label
- 后索引立即数范围 0-32760，8 字节对齐
- 不修改 callee-saved 寄存器（x19-x29）的保存/恢复
- Macro fusion 重排不可破坏 RAW/WAR/WAW 依赖
- `loop_unroll` 和 `prefetch_enhancement` 委托子 skill，不重复实现
- SVE 实现由 `apply-vectorization` 和 `loop-unrolling` 处理

**路由**：委托 `asm-optimization` skill。

---

### 3.9 scalar-vector-hybrid（标矢量混合决策）

**核心原则**：性能由最慢的管线决定。当 V 管线被并行计算占满时，串行依赖链应搬到空闲的 ALU 管线。

**决策流程**：
```
1. 找到串行依赖链 → check pipeline_origin
   ├── 链在 ALU 管线 → 保持现状（无需混合）
   └── 链在 V 管线 → 继续判断
2. 检查可标量化性
   ├── 含 TBL/AESE/PMULL 等专有指令 → 保留 V（不可标量化）
   ├── 链长 < 3 → 拒绝（fmov 搬移开销无法摊薄）
   └── 链长 ≥ 3 且 6+/7 可标量映射 → 继续判断
3. 计算 fmov 搬移开销
   ├── fmov_count × fmov_latency + scalar_chain_cycles < neon_chain_cycles → 执行
   └── 不满足 → 拒绝（搬移开销超过收益）
4. 对比管线利用率差距
   ├── V 利用率高 + ALU 利用率低 + 差距大 → 标量化串行链
   └── 差距小 → 可能收益有限
```

**NEON→标量映射参考**：

| NEON | 标量等价 | 延迟对比 (TSV110) | 可映射性 |
|------|---------|-------------------|---------|
| `shl vN.2d, vN.2d, #1` | `lsl xN, xN, #1` | V:2c vs ALU:1c | direct |
| `ushr vN.2d, vN.2d, #63` | `lsr xN, xN, #63` | V:2c vs ALU:1c | direct |
| `and vN.16b, vN.16b, vM.16b` | `and xN, xN, xM` | V:2c vs ALU:1c | direct |
| `eor vN.16b, vN.16b, vM.16b` | `eor xN, xN, xM` | V:2c vs ALU:1c | direct |
| `orr vN.16b, vN.16b, vM.16b` | `orr xN, xN, xM` | V:2c vs ALU:1c | direct |
| `ext vN.16b, vM.16b, vK.16b, #8` | `lsl + eor + and` (3条) | V:2c vs ALU:~3c | needs_decomposition |
| `tbl vN.16b, {vM.16b}, vK.16b` | — | — | **unmappable** |
| `aese vN.16b, vM.16b` | — | — | **unmappable** |
| `pmull vN.1q, vM.1d, vK.1d` | — | — | **unmappable** |

**常见陷阱 / Gotchas**：
- 128 位 NEON 操作拆为 2 个 64 位标量操作，注意低/高 64 位顺序
- `fmov xN, dN` 只搬移低 64 位；高 64 位需要 `fmov xN, vN.d[1]`
- AArch64 位掩码立即数要求连续 1：`0x87` 不满足，必须 `mov xN, #0x87; and ...`
- 寄存器别名：宏/内联函数中 src 和 dst 可能相同 → 用临时寄存器

**路由**：委托 `scalar-vector-hybrid` skill。

---

### 3.10 其他策略速查

#### code_hoisting（循环不变量外提）

- **触发**：循环内 ≥2 个变量不依赖迭代变量且无跨迭代修改
- **实施**：将不变量的 load/计算移到循环外，循环内直接使用
- **风险**：零（编译器通常已做，但 O2 下可能遗漏；内联函数调用的上下文参数是常见盲区）
- **路由**：在 `apply-optimization` 中直接 Edit，不委托子 Skill

#### bulk-memory-opt（批量内存操作）

- **触发**：循环体单 store + 写值恒定（→ memset 候选）或来自连续 load（→ memcpy 候选），迭代 ≥32
- **实施**：替换为 `memset(dst, val, N*sizeof(T))` 或 `memcpy(dst, src, N*sizeof(T))`
- **风险**：零（libc 实现已全平台调优）
- **路由**：在 `apply-optimization` 中直接 Edit，不委托子 Skill

#### variant-selection（函数变体选型）

- **触发**：同函数存在 `_sve`/`_neon`/`_base` 等多变体，调用点有 ISA 分派
- **实施**：对每个变体 `perf stat` 运行相同 test_cases，选择最优变体设为默认
- **风险**：低（仅修改分派逻辑）
- **注意**：需实测验证，不可静态推断；priority=3

#### algorithm-substitution（算法替换）

- **触发**：≥3 轮位运算（LUT 候选），或 ≥5 级数值区间分支链（闭式公式候选）
- **实施**：仅生成提示和建议文本，**不自动替换**（语义变更需人工判断）
- **风险**：高（正确性、精度、接口契约可能受影响）
- **注意**：`decide-optimization` 自动 skip，由用户在阶段总结中手动决定

---

## 4. 使用指南

interactive-optimizer 代理使用本参考时，执行以下流程：

### 步骤 1：匹配代码特征到策略

从 `analyze-hotspot` 输出的 `static_analysis` 和 `dynamic_analysis` 中提取代码特征，对照 **第 2 节映射表** 找到匹配的策略。多个策略同时匹配时，按 **第 1 节优先级** 排序。

### 步骤 2：阅读策略实施方案

找到匹配策略后，查阅 **第 3 节** 对应条目：
1. 先看 **分析清单**，确认前置条件全部满足
2. 再看 **实施步骤**，按编号顺序执行
3. 特别注意 **常见陷阱**（Gotchas），避免已知错误

### 步骤 3：交叉验证安全约束

打开 `04-safety-and-gotchas.md`，交叉检查：
- 策略的风险等级是否匹配用户 `intent.risk_tolerance`
- 平台约束（`cross-platform` 下跳过平台特定 intrinsics）
- 浮点精度约束（`-ffast-math`/循环重排的影响）

### 步骤 4：查询精确指令数据

当优化涉及具体指令选择、延迟估算或端口压力计算时，打开 `02-instruction-reference.md` 获取：
- 目标指令的延迟（cycles）和吞吐量（/cycle）
- 执行端口分配（判断管线争用）
- 指令编码约束（立即数范围、寄存器对齐）

### 步骤 5：应用优化

根据策略的 **路由** 信息：
- 标注 `委托 <skill>` 的策略 → 构造委托 JSON，调用对应 Skill
- 标注 `在 apply-optimization 中直接 Edit` 的策略 → 直接 Read 源码后 Edit
- 标注 `仅提示` 的策略（algorithm-substitution）→ 生成建议文本，不修改代码

### 常见决策速查

| 场景 | 策略选择 | 理由 |
|------|---------|------|
| 标量循环 + 连续访存 + 热点 | vectorization | 最高收益（2-4x） |
| 已有 NEON 但 64-bit 为主 | vectorization_deepen (lane_width_partial) | 升级宽度，零风险 |
| NEON 循环 + 单 accumulator | vectorization_deepen (accumulator_serial) + throughput-enhancement | 拆累加器+展开 |
| 循环内 if/else + miss rate >5% | branch-elimination | csel 替代分支 |
| 大数据量连续访问 + L1 miss 高 | prefetch-optimization | 软件预取隐藏延迟 |
| 工作集 > L2 + 嵌套循环 | memory-access-optimization (tiling) | 分块适配缓存 |
| V 管线饱和 + ALU 空闲 + 串行链 | scalar-vector-hybrid | 标量化串行链，释放 V 管线 |
| O2 编译 + 未指定 -march | compiler-flag-tuning | 零代码变更，编译器自动优化 |
