# 带宽杠杆库

> 适用前提：已经用 `memory -m 3`（PMU，DDR 实际带宽）实测，且 DDR 带宽**接近平台上限**
> （平台上限以 `memory -m 3` 本机实测标定）——只有这时才叫带宽受限，第二节
> 的杠杆才生效。
>
> **核心警告**：本项目所有哈希表（cuckoo / F14 / swiss / folly-CHM）的实测中，**带宽轴从未
> 真正打满**。动任何布局类优化之前，必须先证伪"带宽受限"假设——"看起来饱和"经常是
> MSHR/延迟受限的伪装（见第一节）。历史测量中最高 DDR 读流量（cuckoo 满并发）
> 出现时，该表 top-down 仍报 DRAM Bound ~0%——
> 高流量 ≠ 带宽墙：流量大是吞吐高的结果，不是限制吞吐的原因。

## 一、带宽受限 vs 延迟受限 vs MLP 不足（三分判据）

### 1.1 理论框架：Little's law 与 MSHR

并发度上限 = 带宽 × 延迟；吞吐 = 并发度 / 延迟，受 MSHR 条目数 + LLC→DDR 往返延迟约束。
**outstanding miss 总数一旦超过共享 LLC→DDR 路径的并发处理槽位（MSHR），
吞吐即饱和——这与 DDR 带宽是否打满无关**。因此"多线程扩展早饱和 + DDR 流量不高"的第一
嫌疑人永远是 MSHR/延迟，不是带宽。

MLP 机制佐证（预取 = 延迟隐藏，不是省流量）：folly-CHM SIMD 后端 PD=8 预取在 100M
（cache 全失效）下，DRAM 访问数不变（L3D refill/query 5.56→5.72，+2.8%）、单次 DRAM
有效延迟 −36%（stall/l3d_refill 73.5→47.0 cyc）、DDR 读带宽 +22%、
IPC +106%（0.33→0.68）——预取让多个 DRAM miss 并发在途（MLP），DRAM 流量反而上升。
同实验 10M 档的 L3D refill −21.6% 是 cache 边缘效应假象，
不能作为"预取省带宽"的证据。

### 1.2 三个实测案例（全部判为"非带宽墙"）

**案例 1｜F14 packed（ValueMap）：MSHR/延迟受限**

| 证据 | 数值 |
|---|---|
| DDR 读带宽（系统级含污染，量级旁证） | ≈ 实测平台上限的一半量级 |
| B packed 扩展曲线 | ~4 线程即饱和（4T 2.32×），满并发仅 **3.27×**（41.6M）；A vector 约 12 线程饱和（6.73×） |
| B 的 1T 吞吐 | 12.7M，为 A（3.97M）的 3.2× → 少数线程的聚合 miss 率先耗尽共享并发槽位 |
| 对照组 cuckoo packed | 同框架 24T 318M，≈24× 近线性（ring 深流水线 + locality=3 预取化随机 LLC miss 为预取命中，推迟 MSHR 饱和点） |

判定：outstanding-miss 并发处理槽位（MSHR/延迟）耗尽，而非带宽墙。
正确杠杆 = chunk 深流水线批量预取，而不是压缩流量。（注意：早期"非锁"结论已被后续
实测更正为读锁争用是查询主因——锁与带宽-vs-MSHR
判定正交，但引用本案例时勿再以"非锁"为前提。）

**案例 2｜swiss（absl flat_hash_map）：延迟 + value-load MLP 不足**

| 证据 | 数值 |
|---|---|
| 24T DDR 读带宽 | 满并发未达平台上限 |
| 24T L3 read hit rate | **12.88%**（≈87% L3 读落 DRAM），L1 hit 92% |
| top-down | 1T Backend 86.6%→L3 Bound 73.4%、IPC 0.94；24T Backend 93.9%→L3 Bound 87.2%、IPC 0.36 |
| footprint | 10M ×（32B slot + 128B 堆 value）≈ 1.6GB ≫ LLC；value 经 slot 内指针堆间接，ring 只预取 control+slot 不预取 value 行，每次命中卡 cold DRAM load |

判定依据（非墙证明）：packed 把 footprint 缩到 ~160MB（vs 1.6GB）回 L3 后，满并发从 40.6M
升到 94.6M（2.4×）——限制器是 footprint→L3-miss→DRAM 延迟 + value-load MLP 不足，不是
带宽墙。value ablation 进一步支持：vector128 基线 1T 5.55M /
24T 39.95M；vector16（16B 堆）1T 8.08M（+46%）；packed 8B 内联 1T 19.29M（+248%）/
24T 94.64M（+137%）——vector16 与 packed 8B 的 2.4× 差距纯来自 pointer-chase 而非字节数。
另一个"非带宽"旁证：shard 数 ×16（256→4096）后锁
争用降 ~8pp 但 QPS 持平——释放的周期立即被 value DRAM stall 吸收，说明绑定约束在延迟侧。

**案例 3｜cuckoo：纯 L3 延迟，DRAM 侧 ~0%**

| 状态 | Backend / Memory / L3 Bound | DRAM Bound | IPC |
|---|---|---|---|
| V0 基线 24T | 87% / 82% / 71% | **~0%** | 0.25 |
| 全优化态 24T | 71.3% / 63.3% / 62.9% | **0.00%** | 0.87 |

