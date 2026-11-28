---
name: mysql-performance-optimization
description: >
  MySQL 8.0 性能优化（配置 + 编译 + 源码）：按瓶颈证据选择唯一杠杆，覆盖部署/绑核、
  Buffer Pool/redo/binlog/IO/刷脏/线程/自旋/AHI、my.cnf 场景矩阵（Point/RO vs RW/WO、
  8U32G 20t/64t、2C8G/8C16G/32C256G 容器）、`-O3 -march=armv8-a+crypto+lse+crc`/LTO
  编译、NVMe 原子写，以及 fast-tuple、Read View version、space-skipping+NEON collation、
  binlog ordered、rec_get_offsets、binlog preallocate 等源码补丁；含决策矩阵、负结果、
  风险/回滚与 `verify_build_opts.sh`。OS/BIOS/中断/大页交给 mysql-os-optimization，
  my.cnf 落地交给 mysql-app-config-optimization，补丁实施交给 mysql-source-optimization。
  触发词：MySQL 优化、MySQL 调优、my.cnf、buffer pool、AHI、redo、binlog、group commit、
  LTO、LSE、原子写、fast-tuple、Read View、collation、源码优化、QPS 提升。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；鲲鹏 920/920B/920G、aarch64、openEuler 22.03/24.03、GCC 10.3/12.3 实测；x86 方法通用但收益不同
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"
---

# MySQL 性能优化（配置 + 编译 + 源码）

## 简介

优化不是“把参数调大/把代码改快”，而是：

```
基线 → 瓶颈归类（hotspot）→ 选择唯一杠杆 → 单变量变更 → 正确性门禁 → 同口径 A/B → 保留/回滚
```

本 skill 提供三类杠杆：

1. **部署/配置**：规格、绑核、线程、Buffer Pool、redo/binlog、刷脏、IO、AHI、自旋。
2. **编译/第三方**：`-O3`、`-march=...+lse+crc`、LTO、原子写、分配器（谨慎）。
3. **源码**：fast-tuple、Read View version、collation/space-skipping、binlog ordered、
`rec_get_offsets`、binlog preallocate 等 MySQL 专属改动。

原则：**先把瓶颈定位对，再动手；部署/配置杠杆通常比源码杠杆大一个数量级。**

## 何时触发

- 已通过 `mysql-hotspot-analysis` 找到瓶颈，需要把“热点”变成“可验证的改动”。
- 要调 my.cnf（buffer pool、redo、IO、刷脏、线程、AHI、自旋）。
- 要切换 `-O3`/LSE/LTO、评估 NVMe 原子写或第三方。
- 要实现/评估 MySQL 源码补丁。
- 优化后负收益，需要判断回滚、换杠杆还是归因错误。

**不适用**：OS/BIOS/IRQ/大页/sysctl → `mysql-os-optimization`（通用原理回到
`kpbot-app-tuner` 的 `os-optimization`/`bios-optimization`/`network-optimization`）；
my.cnf 渲染/场景矩阵落地 → `mysql-app-config-optimization`；补丁代码/构建 →
`mysql-source-optimization`；通用 C/C++ 微架构 → `kpbot-code-optimizer`；
部署/编译流程 → `mysql-build-deployment`；验证方法 → `mysql-test-verification`。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮。
2. **一次只改一个变量**：OS、参数、编译、补丁分开验证。
3. **正确性门禁**：MTR/冒烟/对账/回读不通过，直接否决，不看性能。
4. **分场景判定**：不能用 Point 的微小提升掩盖 RW 的回退。
5. **收益 <5% 视为噪声**；负收益必须回滚并记录。
6. **记录身份**：二进制 md5/commit、编译参数、数据集、部署/绑核、my.cnf。
7. **高风险变更先备份/确认**：durability、log file、数据格式、原子写、补丁。

## 工作流

| 步 | 动作 | 材料 |
| --- | --- | --- |
| 1 | 基线：功能 + 四场景性能，多轮 | `mysql-test-verification` |
| 2 | 归因：确认进程/线程/机制 | `mysql-hotspot-analysis` |
| 3 | 选杠杆：查决策矩阵与负结果 | 本文 §决策矩阵 |
| 4 | 实施：OS/配置/编译/补丁，只改一个变量 | 对应子 skill |
| 5 | 正确性：MTR/冒烟/对账/回读 | `mysql-test-verification` |
| 6 | 性能：同口径 A/B，≥3 轮 | `mysql-test-verification` |
| 7 | 判定：有效/无效/劣化；保留或回滚 | 本文 §输出契约 |
| 8 | 记录：变更、证据、回滚命令、已证伪 | `references/decision-and-negative-results.md` |

