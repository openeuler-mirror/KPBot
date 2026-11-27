# MariaDB 优化域（app-tuner 侧）

MariaDB（源码基线 10.11.16，案例基线 10.3.x 鲲鹏 4P / KVM 虚拟化）这个应用从
**架构理解 → 编译部署 → 正确性/性能验证 → 热点归因 → OS/配置/源码优化**的完整调优链路。
五个成员各自独立可调用，组合起来覆盖一次完整的 MariaDB 性能优化闭环。

> `mariadb-optimize/` 本身是源码树中的领域分组，不是独立 skill；安装时五个成员会被
> 平铺到 `<tool>/skills/<skill-name>/`，由 Claude Code / OpenCode 一层发现。

## 成员

| Skill | 职责 | 典型入口 |
| --- | --- | --- |
| [`mariadb-architecture`](mariadb-architecture/SKILL.md) | Server 层 + 存储引擎 handler 分层、InnoDB/Aria/MyISAM/MyRocks/ColumnStore 定位、连接/线程模型、SQL→优化器→执行器→InnoDB 读路径、redo/binlog 2PC、MVCC/undo/purge、锁与 latch（rw_lock/srw_lock/sux_lock）、内存/文件布局、参数地图、源码路径（被动只读知识库） | “这条 SQL 怎么走到磁盘”“buffer pool 和 redo 谁负责持久化”“10.11 的 rw_lock 为什么和 10.3 不一样” |
| [`mariadb-build-deployment`](mariadb-build-deployment/SKILL.md) | aarch64/x86 源码编译、三方依赖与作用、jemalloc/tcmalloc、LSE 编译、单实例/多实例/主从/Galera、NUMA 绑核与大页、配置模板、数据装载、mariabackup 备份恢复、升级回滚；含只读 `preflight_check.sh` | “从零部署”“换二进制/升级”“恢复备份”“多实例端口怎么排” |
| [`mariadb-test-verification`](mariadb-test-verification/SKILL.md) | mtr 单元/回归、sysbench 场景、TPC-C（TPCCRunner）、mariadb-backup 校验、复制一致性、指标口径（TPS/QPS/p99/错误率/CPU）、A/B 同口径复测与噪声判定；含只读 `smoke_test.sh` | “优化有没有效果”“功能有没有坏”“压测怎么跑才可复现” |
| [`mariadb-hotspot-analysis`](mariadb-hotspot-analysis/SKILL.md) | perf/火焰图/off-CPU/Topdown、pidstat/mpstat/iostat、InnoDB 内部计数器（INNODB_METRICS/MUTEX/TRX/LOCK_WAITS）、performance_schema、场景热点迁移与归因；含 dry-run `collect_profile.sh` | “慢在哪”“为什么 CPU 不饱和”“为什么 P99 抖动”“锁热点是谁” |
| [`mariadb-performance-optimization`](mariadb-performance-optimization/SKILL.md) | OS/虚拟化/绑核/大页、InnoDB 缓存/redo/IO/双写、线程与表缓存、binlog 组提交、Aria、编译与三方库（LSE/jemalloc/tcmalloc）、源码（伪共享、顺序锁、lock_word 拆分、NUMA interleave mask）及负结果；含 `verify_build_opts.sh` | “怎么调”“改哪里”“收益多少”“负收益怎么回滚” |

## 域内路由

```
只想理解系统            → mariadb-architecture
要从零装/恢复/升级      → mariadb-build-deployment
要证明对/证明快         → mariadb-test-verification
要知道慢在哪/为什么慢   → mariadb-hotspot-analysis
要把证据变成改动        → mariadb-performance-optimization
```

一次典型的优化闭环：

```
architecture（理解链路）
  → build-deployment（起环境、建库、验收）
  → test-verification（基线 + 正确性门禁）
  → hotspot-analysis（定位瓶颈机制）
  → performance-optimization（单变量优化）
  → test-verification（同口径 A/B）
  → 保留或回滚
```

## 版本与适用性

- 源码视图以 **MariaDB 10.11.16**（`VERSION`：10.11.16，stable）为真源；目录已按
`storage/innobase/{buf,log,trx,lock,sync,row,...}` 拆分，与 10.3/10.4 的单层目录不同。
- 性能案例（历史集采案例 C3〔TPC-C〕、KVM 虚拟化、LSE/顺序锁/lock_word 拆分）来自 **10.3.x + 鲲鹏**
实测，属于**历史证据**，不是 10.11 的现成补丁：10.6 起 InnoDB 的 `rw_lock_t` 已重写为
`std::atomic<uint32_t>` 的 `rw_lock` 类（`storage/innobase/include/rw_lock.h`），
老的 `lock_word`/`s_lock_word` 字段在 10.11 不存在，移植必须重新做热点验证。
- 结论必须由目标版本、目标硬件的证据确认；文档中的数值标“实测基线”，不写成通用规格。

## 与插件内其他 skill 的边界

| 问题 | 去哪里 |
| --- | --- |
| OS governor/THP/大页/irqbalance/ulimit/grub | `kpbot-app-tuner` 的 `os-optimization` |
| BIOS SMT/C-State/NUMA/内存刷新/预取 | `bios-optimization` |
| 网卡队列/RSS/RPS/中断绑定 | `network-optimization` |
| CPU 绑核/NUMA 布局/线程亲和 | `cpu-affinity-optimization` |
| 磁盘/文件系统/IO 调度器专项 | `io-optimization` |
| malloc/memcpy/压缩/加密库选型 | `performance-library-selection` |
| 通用编译参数/LTO/PGO/向量化 | `compiler-optimization` |
| 通用应用配置（线程/连接/批量/缓存） | `application-config-optimization` |
| MySQL/InnoDB 通用工作负载分析 | `database-workload-analysis` |
| MyRocks/RocksDB 引擎自身写停顿/compaction | `rocksdb-perf-diagnostics` |
| 通用 C/C++ 微架构/算法改造 | `kpbot-code-optimizer` |
| MariaDB 专属架构/部署/验证/热点/配置与源码优化 | 本域五个成员 |

## 准入判据

新增成员须满足：

- 结论落在 **MariaDB 应用自身**（架构、部署、验证、热点、配置/源码优化），
不是通用 OS/网络/编译理论。
- 有明确的输入/输出契约、停止条件、失败处理与回滚方式。
- 涉及变更的内容必须区分只读采集、dry-run、低风险在线变更、需重启变更、需重编译变更。
- 有可复现的验证入口（evals 或 skill 内 `cases/`）。
- 不写入个人环境绝对路径、私网 IP、凭据；示例统一用变量占位。

## 安装与校验

- 领域分组只存在于源码树；`install.sh` 会将五个成员平铺安装。
- 完整性校验（结构、链接、锚点、evals、敏感信息）：
`bash mariadb-architecture/cases/skill-integrity/run.sh`
- 所有成员均提供 `evals/evals.json`；变更后应重跑完整性用例与仓库测试。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/mariadb-optimize/**`
