# 鲲鹏硬件快速参考

> 用途：interactive-optimizer 性能分析过程中快速查阅微架构参数，避免每次派生子代理查询。
> 完整细节见 `pipeline/skills/kunpeng_microarch/kunpeng*-microarchitecture.md`。

---

## 1. CPU 型号识别

### 检测命令

```bash
grep -m1 'CPU part' /proc/cpuinfo
```

### Part ID → 微架构 → 编译选项映射

| CPU Part ID | 型号 | 微架构 | 核心代号 | ISA 基线 | `-mcpu` / `-mtune` | `-march` |
|-------------|------|--------|---------|---------|--------------------|-----------|
| `0xd01` | Kunpeng-0xd01 | **TSV110** | TaiShan v110 | ARMv8.2 | `-mcpu=tsv110` | `-march=armv8.2-a` |
| `0xd03` | Kunpeng-0xd03 / 0xd03 | **0xd03** | LinxiCore | ARMv9-A | `-mcpu=native`（**严禁 tsv110**） | `-march=armv9-a` |
| `0xd06` | Kunpeng-0xd06 | **LC950** | LinxiCore | ARMv8-A (v9.2 兼容) | `-mcpu=native`（**严禁 tsv110**） | `-march=armv8.5-a` |

> **关键规则**：`-mcpu=tsv110` 仅对 Part `0xd01`（Kunpeng-0xd01）有效。对 `0xd03`/`0xd06` 使用 `-mcpu=tsv110` 会导致错误调度，**性能下降**。不确定型号时使用 `-mcpu=native`。

### ISA 特性速查

| 特性 | TSV110 (0xd01) | TSV110 高配 (0xd01) | 0xd03 | LC950 (0xd06) |
|------|-------------|-------------------|-------------|-------------|
| NEON | yes | yes | yes | yes |
| SVE | **no** | yes (256-bit) | yes (SVE + SVE2, 256-bit) | yes (SVE + SVE2, 256-bit) |
| Crypto (AES/SHA/PMULL) | yes | yes | yes | yes |
| DotProd | no (基础版) | yes | yes | yes |
| FP16 | yes | yes | yes | yes |
| BFloat16 | no | no | yes (v8.6) | yes (v8.6) |
| SVE2 | no | no | yes (v9.0) | yes |
| SME | no | no | no | no |

---

## 2. TSV110 (Kunpeng-0xd01) 微架构参数

### 2.1 流水线总览

| 阶段 | 宽度 | 说明 |
|------|------|------|
| Fetch | 4 指令/cycle | L1I 命中时 |
| Decode | 4-wide | 含 Move Elimination |
| Rename/Allocate | ROB ~96 entry | Goldmont Plus 量级 |
| Issue | ≤4 uop/cycle | 3 个统一调度器（ALU/Mem/FP），各 ~33 entry |
| Commit | 4 uop/cycle | — |

### 2.2 执行端口

| 端口 | 功能 | 延迟/吞吐 |
|------|------|----------|
| P0 | 通用 ALU（不支持分支） | 1c, 1/cycle |
| P1 | 通用 ALU + Taken Branch | 1c, 1/cycle |
| P2 | 通用 ALU + Taken Branch | 1c, 1/cycle |
| P3 | 整数乘法/除法 (MDU) | mult 4c, div 多周期; 1/cycle |
| P4 | FP/SIMD (FMA FP32/FP64, FP32 ADD, 向量整数 ADD/MUL) | FP32 FMA 5c; 128-bit |
| P5 | FP/SIMD (FMA FP32/FP64, FP32 MUL, 向量整数 ADD) | FP32 FMA 5c; 128-bit |
| P6 | Load 或 Store (AGU0) | L1D hit 4c |
| P7 | Load 或 Store (AGU1) | L1D hit 4c |

- 整数 ALU 总吞吐：3/cycle（P0+P1+P2）
- FP32 FMA 吞吐：2/cycle（P4+P5）；FP64 FMA：0.5/cycle（四分之一速率）
- Load 带宽：2/cycle；或 1 Load + 1 Store/cycle
- FP32 ADD 仅 P4，FP32 MUL 仅 P5 → 混合 ADD/MUL 需交错排布

### 2.3 向量/NEON/SVE 资源

| 参数 | 数值 |
|------|------|
| NEON 向量宽度 | 128-bit（4×32-bit lane） |
| FP32 FMA/cycle | 2（P4 + P5） |
| SVE 支持 | **基础版不支持**；高配版 SVE 256-bit 占 2 个 NEON lane |
| FP/向量 PRF 压力 | 🟠 中高 — AArch64 32 个架构 FP 寄存器，可重命名槽更少 |

