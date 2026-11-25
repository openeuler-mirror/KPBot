---
name: nebula-performance-optimization
description: >
  NebulaGraph 3.8.0 性能优化（配置 + 源码）：部署拓扑/实例数/绑核/分区、线程与队列、
  RocksDB Block Cache/写缓冲/WAL/compaction、optimizer/MemoryTracker/日志、NUMA；
  编译与第三方层（LTO、LSE、jemalloc 5.3.1、F14 ABI、第三方升级的收益与代价）；
  源码层（Value SSO、BFS flat_map、SpookyHash、迭代器复用、schema 缓存、拷贝消除、
  prefetch、aarch64 TSC、UNLIKELY）及其实测影响、适用条件、风险和回滚。
  用于把热点证据转成单变量优化并验证；不修改 OS/BIOS/网卡（交给 kpbot-app-tuner 子技能），
  不做通用 C/C++ 微架构开发（交给 kpbot-code-optimizer）。触发词：Nebula 优化、Nebula 调优、
  Nebula 参数、Nebula 源码优化、LTO、LSE、jemalloc、Block Cache、WAL、compaction、
  Value SSO、BFS、flat_map、schema 缓存、QPS 提升。
license: MulanPSL-2.0
compatibility: NebulaGraph 3.8.0；鲲鹏 950 / aarch64、openEuler 24.03、GCC 12.3.1 实测；x86 方法通用但收益不同
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: graph_database
  version: "1.0.0"
---

# NebulaGraph 性能优化（配置 + 源码）

## 简介

优化不是“把参数调大/把代码改快”，而是：

```
基线 → 瓶颈归类（hotspot）→ 选择唯一杠杆 → 单变量变更 → 正确性门禁 → 同口径 A/B → 保留/回滚
```

本 skill 提供三类杠杆：

1. **部署/配置**：实例数、绑核、分区、线程、缓存、WAL、compaction、日志、NUMA。
2. **编译/第三方**：LTO、LSE、jemalloc、third-party 升级。
3. **源码**：Value/容器/哈希/迭代器/schema/拷贝/预取/时间戳等 Nebula 特有改动。

原则：**部署/配置杠杆通常比源码杠杆大一个数量级；先把瓶颈定位对，再动手。**

## 何时触发

- 已通过 `nebula-hotspot-analysis` 找到瓶颈，需要把“热点”变成“可验证的改动”。
- 要调 graphd/storaged 线程、block cache、WAL、compaction、optimizer、日志、NUMA 绑核。
- 要切换 LTO/LSE/jemalloc、评估第三方升级。
- 要实现/评估 Nebula 源码优化（SSO、flat_map、哈希、迭代器复用、schema 缓存等）。
- 优化后出现负收益，需要判断是回滚、换杠杆还是归因错误。

**不适用**：OS/BIOS/网卡/CPU 亲和性等系统层 → `kpbot-app-tuner` 的 `os-optimization` /
`bios-optimization` / `network-optimization` / `cpu-affinity-optimization`；
通用 C/C++ 微架构/向量化开发 → `kpbot-code-optimizer`；部署/编译本身 →
`nebula-build-deployment`；验证方法 → `nebula-test-verification`。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮。
2. **一次只改一个变量**：参数、编译选项、源码补丁分开验证。
3. **正确性门禁**：功能冒烟/路径比对/计数对账不通过，直接否决，不看性能。
4. **分场景判定**：不能用 Go1 的微小提升掩盖 Go3 的回退。
5. **收益 <5% 视为噪声**；负收益必须回滚并记录。
6. **记录身份**：二进制 md5/commit、编译参数、数据集、部署/绑核。
7. **高风险变更先备份/确认**：WAL、副本、数据格式、第三方升级。

## 工作流

| 步 | 动作 | 材料 |
|---|------|------|
| 1 | 基线：功能 + 五场景性能，多轮 | `nebula-test-verification` |
| 2 | 归因：确认瓶颈进程/线程/机制 | `nebula-hotspot-analysis` |
| 3 | 选杠杆：查决策矩阵与已证伪清单 | 本文 §决策矩阵；`references/decision-and-negative-results.md` |
| 4 | 实施：配置/编译/补丁，只改一个变量 | `references/config-tuning.md`、`compiler-and-thirdparty.md`、`source-optimizations.md` |
| 5 | 正确性：冒烟/对账/路径比对/写回读 | `nebula-test-verification` |
| 6 | 性能：同口径 A/B，≥3 轮 | `nebula-test-verification` |
| 7 | 判定：有效/无效/劣化；保留或回滚 | 本文 §输出契约 |
| 8 | 记录：变更、证据、回滚命令、已证伪 | `references/decision-and-negative-results.md` |

## 决策矩阵（先看瓶颈，再选杠杆）

