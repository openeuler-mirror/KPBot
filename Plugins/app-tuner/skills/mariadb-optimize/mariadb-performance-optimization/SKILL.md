---
name: mariadb-performance-optimization
description: >
  MariaDB 10.11.16（兼容 10.3.x 案例）性能优化：OS/虚拟化层（governor、THP/大页、
  dirty_ratio/swappiness、IO 调度、grub、网卡中断与绑核、NUMA）、部署层（实例/绑核/
  内存交错）、配置层（buffer pool/redo/flush_method/io_capacity/doublewrite/线程与表缓存/
  Aria/binlog 组提交/优化器开关）、编译与三方库（LSE、jemalloc、tcmalloc、-march、LTO/PGO）、
  源码层（伪共享 padding、顺序锁、lock_word 拆分、NUMA interleave node mask、cache-miss）
  及其实测影响、适用条件、版本差异、风险与回滚。用于把热点证据转成单变量优化并验证；
  不修改 OS/BIOS/网卡通用项（交给 kpbot-app-tuner 子技能），不做通用 C/C++ 微架构开发
  （交给 kpbot-code-optimizer）。触发词：MariaDB 优化、MariaDB 调优、MariaDB 参数、
  buffer pool、redo、io_capacity、LSE、jemalloc、tcmalloc、伪共享、顺序锁、NUMA、
  QPS 提升、TPS 提升。
license: MulanPSL-2.0
compatibility: MariaDB 10.11.16；10.3.x + 鲲鹏 4P / KVM 实测案例；aarch64 与 x86_64 方法通用但收益不同
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"
---
# MariaDB 性能优化（OS / 配置 / 编译 / 源码）

## 简介

优化不是“把参数调大/把代码改快”，而是：

```
基线 → 瓶颈归类（hotspot）→ 选择唯一杠杆 → 单变量变更 → 正确性门禁 → 同口径 A/B → 保留/回滚
```

本 skill 提供四类杠杆，按投入产出排序：

1. **OS/虚拟化**：governor、THP/大页、dirty_ratio、swappiness、调度器、绑核、NUMA、
网卡中断、grub 参数。
2. **部署/配置**：实例数、buffer pool、redo、刷盘、IO 容量、双写、线程/表缓存、
Aria、binlog 组提交、优化器开关。
3. **编译/三方库**：`-march`、LSE、jemalloc、tcmalloc、LTO/PGO、三方升级。
4. **源码**：伪共享、顺序锁、lock_word 拆分、NUMA interleave mask、cache-miss 等。

原则：**OS/配置杠杆通常比源码杠杆大一个数量级；先把瓶颈定位对，再动手。**

## 何时触发

- 已通过 `mariadb-hotspot-analysis` 找到瓶颈，需要把“热点”变成“可验证的改动”。
- 要调 buffer pool/redo/刷盘/IO/线程/表缓存/Aria/binlog/优化器。
- 要做 NUMA 绑核、大页、CPU 频率、IO 调度、虚拟化绑核。
- 要切换 LSE/jemalloc/tcmalloc、评估 `-march`/LTO/PGO、升级三方库。
- 要实现/评估 MariaDB/InnoDB 源码优化（伪共享、锁结构、NUMA、拷贝）。
- 优化后出现负收益，需要判断是回滚、换杠杆还是归因错误。

**不适用**：OS/BIOS/网卡/CPU 亲和性等**通用**项 → `kpbot-app-tuner` 的
`os-optimization`/`bios-optimization`/`network-optimization`/`cpu-affinity-optimization`/
`io-optimization`；通用 C/C++ 微架构开发 → `kpbot-code-optimizer`；
MyRocks/RocksDB 自身 → `rocksdb-perf-diagnostics`；部署/编译本身 →
`mariadb-build-deployment`；验证方法 → `mariadb-test-verification`。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮，波动 ≤5%。
2. **一次只改一个变量**：OS、配置、编译、源码分开验证。
3. **正确性门禁**：mtr/冒烟/对账/复制/备份不通过，直接否决，不看性能。
4. **分场景判定**：不能用只读的提升掩盖只写/TPC-C 的回退。
5. **收益 <5% 视为噪声**；负收益必须回滚并记录。
6. **记录身份**：二进制 md5/commit、编译参数、数据集、部署/绑核、关键参数快照。
7. **高风险变更先备份/确认**：redo、双写、持久性参数、数据格式、三方升级，以及 OS/主机级
   变更（grub/重启、THP、`drop_caches`、sysctl、irqbalance、systemd 单元覆写/服务重启）——
   按 `kpbot-app-tuner/references/user-interaction-gates.md` 的 `approved_execute` +
   `change_scope` 逐项确认，**Agent 不得自动重启主机**；门控细则见
   `references/os-and-deployment-tuning.md` §0。