## 决策矩阵（先看瓶颈，再选杠杆）

| 瓶颈证据 | 首选杠杆 | 次选 | 避免 |
| --- | --- | --- | --- |
| Point 高 QPS，SQL/页查找/Read View 热点 | AHI 关闭（高 CPU 读多）、`read_io_threads`、表缓存/PS 上限、spin 参数 | Read View version、fast-tuple、rec_get_offsets 补丁 | 无脑加连接/线程 |
| RO 排序/collation 热点 | 索引/SQL 改写、减少 order/distinct、sort/tmp 内存 | space-skipping/NEON collation 补丁 | 只加 buffer pool |
| RW 锁等待/长事务 | 索引/小事务/隔离级别、拆分热点行 | 死锁检测评估、锁等待超时 | 加 CPU 核 |
| RW/WO 提交/fsync 尾延迟 | durability 策略、组提交、io_capacity、盘 | binlog ordered/preallocate 补丁 | 只调 buffer pool |
| 刷脏/checkpoint 抖动 | `io_capacity(_max)`、dirty 水位、page cleaners、盘 | 增大 redo | 长期把 dirty 拉到 100 |
| Buffer Pool 未命中 | 增大 buffer pool/减少工作集 | 索引/查询优化 | 只加 instances |
| IRQ/网络瓶颈 | IRQ/绑核/队列（→ os-optimization） | 多网卡/bond | 调 my.cnf |
| 编译/指令层 | `-O3 -march=...+lse+crc`、LTO（验证） | 原子写、第三方 | 未验证就全量升级 |
| 读路径结构性问题 | 容器/拷贝/缓存/比较补丁 | 微指令优化 | 先做 NEON 再做结构 |

## 配置杠杆（详见 mysql-app-config-optimization）

| 杠杆 | 8U32G 实测参考 | 适用/风险 |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 22G（内存约 70%，符合 50%–70% 区间） | 覆盖工作集后收益递减；过大 OOM |
| `innodb_buffer_pool_instances` | 16 | 小 pool 过多反而开销 |
| `innodb_log_file_size` | 2048M（×8） | 大文件减少 checkpoint，恢复变慢 |
| `innodb_log_buffer_size` | 2G | 大事务/高并发可增 |
| `innodb_io_capacity(_max)` | 30000/40000（峰值 1.33×，按盘实测可低于 1.5× 基线规则） | 按盘能力；过高 IO 竞争 |
| `innodb_page_cleaners` | 16 | 与 pool/盘匹配 |
| `innodb_max_dirty_pages_pct` | 90（只读基线）/按写调 | 高水位尾延迟/恢复风险 |
| `innodb_flush_method` | O_DIRECT | 避免 double cache；小 IO 可能变慢 |
| `innodb_spin_wait_delay/loops` | 20/100（Point）、30/110（RW） | `ut_delay` 热点时调；CPU 饱和慎增 |
| `innodb_read/write_io_threads` | RO 25/5，RW 20/15 | 只读降写线程；写多增写线程 |
| `innodb_adaptive_hash_index` | 高 CPU 读多 OFF | 场景化；必须 A/B |
| `table_open_cache(_instances)` | 500000/64 或 30000/16 | 表多/并发高 |
| `max_prepared_stmt_count` | 4194304（8.0 上限）或 1280000 | PS 并发×语句；超过 4194304 启动失败 |
| `performance_schema` | 压测 OFF | 失去等待证据 |
| `sync_binlog` / `flush_log_at_trx_commit` | 1/1（durable） | 压测可调，需声明数据风险 |

>
> 参数收益高度依赖数据/部署/版本；“参考值”是特定实测，不是承诺。

## 编译/第三方杠杆（详见 mysql-source-optimization 与 build-deployment）

| 杠杆 | 实测/经验 | 条件/风险 |
| --- | --- | --- |
| `-O3` | 基础优化，通常正收益 | 编译时间增加 |
| `-march=armv8-a+crypto+lse+crc` | LSE/CRC 降低原子/校验开销 | CPU 必须支持；x86 不可套用 |
| `-flto=auto` | 场景相关；二进制略小 | 链接失败/编译内存 |
| `-funroll-loops` | 场景相关 | 代码膨胀 |
| 关闭不用引擎/MYSQLX | 减少体积/攻击面 | 业务确认 |
| NVMe 原子写 + `innodb_doublewrite=0` | 减少一次写 | 仅当实例内所有表空间路径（系统/独立/undo/临时/通用）整页原子写逐一确认；任一无法确认则保持 `innodb_doublewrite=1` |
| 第三方分配器（jemalloc/tcmalloc） | 需验证 | ABI/行为差异，非默认 |