| 瓶颈证据 | 首选杠杆 | 次选 | 避免 |
|----------|---------|------|------|
| graphd futex/队列争用高（Go1） | 增加 graphd 实例；降低单实例 worker | 减少每查询队列操作 | 继续加 worker |
| storaged RocksDB 扫描高（Go2/3） | 增加 storaged/读池；RocksDB cache/选项 | Key/索引/查询改写 | 只加 graphd |
| graphd BFS/Value 字符串（FSP） | BFS 容器改造；拷贝消除；减少字符串分配 | 预分配/去重容器 | 加 worker |
| 内存分配/释放高 | 对象池、减少拷贝、SSO、flat_map | 换分配器（需验证） | 无脑调大 cache |
| nGQL 解析高（Insert） | 批量/预编译/减少语句复杂度 | 解析层优化 | 加 CPU |
| WAL fsync/compaction（Insert） | WAL/compaction/磁盘/批量 | RocksDB 选项 | 加 worker |
| 多跳 RPC 等待 | 减少跳数/扇出、批量 RPC、结果裁剪 | 增加 storaged | 只加 VU |
| 混合负载利用率低 | 浮动部署 + 多实例 | 按场景切换绑核 | 单一静态绑核 |
| 读路径整体慢 | LSE/LTO/jemalloc/缓存 | 源码热路径 | 无基线换第三方 |

## 配置杠杆（详见 references/config-tuning.md）

| 杠杆 | 典型值 | 实测/经验 | 风险 |
|------|--------|----------|------|
| graphd 实例数 | 2→4/8 | Go1 QPS +44%（组合配置：实例 2→4 且 worker 48→16；归因实例数需 worker 口径固定的对照）；storaged 成新瓶颈 | 端口/内存/缓存副本 |
| graphd worker | 48→16/24 | 降低单队列 futex；但过低会排队 | 并发不足 |
| storaged 数量 | 2→4 | Go2/3 分摊扫描；深查询稳定性提升 | 磁盘/内存/RPC |
| 绑核 | 固定 vs 浮动 | 点查绑核更快；混合负载浮动利用率高 | 场景切换失效 |
| `rocksdb_block_cache` | 8–32GB | 覆盖工作集后收益递减；过大 LRU 开销 | 内存水位 |
| `disable_page_cache` | true | 避免双重缓存；需 O_DIRECT | 小 IO 可能变慢 |
| `write_buffer_size` / `max_write_buffer_number`（ColumnFamily 级，经 `rocksdb_column_family_options` JSON，非 flagfile 顶层） | 64–256MB / 4 | 减少 flush；过大会增加恢复/compaction 压力 | 写入长尾 |
| `wal_ttl` | 60（只读）/ 14400（生产） | 只读压测减少 WAL 干扰 | 恢复窗口 |
| `enable_optimizer` | 简单 GO 可关 | 实测 Go1 +2.6%（关闭） | 复杂查询变慢 |
| `ENABLE_MEMORY_TRACKER`（CMake 构建选项，需 -DOFF 重建，非运行开关） | OFF | 实测 +2.5% | 内存问题难定位 |
| `minloglevel` | 3 | 实测 +0.6% | 丢失诊断日志 |
| `numa_balancing` | 0（压测） | 减少迁移抖动 | 需固定绑核 |

> 参数收益高度依赖数据/部署/版本；“+X%”是特定实测，不是承诺。

## 编译/第三方杠杆（详见 references/compiler-and-thirdparty.md）

| 杠杆 | 实测 | 条件/风险 |
|------|------|----------|
| `-march=armv8-a+lse`（Nebula + third-party） | FSP +17.4%；其他场景噪声内 | 消除 outline atomics；CPU 需支持 LSE |
| LTO（`-flto=auto`） | 小幅/场景相关；二进制略小 | 编译时间增加；需整体一致 |
| jemalloc 5.3.1（静态 + LSE） | 读场景有收益，写场景视版本 | 链接顺序、ABI、F14 |
| 全量第三方升级 2021→2025 | 读路径净贡献≈0；写路径受 RocksDB 影响大 | 数据格式/API/ABI/周期 |
| `-mtune=neoverse-n2` | Go1 -2.5% | 回退 |
| `-mtune=tsv110` | 全场景 -1%~-3.2% | 回退 |
| third-party `-O3` | 无显著收益 | 回退 |
| 手写 NEON（字符串/TSC） | Go1 +0.4%；整体噪声内 | 收益取决于 VID 类型与热点占比 |

结论：编译杠杆先做 LSE（收益明确、风险可控），LTO/jemalloc 按场景验证；第三方全量升级
没有明确收益时不要做。

## 源码杠杆（详见 references/source-optimizations.md）

