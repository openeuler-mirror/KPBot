---
name: oceanbase-performance-optimization
description: >
  OceanBase 性能优化（OS/部署 + 配置 + 编译 + 源码）：cgroup 绑 NUMA 与 1NPS、numa_distance/
  node_reclaim_distance、migratepages、grub（nohz/idle=poll/kpti）、nf_conntrack、64K 页、
  sockmap/SMC-D、uncore turbo、网卡中断与 VF、obproxy/sysbench 亲和；OB 配置（cpu/memory/
  net_thread_count/租户 Unit/freeze_trigger/memstore/限流/排序内存/审计）；编译（LLVM for
  openEuler、LSE/NOLSE、LTO/AutoFDO/HOTFUNC/BOLT/CSPGO、-mtune）；源码（ApplySrv 自适应
  线程、sql_nio-worker 多队列亲和、gettimeofday 消除、serialize/prefetch/false sharing
  尝试）及其实测影响、适用条件、风险与回滚。用于把热点证据转成单变量优化并验证；
  不修改通用 OS/BIOS/网卡方法（交给 kpbot-app-tuner 子技能），不做通用 C/C++ 微架构开发
  （交给 kpbot-code-optimizer）。触发词：OceanBase 优化、OB 调优、OB 参数、OB 源码优化、
  cgroup、numa_distance、migratepages、uncore turbo、LSE、NOLSE、LTO、ApplySrv、多队列、
  QPS 提升、insert 优化。
license: MulanPSL-2.0
compatibility: OceanBase CE 4.3.5 源码树 4.3.5_CE_BP5_HF2；鲲鹏 950/920B/7270Z、openEuler 24.03 / AntOS 6.6、AMD 9T34 实测；x86 方法通用但收益不同（LSE 指令计数/指令分布等二进制核验指标仅 AArch64 适用）
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"
---
# OceanBase 性能优化（OS/部署 + 配置 + 编译 + 源码）

## 简介

优化不是“把参数调大/把代码改快”，而是：

```
基线 → 瓶颈归类（hotspot）→ 选择唯一杠杆 → 单变量变更 → 正确性门禁 → 同口径 A/B → 保留/回滚
```

本 skill 提供四类杠杆：

1. **OS/部署**：cgroup/绑核、NUMA 与 1NPS、numa distance、migratepages、grub、网络、
64K 页、sockmap、uncore turbo、中断/VF。
2. **配置**：规格与租户、线程、内存/冻结/限流、排序内存、审计。
3. **编译/第三方**：LLVM for openEuler、LSE/NOLSE、LTO/AutoFDO/HOTFUNC/BOLT/CSPGO。
4. **源码**：ApplySrv、多队列亲和、gettimeofday、serialize/prefetch/cache line 等。

原则：**部署/OS 杠杆通常比源码杠杆大一个数量级；先把瓶颈定位对，再动手。**

## 何时触发

- 已通过 `oceanbase-hotspot-analysis` 找到瓶颈，需要把“热点”变成“可验证的改动”。
- 要调 cgroup/绑核/1NPS、NUMA 距离、migratepages、grub、sockmap、uncore turbo、中断/VF。
- 要调 `cpu_count`/`memory_limit`/`net_thread_count`/租户 Unit/冻结/限流/排序内存。
- 要切换 LLVM for openEuler、LSE/NOLSE、LTO/AutoFDO/HOTFUNC/BOLT。
- 要实现/评估 OB 源码优化（ApplySrv、多队列、gettimeofday、serialize）。
- 优化后出现负收益，需要判断是回滚、换杠杆还是归因错误。

**不适用**：OS/BIOS/网卡/CPU 亲和性的通用方法 → `kpbot-app-tuner` 的 `os-optimization` /
`bios-optimization` / `network-optimization` / `cpu-affinity-optimization`；
通用 C/C++ 微架构/向量化开发 → `kpbot-code-optimizer`；部署/编译本身 →
`oceanbase-build-deployment`；验证方法 → `oceanbase-test-verification`。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮。
2. **一次只改一个变量**：OS、参数、编译选项、源码补丁分开验证。
3. **正确性门禁**：功能冒烟/计数/事务回读不通过，直接否决，不看性能。
4. **分场景判定**：不能用 read 的微小提升掩盖 insert 的回退。
5. **收益 <5% 视为噪声**；负收益必须回滚并记录。
6. **记录身份**：二进制 md5/commit、编译参数、租户 Unit、绑核、数据集。
7. **高风险变更先备份/确认**：副本、数据格式、归档、升级、64K 内核。