> **PRF 警告**：浮点密集代码应减少活变量以缓解 FP/向量 PRF 压力。SVE 256-bit 操作占用 2 个 128-bit lane，无吞吐量增益——仅在谓词化、gather/scatter、长度无关循环等场景选择 SVE。

### 2.4 缓存层次

| 层级 | 容量 | 相联度 | 命中延迟 | 关键特征 |
|------|------|--------|---------|---------|
| L1 I-Cache | 64 KB | — | — | — |
| L1 D-Cache | 64 KB | 4-way | 4c (简单寻址), 5–6c (索引寻址) | 2×128-bit/cycle 读带宽, 16B 对齐块 |
| L2 Cache | 512 KB/core (私有) | 10-way | 10c | ~32B/cycle 接口, 优于 Neoverse N1 |
| L3 Cache | 每 Cluster 1 Bank | — | Partition 近端 ~36c, Shared >90c | Ring Bus 互联, Tag 在 Cluster 侧 |
| Cache Line | 64 B | — | — | — |
| DRAM | DDR4-2400, 4 通道 | — | 96ns (空载), >300ns (满载) | 63 GB/s 读带宽 |

> **L3 关键规则**：Partition 近端 ~36c vs Shared >90c，3 倍差值。单核工作集 <4 MB 保持 Partition 模式，避免触发 Shared 模式。

**TLB 层次**：

| 层级 | 条目 | 命中延迟 |
|------|------|---------|
| L1 I-TLB | 32 (全相联) | — |
| L1 D-TLB | 32 (全相联) | — |
| L2 Unified TLB | 1024 | +11c |

> L2 TLB +11c 代价高，大页 (2MB/1GB) 收益显著。

### 2.5 分支预测器

| 参数 | 数值 |
|------|------|
| 类型 | 两级动态分支预测器 |
| 一级 BTB | 64 entry，单周期跳转目标，零气泡 Taken Branch |
| 二级覆盖 | 32KB 代码内 3c 处理 |
| 返回地址栈 (RAS) | 31 entry |
| 间接分支 | 最多 16 历史周期，~256 间接目标 |
| 分支间距惩罚 | ≤16B 间距 +1c |
| 误预测惩罚 | 15–20c |

### 2.6 IPC 健康阈值

| IPC 范围 | 状态 | 说明 |
|----------|------|------|
| <1.0 | 🔴 严重停顿 | 前端/访存/分支瓶颈明显，优先排查 |
| 1.0–2.0 | 🟡 中等 | 有优化空间，检查端口利用率和缓存命中 |
| >2.0 | 🟢 良好 | 4-wide 机器上接近饱和，关注微架构细节 |

---

## 3. 0xd03 (Kunpeng-0xd03 / 0xd03 / 0xd03) 相对 TSV110 的差异

| 维度 | TSV110 (0xd01) | 0xd03 | 变化 |
|------|-------------|-------------|------|
| **ISA** | ARMv8.2 | ARMv9-A | 升级 |
| **SVE** | 基础版无 / 高配有 SVE | SVE + SVE2 (256-bit) | 新增 SVE2 |
| **取指/译码宽度** | 4-wide | **8-wide** | 翻倍 |
| **ROB** | ~96 | **192** | 翻倍 |
| **SMT** | 无 | **SMT2**（2 线程/核） | 新增 |
| **FP/SIMD 吞吐** | 2/cycle (128-bit) | **4/cycle (128-bit)** 或 **2/cycle (256-bit)** | 翻倍 |
| **L1 I-Cache** | 64 KB | **128 KB** | 翻倍 |
| **L2 Cache** | 512 KB/core, 10-way | **1 MB/core**, 8-way | 容量翻倍 |
| **L3 Cache** | Ring Bus, Partition/Shared | **~23 MB/16c**, 19-way, 546MB 总计 | 大容量、高相联 |
| **Load/Store 带宽** | 2 Load 或 1L+1S/cycle | **3 Load + 2 Store/cycle** (3×32B 读 / 2×32B 写) | 大幅提升 |
| **BTB** | 64 entry | **Main BTB 12K** + Nano BTB（零周期） | 大幅提升 |
| **硬件预取器** | SEQ | BOF + SEQ + SMS + MOP + META（5 种） | 大幅增强 |
| **核心数** | 最多 64/芯片 | 192 核心 | — |
| **BFloat16** | 不支持 | 支持 (v8.6) | 新增 |
| **DotProd** | 仅高配 | 支持 | 新增 |
| **Mop Cache** | 无 | 支持 | 新增 |
| **NUMA** | 每 SCCL 1 节点 | 4 NUMA 节点（每节点 96 核） | — |