| 优化 | 机制 | 实测 | 适用 |
|------|------|------|------|
| Value SSO（`unique_ptr<string>`→内联 `string`） | 短字符串零堆分配 | FSP +11~13% | 字符串 VID/属性多 |
| BFS flat_map（`unordered_multimap`→`robin_hood::unordered_flat_map`） | 同 key 边连续、开放寻址 | FSP +6%（叠加后 +15%） | FSP/路径 |
| Edge 移动/拷贝消除 | 避免 `std::move(const&)` 退化为拷贝 | FSP 叠加收益 | BFS/路径 |
| 删除 BFS 去重容器 | 用空 vector 判断新 dst | FSP 叠加 +6% | BFS |
| SpookyHash/分块哈希 | 替代逐字节 FNV；去掉 toString | 读场景收益 | Value/Edge/Vertex 哈希 |
| 迭代器复用（成员化 + reset） | 每顶点 `new` 迭代器→复用 | Go2/3 收益 | storaged 读 |
| schema 列名索引缓存 | 跨响应共享列索引 | 减少每响应构建 | GetNeighborsIter |
| RowReader schema 版本缓存 | 免每行双重解析 | Go2/3 +0.5% | 行解码 |
| NEON null/VID 比较 | 16B 一次比较 | 取决于 VID 类型，常为噪声 | FIXED_STRING |
| aarch64 `cntvct_el0` 时间戳 | 替代 `clock_gettime` | Go1 +0.4% | 高频计时 |
| prefetch | 预取边属性 | 边际 | 规律访存 |

原则：**结构性去分配/去哈希 > 微调拷贝 > 指令级微优化**。先做容器/拷贝/缓存，再做 NEON/预取。

## 负结果与已证伪（详见 references/decision-and-negative-results.md）

| 尝试 | 结果 | 原因 |
|------|------|------|
| `-mtune=neoverse-n2` / `tsv110` | 负收益 | 热点在 futex/内存，调度优化无益 |
| 关闭 `query_concurrently` | -0.8% | 单跳无收益，增加调度 |
| `wal_sync=false`（3.8.0 上游默认即 false，实验中 true 为显式配置） | Insert -23.6%（对照 true） | compaction 开销增加 |
| `rocksdb write_buffer=256MB` | Insert -25.4% | 大 buffer 增加 flush 延迟 |
| schema 哈希表返回引用 | 未实施 | 虚函数接口影响 10+ 文件，风险过大 |
| NEON 比较 bug（vmaxvq 判等） | QPS 虚高 +126% | 误判不同 VID 相等，少做工作；正确性否决 |
| 全量第三方升级 | 读路径≈0 | 收益主要来自 Nebula 补丁 |

任何负结果都要记录，避免团队重复试验。

## 风险与回滚

| 变更 | 风险 | 回滚 |
|------|------|------|
| 实例/绑核/端口 | 服务中断、端口冲突 | 恢复启动命令/配置，按顺序重启 |
| 线程/缓存/WAL | 内存/OOM、写入长尾、恢复窗口 | 恢复配置并重启/热加载 |
| 编译参数 | 链接失败、ABI 不兼容 | 回退 CMake flags，重编，恢复二进制 |
| 第三方升级 | 数据格式、API/ABI、长周期 | 旧 build/二进制 + 数据备份 |
| 源码补丁 | 语义回归、崩溃 | `git apply -R`/恢复源码，重编，恢复二进制 |

高风险变更（WAL、副本、数据格式、第三方升级）必须先与用户确认并备份；生产变更安排窗口。

## 输出契约

每轮优化产出：

1. **变更记录**：变量/文件/函数、改前→改后、编译参数、二进制 md5/commit。
2. **归因**：对应热点证据（进程/线程/函数/机制），不是“感觉慢”。
3. **正确性证据**：冒烟/对账/路径比对/写回读结果。
4. **A/B 结果**：分场景 QPS/成功率/p50/p99/CPU 的逐轮明细与汇总（每场景逐轮结果随证据包归档，便于复核验收结论），轮数与波动，口径。
5. **判定**：有效/无效/劣化；保留或回滚；回滚命令。
6. **已证伪**：负结果与原因，写入团队台账。

## 参考资料与脚本

| 文件 | 何时读 |
|------|--------|
| `references/config-tuning.md` | 调实例/线程/缓存/WAL/compaction/optimizer/日志/绑核：参数、实测影响、条件 |
| `references/compiler-and-thirdparty.md` | LTO/LSE/jemalloc/F14/第三方升级：构建参数、验证、收益与陷阱 |
| `references/source-optimizations.md` | 源码补丁：位置、机制、代码要点、实测、适用条件、风险 |
| `references/decision-and-negative-results.md` | 决策矩阵、负结果、反模式、停止条件、台账模板 |
| `scripts/verify_build_opts.sh` | 只读校验：二进制库版本、LSE 指令数、关键符号、md5/大小 |