## 工作流

| 步 | 动作 | 材料 |
| --- | --- | --- |
| 1 | 基线：功能 + 五场景/业务模型，多轮 | `oceanbase-test-verification` |
| 2 | 归因：确认瓶颈进程/线程/机制 | `oceanbase-hotspot-analysis` |
| 3 | 选杠杆：查决策矩阵与已证伪清单 | 本文 §决策矩阵；`references/decision-and-negative-results.md` |
| 4 | 实施：OS/配置/编译/补丁，只改一个变量 | `references/os-and-deploy-tuning.md`、`config-tuning.md`、`compiler-and-thirdparty.md`、`source-optimizations.md` |
| 5 | 正确性：冒烟/计数/事务回读 | `oceanbase-test-verification` |
| 6 | 性能：同口径 A/B，≥3 轮 | `oceanbase-test-verification` |
| 7 | 判定：有效/无效/劣化；保留或回滚 | 本文 §输出契约 |
| 8 | 记录：变更、证据、回滚命令、已证伪 | `references/decision-and-negative-results.md` |

## 决策矩阵（先看瓶颈，再选杠杆）

| 瓶颈证据（触发口径详见各 references） | 首选杠杆 | 次选 | 避免 |
| --- | --- | --- | --- |
| insert 落后、ApplySrv 单线程持续 ≥90% 单核，加 worker 不涨 | **ApplySrv 自适应线程** | 多队列叠加 | 只加 worker |
| 读/点查/更新偏慢，L3 miss 高 | **sql_nio-worker 多队列亲和** | 绑核/1NPS | 只调 worker 数 |
| 950 insert 跨 P、numa distance | **cgroup 绑 NUMA / 启动时 numactl** | 1NPS；migratepages | 启动后 taskset |
| 只写等 IO、盘延迟高 | clog/data 分盘、换盘、冻结错峰 | 内存盘仅诊断 | 盲调大 buffer |
| 128U CPU 未满、sql_nio 单线程饱和且队列增长 | `net_thread_count` 12→36 | 网卡队列/中断 | 只加租户 CPU |
| 排序/聚合慢 | `_sort_area_size`、索引/collation | PX | 归因到 OS |
| 绑核策略不优 | 950 绑核、920B 绑 NUMA | — | 一套策略套所有机型 |
| 锁争用明显 | 先确认锁占比，再考虑 LSE | 降并发 | 盲调自旋 |
| 低争用、LSE 版本偏慢 | **NOLSE 重编** | LTO/AutoFDO | 无基线换编译器 |
| 前端/icache 受限 | LLVM for openEuler + LTO/hotfunc/BOLT | — | 只调 `-mtune` |
| 全机整体偏低 | uncore turbo、grub、nf_conntrack、64K 页 | sockmap/SMC-D | 不验证就叠加 |

## OS/部署杠杆（详见 references/os-and-deploy-tuning.md）

头条实测（口径见该文件，跨机型不横比）：cgroup 绑 NUMA 平均 +10%（insert 跨 P 场景 +38%）、1NPS（950）读 +12.8%/写 +14.2%、uncore turbo +7~8%、nf_conntrack 卸载 +6.8%、64K 页 +5%、obproxy/sysbench 同 NUMA +8.4%（AMD 无收益）；其余（grub/migratepages/node_reclaim_distance/sockmap/SMC-D/VF 隔离）及各自条件/风险见表。

两条硬纪律：
- **绑核/绑内存必须启动时生效**（cgroup 或 `numactl -C -m`）：启动后 `taskset` 只改 CPU 不改内存、留跨 P 访存；容器化用 cgroup + VF 隔离（线性损耗约 5%）。
- sockmap 与 SMC-D 收益重叠且冲突，二选一实测后再定。

## 配置杠杆（详见 references/config-tuning.md）

核心项（典型值/实测/风险全表见该文件）：`cpu_count`/`memory_limit`/租户 `cpu_num` 按规格（32U→250G，128U 租户 CPU 可到 120/128）；`net_thread_count` 12→36 仅当「整机未满 + sql_nio 单线程饱和 + 队列增长」三证齐；`freeze_trigger_percentage`/`_memstore_limit_percentage` 为**租户级**临时杠杆（sys 侧改需 `TENANT=` 显式指定目标租户，事后必须恢复）；`writing_throttling_trigger_percentage` 调低=提前限流；`_sort_area_size` 按算子调；obproxy `work_thread_num` 128。

硬纪律：集群级 `memstore_limit_percentage` 仅 sys 可改且影响全部租户，不要当租户临时杠杆用；"+X%" 均为特定实测，不是承诺。