约 90% 周期停在随机桶访问的 LLC cold miss 上；预取把 DRAM 延迟掩盖后
残余瓶颈仍是 L3 访问延迟——"DRAM 带宽不是瓶颈"。

### 1.3 三分判定表

| 观测组合 | 判定 | 去向 |
|---|---|---|
| DDR 带宽占比高（接近上限）+ miss 延迟已被预取隐藏 | **带宽受限** | 本文第二节 |
| DDR 流量低 + L3/DRAM miss 延迟主导（Backend/Memory/L3 Bound 高企） | **延迟受限** | latency-levers：预取、间接层消除、SoA、value 内联 |
| DDR 占比中等 + 多线程扩展早饱和 + 锁开销低 | **MLP/MSHR 受限** | 深流水线批量预取，抬并发在途 miss 数（同 latency-levers 方向） |

## 二、带宽杠杆清单（仅在确认带宽受限后使用）

| # | 杠杆 | 机理 | 代价 / 边界 | 实证 |
|---|---|---|---|---|
| 1 | value 压缩 / 紧凑编码 | 缩小 value 载荷直接降每 query 流量（embedding 场景可考虑低精度编码） | 改表示法，涉及序列化兼容 | cuckoo 优化态下"value 侧压缩"被列为唯一仍有显著空间的方向之一 |
| 2 | 桶塌缩 ≤4B 单 cache line（cuckoo Design A，`TFRA_CUCKOO_SOA_SPLIT`） | 真正拆分 keys_/mapped_、无键冗余：value≤4B 时整桶 128B（2 line）→ **64B（1 line）**，L3 驻留减半，探测与命中取值同处一条已预取 line | ≥8B 时 A/B=0.98（A 略慢 ~2%，读 key 走 `value_proxy` 多一次间接抵消省内存收益），杠杆仅 ≤4B 成立 | sizeof/line 数 + 机制论证；当前 benchmark 无 ≤4B value 负载，**未做端到端 QPS 验证** |
| 3 | 冷热分离（SoA 键镜像，Design B） | 热探测数据（key 镜像/occupied_/seq_）收进首 line，冷 value 独立存放，缩热工作集 | footprint 不变（128B 桶），收益机制其实是探测局部性而非省带宽 | 24T QPS +31.1%（121.65M→159.51M）、LLC 读 miss −25%、L1D 访问 −29% |
| 4 | numactl --interleave 分摊多 node 带宽 | 数据交错分布到多 NUMA node，分摊 DDR 控制器带宽 | 跨 node 访问延迟上升，需权衡 | 定性建议，未单独 A/B 验证 |
| 5 | 负载因子调整 | 降负载因子缩短链长/探测序列、减少每 query 触碰行数 | 桶内存约 **+40%**；CHM 负载因子 1.05（SIMDTable ≈0.857）、平均链长 <0.6，收益边际 | **仅当结构优化（O1–O3）后仍受带宽限再做**（原文条件句）；cuckoo 侧 load factor/BFS 调优在正常 benchmark 无吞吐收益（±2% 噪声内） |

使用约束：杠杆 4、5 属边际收益级（预期 <5%）；布局类杠杆（1–3）
同时改变 L3 命中行为——落地后必须用 top-down + `memory -m 3` 复测，确认瓶颈确实移动而非
仅搬移了 miss 落点。

## 三、轴封闭判定（何时宣布带宽轴已尽）

### 3.1 封闭判据

**DDR 实测带宽 < 平台上限 70%，且已有预取覆盖** → 带宽杠杆全部无效，回到 latency-levers
继续找延迟/MLP 杠杆。此时压缩流量不会提升吞吐——瓶颈不在流量在等待。判据里"已有预取
覆盖"不可省略：无预取时 DDR 低只说明 MLP 不足，先补深流水线预取再重判。

### 3.2 本项目实证：cuckoo 的四轴封闭

cuckoo 全优化态 24T top-down：L3 Bound 62.9%、DRAM Bound 0.00%、DTLB 0.17%、Core
Bound 8.0%、IPC 0.87。据此封闭四轴——**TLB、DRAM 带宽、计算、分片
亲和**——唯一开放方向是**降 L3 working-set / 提升 L3 局部性**，指向 value 压缩与 ≤4B 桶塌缩。

支撑封闭的反证记录（每条都经过 A/B 实测）：

| 已证伪尝试 | 实测结果 |
|---|---|
| 双桶预取 | 24T Query P95 +8.4%（反向） |
| 桶内无分支标量比对 | clang 24T −8% |
| SVE 一条比 4 槽 | 24T −13%（66.0M→57.1M） |
| load factor / BFS 调优 | 无吞吐收益（±2% 噪声内） |
| 批量 hash 向量化 | 撤回（hash 已被预取流水线掩盖） |
| 分片亲和（模式 B） | 24T −11.7%（单表预取已覆盖 L3 延迟，分片只剩 collect 纯增开销） |

### 3.3 启示

轴封闭声明必须**写入瓶颈模型**（判定依据 + 数据 + 封闭时间），防止下游会话重复尝试已证伪
方向。cuckoo 的反证记录就是"负结果资产"：每条封死一条轴，后续优化预算才能集中到唯一
开放的降 L3 working-set 方向。同理，本库第一节的三个案例封死了"本表是带宽受限"这一假设
在 cuckoo/F14/swiss 上的默认成立——任何新表新场景仍需按 1.3 的判定表重新取证。