> 0xd03 在吞吐量、缓存、分支预测方面相对 TSV110 有显著提升。SVE2 的 predication 和 gather/scatter 为向量化提供了更多模式选择。

---

## 4. LC950 (Kunpeng-0xd06) 相对 0xd03 的差异

| 维度 | 0xd03 | LC950 (0xd06) | 说明 |
|------|-------------|-------------|------|
| **ISA 基线** | ARMv9-A | ARMv8-A (v9.2 兼容) | 基线不同，但均兼容 v9.2 |
| **SVE/SVE2** | 支持 | 支持 | 相同 |
| **流水线/缓存参数** | 相同 | 相同 | 取指 8-wide, ROB 192, L1I 128KB, L2 1MB, L3 ~23MB/16c |
| **`-march`** | `armv9-a` | `armv8.5-a` | ISA 基线影响 `-march` 选择 |
| **适用场景** | 通用计算、AI 推理 | 通用计算、AI 推理 | 性能特征基本相同 |

> LC950 与 0xd03 在微架构参数上几乎一致（相同流水线宽度、缓存层次、预取器种类），主要区别在于 ISA 基线版本，影响 `-march` 编译选项选择。当前源文档中两代均标注核心为 "0xd03"，实际产品定位通过 Part ID 区分。

---

## 5. 使用指南

### 5.1 何时查阅本文档

在 interactive-optimizer 分析过程中，以下场景直接查阅本文档，**无需派生子代理**：

- **识别 CPU 型号**：根据 `perf` 输出或 `/proc/cpuinfo` 确定 Part ID → 微架构 → 编译选项
- **评估向量化策略**：确认目标 CPU 是否支持 SVE/SVE2，避免在不支持 SVE 的 0xd01 基础版上生成 SIGILL 代码
- **缓存参数调优**：L1D/L2/L3 容量和延迟用于评估 working set 是否 fit、循环分块大小选择
- **端口压力分析**：判断热点是 ALU-bound、FP-bound 还是 Load/Store-bound
- **NUMA 感知**：确定是否需要显式 NUMA 绑定

### 5.2 快速决策树

```
检测到热点函数
  │
  ├─ 浮点密集？
  │   ├─ FP32？ → TSV110: 2 FMA/cycle, 注意 ADD/MUL 端口分离
  │   │          0xd03/0xd06: 4 FMA/cycle (128-bit), 无端口不对称
  │   └─ FP64？ → TSV110: 0.5 FMA/cycle (瓶颈), 考虑 mixed-precision
  │              0xd03/0xd06: 同上瓶颈
  │
  ├─ 向量化候选？
  │   ├─ 目标 CPU 是 0xd01 基础版？ → 仅 NEON 128-bit, 无 SVE
  │   ├─ 目标 CPU 是 0xd01 高配？   → NEON + SVE 256-bit, 无 SVE2
  │   └─ 目标 CPU 是 0xd03/0xd06？    → NEON + SVE + SVE2 256-bit
  │
  ├─ 访存密集？
  │   ├─ working set < 32KB？     → L1D fit, 最优
  │   ├─ working set < 512KB？    → 0xd01: L2 fit; 0xd03/0xd06: L2 fit (1MB)
  │   ├─ working set < 4MB？      → 0xd01: L3 Partition ~36c
  │   └─ working set > 4MB？      → 0xd01: L3 Shared >90c, 需分块
  │
  └─ 分支密集？
      ├─ 0xd01？ → BTB 64 entry, 间接分支 ~256 目标, 考虑 branchless
      └─ 0xd03/0xd06？ → BTB 12K + Nano BTB, 分支预测能力强, 但仍避免不可预测分支
```

### 5.3 与其他参考文档的关系

| 场景 | 查阅文档 |
|------|---------|
| 需要完整微架构细节（指令延迟表、竞品对比等） | `pipeline/skills/kunpeng_microarch/kunpeng920-microarchitecture.md` |
| 需要 0xd03/0xd06 完整参数（预取器详情、架构扩展列表等） | `pipeline/skills/kunpeng_microarch/kunpeng_uarch_b-microarchitecture.md` / `kunpeng950-microarchitecture.md` |
| 需要编译选项推荐和 ISA 推导规则 | `pipeline/skills/compiler-flag-tuning/SKILL.md` |
| 需要指令级延迟/吞吐量精确数据 | `pipeline/skills/kunpeng_microarch/tsv110_full.json` (TSV110 only) |
| ARM NEON/SVE 指令查询 | 调用 `arm-instructions-query` skill |
| ARM SPE 微架构分析 | 调用 `arm-spe-analysis` skill |