## 编译/第三方杠杆（详见 references/compiler-and-thirdparty.md）

头条实测：LLVM for openEuler（vs 开源 LLVM 11）920B +21.81%/950 +11.25%；BOLT 约 +3% 但开源 CE 树须显式 `-DOB_BUILD_OPENSOURCE=OFF` 否则门控恒假＝未生效（`--emit-relocs` 需要）；LSE/NOLSE 按争用强度二选一实测。其余（架构优化组合/内联/ICP/预取/GOT、tcmalloc/jemalloc 分配器、三方升级）的条件与风险见表；分配器二选一、全量三方升级无明确收益不做。

## 源码杠杆（详见 references/source-optimizations.md）

头条实测（§1-§4 详表）：ApplySrv 自适应线程 insert +12%（write 约 -3% 需复测、如实记录）；sql_nio-worker 多队列亲和 read +4.7%/select +4.6%/update +3.6%；gettimeofday 替换 update +1.4%。**serialize `idx=0` 牺牲正确性——拒绝**；false sharing/预取/减少 cluster 共享见负结果台账。

两条硬纪律：
- **干净重编**：补丁应用与回退两侧都从干净构建目录整树重编并核产物身份（OB 构建未启用 `--enable-depend` 类依赖跟踪，改头文件后直接 make 会复用旧对象——见 source-optimizations.md §9 检查单）。
- 原则：结构性去等待（线程/队列）> 去锁/去拷贝 > 指令级微优化；OB 热点分散（最大约 4%），不要期待单点指令优化大幅提升。

## 负结果与已证伪（详见 references/decision-and-negative-results.md）

已证伪项（全文表见该文件，动手前先查）：jemalloc/tcmalloc、CRC32 硬件指令、`-mtune=neoverse-n2`/`tsv110`（负收益）、`numa_balance=on`（920B 只写 -2.6%）、5.10 内核（劣化 7~8%）、锁效率微调、false sharing 对齐、数据预取、全局 proxy 编译优化（仅 ~1.7%）。任何负结果入台账，防重复试验。

## 风险与回滚

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| cgroup/绑核/1NPS/grub | 服务起不来、跨 P 变化 | 恢复启动命令/BIOS/grub，重启 |
| 冻结/限流/内存/线程 | OOM、写抖动、限流 | 恢复参数并重启/等待生效 |
| 编译参数/编译器 | 链接失败、ABI/指令集不兼容 | 回退 CMake flags/工具链，重编，恢复二进制 |
| 源码补丁 | 语义回归、崩溃 | `git apply -R`/恢复源码，重编，恢复二进制 |
| 内核（64K/PGO） | 无法启动、驱动不兼容 | 恢复原内核/grub |
| 备份/归档/升级 | 数据不可用 | 备份恢复；旧二进制+旧数据 |

高风险变更（副本、数据格式、归档、升级、内核、64K 页）必须先与用户确认并备份；
生产变更安排窗口。

## 输出契约

每轮优化产出：

1. **变更记录**：变量/文件/函数、改前→改后、编译参数、二进制 md5/commit。
2. **归因**：对应热点证据（进程/线程/函数/机制），不是“感觉慢”。
3. **正确性证据**：冒烟/计数/事务回读结果。
4. **A/B 结果**：分场景 TPS/QPS、成功率、avg/P95/P99、CPU 的逐轮明细与汇总，轮数与波动，口径。
5. **判定**：有效/无效/劣化；保留或回滚；回滚命令。
6. **已证伪**：负结果与原因，写入团队台账。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/os-and-deploy-tuning.md` | cgroup/绑核/1NPS/numa distance/migratepages/grub/网络/64K/sockmap/uncore/中断/VF：原理、实测、条件 |
| `references/config-tuning.md` | 规格/租户/线程/内存/冻结/限流/排序内存/审计/obproxy/sysbench：参数、实测、风险 |
| `references/compiler-and-thirdparty.md` | LLVM for openEuler、LSE/NOLSE、LTO/AutoFDO/HOTFUNC/BOLT/CSPGO、-mtune、第三方升级：参数与陷阱 |
| `references/source-optimizations.md` | ApplySrv、多队列、gettimeofday、serialize、prefetch、false sharing：位置、机制、实测、适用、风险 |
| `references/decision-and-negative-results.md` | 决策矩阵、负结果、反模式、停止条件、台账模板 |
| `scripts/verify_build_opts.sh` | 只读校验：编译器版本、LSE 指令、符号/段、md5/大小 |
