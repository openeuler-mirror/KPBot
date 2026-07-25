# MCA 输出解读参考

本文件解释 `llvm-mca-analysis` 输出的各项指标含义、瓶颈类型如何解读，以及 MCA 仿真的固有局限。分析复杂结果或向用户解释"为什么"时阅读本文件。

## 目录

1. [汇总指标](#汇总指标)
2. [瓶颈类型](#瓶颈类型)
3. [逐资源端口压力](#逐资源端口压力)
4. [关键依赖序列](#关键依赖序列)
5. [Dispatch 停顿](#dispatch-停顿)
6. [各微架构资源命名](#各微架构资源命名)
7. [MCA 的局限与准确性](#mca-的局限与准确性)
8. [MCA vs perf/SPE](#mca-vs-perfspe)

## 汇总指标

| 指标 | 含义 | 怎么读 |
|------|------|--------|
| `ipc` | 每周期退役指令数 | 越高越好。受 dispatch 宽度、依赖链、端口吞吐三重约束。接近 dispatch_width 说明前端没拖后腿。 |
| `block_rthroughput` | 每迭代稳态周期数下界 | **越低越好**。是吞吐瓶颈的逆指标：MCA 认为每迭代至少要这么多周期。 |
| `cycles_per_iteration` | 每迭代实际周期数（total_cycles/iterations） | 与 block_rthroughput 对照，接近说明已到吞吐下界。 |
| `dispatch_width` | 每周期最多发射指令数 | IPC 的理论上限。各 uarch 不同（hip08=4, hip09=6, hip10c=12, hip11=4, hip12=16）。 |
| `uops_per_cycle` | 每周期 uOps | 与 IPC 接近；复杂指令（>1 uOp）时 uOps/Cycle > IPC。 |
| `total_uops` / `instructions` | 总 uOps / 总指令（含所有迭代） | 单迭代 = 总数 / iterations。看 `instructions_info` 里哪些指令 uOps>1。 |

## 瓶颈类型

`bottleneck.primary_type` 是 MCA 对"什么限制了吞吐"的归因。MCA 把后端压力周期拆成两大类：

- **Resource Pressure（资源压力）**：指令争用同一执行端口，端口吞吐饱和。对应 `resource_pressure_pct` 和 `limiting_resources`（饱和资源及各自占比）。
- **Data Dependencies（数据依赖）**：指令间 RAW 依赖，后指令必须等前指令结果。对应 `data_dependencies_pct`，细分为 `register_dependencies_pct`（寄存器依赖）和 `memory_dependencies_pct`（地址依赖）。

`primary_type` 判定逻辑（脚本 `classify_bottleneck`）：

| primary_type | 条件 | 解读 |
|--------------|------|------|
| `resource_pressure` | 资源压力 > 数据依赖 × 1.5 | 端口饱和是主因。优化：减少对该资源的需求（换更少 uOps 的指令、融合乘加、打断依赖让指令能并行到其他端口）。 |
| `data_dependency` | 数据依赖 > 资源压力 × 1.5 | 依赖链是主因。优化：拆累加器打破链、寄存器重命名、循环展开提升 ILP。 |
| `mixed` | 两者相当 | 先动依赖链（解锁 ILP 后端口压力可能自行缓解），再看端口。 |
| `low_pressure` | 后端压力周期占比 < 10% | 后端不是瓶颈。怀疑前端（fetch/decode）、dispatch/retire，或 cache/内存（MCA 不建模）。 |
| `unknown` | 无瓶颈分析数据 | iterations 太少或 MCA 未产出瓶颈段。调大 `--iterations` 重试。 |

`backend_pressure_cycles_pct` 是"有后端压力增加的周期占比"--这个值高说明后端是瓶颈，低说明瓶颈在别处。

## 逐资源端口压力

`resource_pressure_per_iteration` 是每迭代每个执行资源的占用（MCA 的 "Resource pressure per iteration" 视图）。值越大说明该资源越繁忙。解读要点：

- **找最大值**：压力最高的资源就是吞吐瓶颈候选（与 `limiting_resources` 互印证）。
- **资源容量**：分组资源的子单元（如 `TSV110UnitAB.0` 和 `.1`）表示该组有 2 个并行端口；压力接近子单元数说明该组接近满载。
- **零压力资源**：说明这些端口闲置，理论上可以把指令调度过去（这就是指令交错调度优化的依据）。

注意：`limiting_resources` 来自 MCA 的 bottleneck-analysis 文本段（MCA 自己挑出的饱和资源），比单纯看压力最大值更权威--MCA 会考虑资源容量与冲突概率。

## 关键依赖序列

`critical_sequence` 是 MCA 认定构成瓶颈的指令序列，每条带依赖标注：

| dependency_type | detail 形式 | 含义 |
|-----------------|------------|------|
| `resource_interference` | `RESOURCE interference: <资源> [probability: X%]` | 该指令与它指令争用同一资源 |
| `register` | `REGISTER dependency: <寄存器>` | 等待某寄存器的写后读 |
| `memory` | `MEMORY dependency: <地址寄存器>` | 等待某地址的访存 |
| `other` | 其他标注 | |

`< loop carried >` 标注表示跨迭代依赖（上一轮结果被本轮使用），常见于 post-index 地址更新（如 `ldr q0, [x0], #16` 每轮更新 x0，下一轮的 ldr 依赖 x0）和循环携带累加。打破 loop-carried 依赖通常收益最大（累加器拆分、指针多路推进）。

## Dispatch 停顿

`dispatch_stalls` 给出各停顿原因的周期数（MCA 的 "Dynamic Dispatch Stall Cycles"）：

| 字段 | 含义 | 高值指向 |
|------|------|---------|
| `RAT` | Register Alias Table 寄存器不可用 | 物理寄存器耗尽，寄存器压力过高 |
| `RCU` | Retire Control Unit token 不足 | ROB 满，长期指令未退役（依赖链/长延迟指令） |
| `SCHEDQ` | 调度器队列满 | 调度窗口被长延迟指令占满 |
| `LQ` / `SQ` | Load/Store 队列满 | 访存密集（注意：MCA 不算 cache miss，这里只反映队列容量） |
| `GROUP` | dispatch 组静态限制 | 指令组合约束 |
| `USH` | 未分类结构冒险 | |

`RCU` 高常与 `data_dependency` 伴生（依赖链让指令迟迟不能退役，ROB 填满）。

## 各微架构资源命名

不同 uarch 的资源命名前缀不同，`limiting_resources` 和 `resource_pressure_per_iteration` 里会用到。命名约定：`<UARCH>Unit<类型>[.子单元]`。

| uarch (-mcpu) | 整数 ALU | 浮点/向量 | 访存 | 乘除 | 分支 | 资源总数 |
|---------------|---------|-----------|------|------|------|---------|
| hip08 (tsv110) | AB(.0/.1), ALU | FSU1, FSU2 | Ld0St, Ld1 | MDU | (并入 AB) | 8 |
| hip09 (hip09) | ALUM0/1, ALUS0/1, ALUS23.0/1 | FSU0, FSU2, FSU13.0/1 | LD.0/1, ST.0/1, STD.0/1 | (并入 ALUM) | BRU.0/1 | 18 |
| hip10 (hip10c) | S0, S1, S23.0/1 | F0, F1, V (V0-V3) | LD.0/1, ST.0/1, STD.0/1 | M0, M1 | B.0/1 | 16 |
| hip11 (hip11) | ALU | FSU1, FSU2, FSTD | Ld0St, Ld1 | MDU | BRU1, BRU2 | 9 |
| hip12 (hip12) | S0, S1, S3, S4, SM2, SM5 | V0, V1, V2, V3 | LD.0/1/2, ST.0/1, STD.0/1 | (并入 SM) | B.0/1 | 19 |

命名规律：
- `ALU` / `S` / `AB`：标量整数 ALU。`AB` = 地址生成 + ALU 复用（tsv110）。
- `FSU` / `F` / `V`：浮点/SIMD 执行单元。`FSU` = Float/SIMD Unit，`V` = Vector。
- `LD` / `ST` / `STD`：load / store-address / store-data 端口。
- `MDU` / `M` / `SM`：乘除单元（Mul/Div）。
- `BRU` / `B`：分支单元。
- `.0` / `.1` / `.2` 后缀：同一组的并行子端口。

看到 `limiting_resources` 里某资源饱和时，对照上表判断是哪类功能单元饱和，再据此给优化方向。

## MCA 的局限与准确性

MCA 是**静态后端流水线仿真**，建模 dispatch、scheduler、执行单元、寄存器堆、ROB。它有明确的边界，解读时必须牢记：

1. **不建模 cache / 内存层级**：load/store 在 MCA 里只有执行延迟（几个周期），没有 cache miss（几十到几百周期）。所以 MCA 对访存密集代码的 IPC 偏乐观。若 MCA 说 IPC 很高但实测慢，瓶颈大概率在 cache/内存，要用 perf/SPE 查。
2. **不跟随控制流**：MCA 把输入当直线序列仿真，分支不改变流向。分析循环时务必把循环体单独抽出（asm 模式），否则函数级的分支/尾部会让稳态分析失真。
3. **依赖调度模型质量**：结果取决于 LLVM 调度模型对该 uarch 的刻画精度。hip 系列模型是 HiSilicon 贡献的，但仍有简化（如不建模某些微架构细节）。把 MCA 当成"纸面预估"（paper prediction），与硅片实测（silicon）对照使用，不要当成精确预言。
4. **不建模前端**：fetch/decode、分支预测命中与否不在 MCA 范围。`low_pressure` 时前端可能是真凶。
5. **指令必须是 MCA 能识别的**：罕见指令若调度模型里没刻画，MCA 会标 `has_unmodeled_side_effects` 或给保守估计，结果不可靠。看 `instructions_info` 里这类标记。

## MCA vs perf/SPE

| 维度 | MCA（本 Skill） | perf / SPE |
|------|----------------|------------|
| 类型 | 静态仿真 | 动态采样 |
| 需要运行 | 否（只需汇编） | 是（需可运行的二进制+负载） |
| 瓶颈定位 | 计算端口饱和、依赖链 | cache miss、分支误预测、真实 IPC |
| 内存层级 | 不建模 | 建模（SPE 能看 L1/LLC miss） |
| 适合 | 改代码前预估、指令级瓶颈归因、对比候选指令序列 | 改代码后验证、定位真实热点、内存/分支瓶颈 |

两者互补：先用 MCA 预估指令级瓶颈并指导优化方向，再用 perf/SPE 验证真实效果。MCA 预估与 perf 实测的偏差本身就是诊断信息（偏差大 → 怀疑 MCA 没建模的内存/前端因素）。