## 工作流

| 步 | 动作 | 材料 |
| --- | --- | --- |
| 1 | 基线：功能 + 场景性能，多轮 | `mariadb-test-verification` |
| 2 | 归因：确认线程/机制/证据 | `mariadb-hotspot-analysis` |
| 3 | 选杠杆：查决策矩阵与已证伪清单 | 本文 §决策矩阵；`references/decision-and-negative-results.md` |
| 4 | 实施：OS/配置/编译/补丁，只改一个变量 | `references/os-and-deployment-tuning.md`、`config-tuning.md`、`compiler-thirdparty-and-source.md` |
| 5 | 正确性：mtr/冒烟/对账/复制/备份 | `mariadb-test-verification` |
| 6 | 性能：同口径 A/B，≥3 轮 | `mariadb-test-verification` |
| 7 | 判定：有效/无效/劣化；保留或回滚 | 本文 §输出契约 |
| 8 | 记录：变更、证据、回滚命令、已证伪 | `references/decision-and-negative-results.md` |

## 决策矩阵与杠杆（收敛到 references，防双份漂移）

完整决策矩阵与各杠杆表都在 references（此处只做路由，不复制表格——两处维护必然漂移）：

| 瓶颈证据形态 | 先读 |
| --- | --- |
| 物理读高/命中率低、`Innodb_log_waits`、脏页/checkpoint、buffer pool/redo/刷脏/`innodb_flush_method`（10.11 默认 `O_DIRECT`） | `references/config-tuning.md`（参数—现象对照表与风险列） |
| 行锁/latch/futex/MDL/临时表、线程池与连接模型 | `references/config-tuning.md` §锁 与 `references/decision-and-negative-results.md` 决策矩阵 |
| OS/governor/大页/IO 调度/绑核/虚拟化 | `references/os-and-deployment-tuning.md` |
| 编译选项（LSE/march）、分配器（jemalloc/tcmalloc）、三方库 | `references/compiler-thirdparty-and-source.md` §编译 |
| 源码杠杆（伪共享/顺序锁/lock_word 拆分/NUMA mask）与 10.11 适用性 | `references/compiler-thirdparty-and-source.md` §源码 |
| 负结果/已证伪/反模式/停止条件 | `references/decision-and-negative-results.md` |

选杠杆原则：**结构性去争用/去拷贝 > 锁算法微调 > 指令级微优化**；先配置/绑核/索引，再编译，最后源码；任何变更前先查负结果台账（已证伪项勿重复踩坑）。

两条硬纪律（详表见对应 references）：
- **绑核/内存策略必须写进 systemd 服务单元**（`CPUAffinity`/`NUMAPolicy` drop-in）：只在 `systemctl start` 前加 `numactl`/`taskset` 只约束 systemctl 客户端，mariadbd 不会继承——A/B 会把未绑核的一轮当成绑核成功；改后用 `MainPID` 核对（`references/os-and-deployment-tuning.md` §5）。
- **10.11 已移除 `innodb_buffer_pool_instances`/`innodb_page_cleaners`**（仅 10.5.1 及更早功能版本；10.5.2 起弃用并忽略，10.6.0 起移除）：写入 my.cnf 会因未知配置项启动失败；`scripts/verify_build_opts.sh` 会把"未知变量（版本不支持）"与"查询失败"分开报告（`references/config-tuning.md`）。

> OS/主机级命令的完整门控见 `references/os-and-deployment-tuning.md` §0；通用 OS 变更的
> 清单、门控与回滚仍走 `kpbot-app-tuner` 的 `os-optimization`（已带确认门控），不要绕过
> 其授权流程直接照 OS 手册执行。`drop_caches` 仅专用压测机可用，生产禁止。

## 负结果与已证伪（详见 references/decision-and-negative-results.md）