## 源码杠杆（详见 mysql-source-optimization）

| 优化 | 机制 | 适用 |
| --- | --- | --- |
| fast-tuple | 固定长度、内存可比较字段直接 memcmp，跳过逐字段 collation | Point/range，aarch64 |
| Read View version | 原子版本号复用视图，减少 `trx_sys->mutex` | 高并发只读/RC |
| space-skipping + NEON collation | ASCII 批量跳过/生成 sort key/哈希 | RO 的 order/distinct |
| binlog ordered | 队列内多组 pending lock/cond，降低全局 `m_lock_done` 争用 | RW/WO 高并发 |
| rec_get_offsets | 传入 compact 标志，省去重复 `dict_table_is_comp` | 读路径 |
| binlog preallocate | `fallocate` 预分配 + 读取有效 EOF | 写多/大 binlog |

原则：**结构性去分配/去哈希 > 微调拷贝 > 指令级微优化**；任何补丁都要 MTR + A/B。

## 负结果与已证伪（详见 references/decision-and-negative-results.md）

| 尝试 | 结果/风险 | 原因 |
| --- | --- | --- |
| 只加 `innodb_buffer_pool_size` 不解工作集 | 收益递减 | 命中率已高 |
| 高 CPU 读多仍开 AHI | 可能负收益 | latch/维护成本 |
| 盲目增大 `innodb_io_capacity` | IO 竞争/尾延迟 | 超过盘能力 |
| 未验证的 `-march`/LTO | 崩溃/链接失败/无收益 | 工具链/CPU 差异 |
| 只改 collation 实现不改 SQL | 收益有限 | 语义/数据分布 |
| 未逐一确认全部表空间路径原子写就关 doublewrite | 数据损坏风险 | 只验数据目录会漏掉独立/undo/临时表空间；任一路径不满足即不可关 |
| 把线程/query-mix 变化当配置收益 | 口径错误 | 运行点变化 |

任何负结果都要记录，避免团队重复试验。

## 风险与回滚

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| my.cnf 参数 | 重启失败/OOM/尾延迟 | 恢复备份 + 重启 |
| durability 策略 | 丢数据 | 恢复原值并确认数据 |
| 绑核/IRQ | 服务抖动/网络瞬断 | 恢复掩码/irqbalance/启动参数 |
| 编译参数 | 链接失败/ABI/崩溃 | 回退 CMake flags，重编，恢复二进制 |
| 原子写/doublewrite | 数据损坏 | 恢复 doublewrite + 重启 + 校验 |
| 源码补丁 | 语义回归/崩溃 | `git apply -R`/恢复源码，重编，恢复二进制 |

高风险变更（durability、数据格式、原子写、补丁）必须先与用户确认并备份；生产变更
安排窗口。

## 输出契约

每轮优化产出：

1. **变更记录**：变量/文件/函数、改前→改后、编译参数、二进制 md5/commit。
2. **归因**：对应热点证据（进程/线程/函数/机制），不是“感觉慢”。
3. **正确性证据**：MTR/冒烟/对账/回读结果。
4. **A/B 结果**：分场景 TPS/QPS/errors/P50/P95/P99/CPU 的逐轮明细与汇总、轮数、波动、口径。
5. **判定**：有效/无效/劣化；保留或回滚；回滚命令。
6. **已证伪**：负结果与原因，写入团队台账。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/config-tuning.md` | 参数原理、场景矩阵、实测影响、在线/重启、风险 |
| `references/compiler-and-atomic-write.md` | `-O3`/LSE/LTO/原子写/第三方、构建与验证 |
| `references/source-optimizations.md` | 补丁位置、机制、代码要点、适用条件、风险、验证 |
| `references/decision-and-negative-results.md` | 决策树、负结果、反模式、停止条件、台账模板 |
| `scripts/verify_build_opts.sh` | 只读校验：版本、库、aarch64 特性、关键符号、md5 |
| `mysql-os-optimization/`（域内兄弟成员） | BIOS 相邻项、IRQ/网卡、THP/大页、ulimit/SELinux、sysctl、磁盘 |
| `mysql-app-config-optimization/`（域内兄弟成员） | my.cnf 场景矩阵、模板渲染、在线/重启、回滚 |
| `mysql-source-optimization/`（域内兄弟成员） | 源码优化点挖掘、补丁清单、实施与验证 |