| 尝试 | 结果 | 原因 |
| --- | --- | --- |
| 在所有场景默认开 LSE | 部分场景退化 | 原子竞争不是唯一瓶颈，指令/功耗差异 |
| 同时链接 jemalloc + LD_PRELOAD tcmalloc | 行为不确定 | 分配器冲突 |
| 直接把 10.3 的 lock_word/顺序锁补丁套到 10.11 | 编译失败/无效 | 锁实现已重写为 `std::atomic` |
| 只加大 buffer pool 解决写抖动 | 无效或更差 | 瓶颈在 redo/刷脏，不在读缓存 |
| `Innodb_log_waits` 高就直接扩 redo | 无效、抖动仍在 | log buffer 太小或刷出滞后才是原因；先查 `innodb_log_buffer_size` 与刷出速率 |
| 10.11 my.cnf 照抄 `innodb_buffer_pool_instances`/`innodb_page_cleaners` | 服务无法启动（未知配置项） | 两项仅 10.5.1 及更早的功能版本支持（10.5.2 起弃用并忽略，10.6.0 起移除） |
| 不重置数据反复压测 | “提升”虚高 | 数据分布/缓存变化 |
| 多变量一起改 | 无法归因 | 违反单变量纪律 |

任何负结果都要记录，避免团队重复试验。

## 风险与回滚

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| OS/grub/绑核 | 重启失效/性能回退/无法启动 | 恢复 grub 备份、重启、恢复绑核脚本 |
| 大页/THP | 启动失败、内存不足 | 取消 `large_pages`，恢复 THP 设置 |
| 缓存/redo/刷盘 | OOM、写长尾、恢复窗口变长 | 恢复 my.cnf 并重启/在线调回 |
| 持久性参数 | 丢数据风险 | 立即恢复 1/1，并核对数据 |
| 编译参数 | 非法指令、链接失败 | 回退 flags，重编，恢复二进制 |
| 三方库/分配器 | ABI/崩溃/性能退化 | 恢复旧二进制与库 |
| 源码补丁 | 语义回归、崩溃 | `git apply -R`，重编，恢复二进制 |

高风险变更（持久性、双写、redo、数据格式、三方升级）必须先与用户确认并备份；
生产变更安排窗口。

## 输出契约

每轮优化产出：

1. **变更记录**：变量/文件/函数、改前→改后、编译参数、二进制 md5/commit。
2. **归因**：对应热点证据（线程/函数/机制/计数器），不是“感觉慢”。
3. **正确性证据**：mtr/冒烟/对账/复制/备份结果。
4. **A/B 结果**：分场景 TPS/QPS、错误率、p50/p99、CPU、IO 的逐轮明细与汇总，
轮数与波动、口径。
5. **判定**：有效/无效/劣化；保留或回滚；回滚命令。
6. **已证伪**：负结果与原因，写入团队台账。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/os-and-deployment-tuning.md` | governor/THP/大页/dirty/swappiness/调度器/绑核/NUMA/虚拟化/网卡中断/grub |
| `references/config-tuning.md` | buffer pool/redo/刷盘/IO/双写/线程/表缓存/Aria/binlog/优化器：取值、影响、条件 |
| `references/compiler-thirdparty-and-source.md` | LSE/jemalloc/tcmalloc/march/LTO/PGO/三方升级与源码补丁：位置、机制、适用、风险 |
| `references/decision-and-negative-results.md` | 决策矩阵、负结果、反模式、停止条件、台账模板 |
| `scripts/verify_build_opts.sh` | 只读校验：版本、md5、分配器、LSE 指令、关键参数 |

### `verify_build_opts.sh` 变量表

| 变量 | 含义 |
| --- | --- |
| `ARGS` | 客户端连接参数；`--socket` 指定目标实例 |
| `Q_OUT` / `Q_ERR` / `Q_RC` | 查询结果、查询错误、退出码；错误不冒充空值 |
| `DB_OK` | 指定连接是否可查询 `SELECT 1` |
| `VVAR` | `qvar` 读到的系统变量值，查询失败时不赋值 |
| `TARGET_PID` / `MULTI_INSTANCE` | 从连接确认的服务端 PID / 是否多实例；PID 不确定时跳过运行期检查 |
| `VSERVER` | 从 `--version` 的 `Distrib` 字段提取的服务端版本（按主.次版本核对） |
| `DIS` / `CAS` | 反汇编临时文件 / LSE 助记符（含内存序后缀）命中数；objdump 失败不算零命中 |
