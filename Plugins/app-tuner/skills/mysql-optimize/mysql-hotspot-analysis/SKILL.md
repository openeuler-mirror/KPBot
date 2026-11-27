---
name: mysql-hotspot-analysis
description: >
  MySQL 8.0 热点分析与 profiling：按场景（Point、只读、读写、只写）采集 perf 火焰图、
  on/off-CPU、Topdown/PMU、`performance_schema` 等待、`SHOW ENGINE INNODB STATUS`、
  `SHOW GLOBAL STATUS`、慢日志，结合连接/后台线程（handle_connection、log_writer/
  log_flusher、page_cleaner、purge）与 InnoDB 结构做归因；覆盖 B+Tree 查找、记录比较/
  offset、Buffer Pool/页 latch、trx_sys/Read View、锁等待、redo/binlog 组提交、
  doublewrite、刷脏、collation/sort key、解析/优化、网络等热点，并给出瓶颈迁移与优化方向。
  用于回答“慢在哪、为什么、证据是什么”；不修改代码/配置，不执行部署与压测
  （分别交给 mysql-build-deployment / mysql-test-verification / mysql-performance-optimization）。
  触发词：MySQL 热点、MySQL profiling、火焰图、perf、off-CPU、ut_delay、锁等待、
  Buffer Pool 热点、log_writer、group commit、排序慢、P95 高、CPU 利用率低。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；perf/FlameGraph、bcc offcputime、devkit Topdown（如可用）、performance_schema；鲲鹏 920/920G + openEuler 实测，x86 可复用方法
metadata:
  skill_type: tool_assisted
  category: diagnosis
  domain: relational_database
  version: "1.0.0"
---

# MySQL 热点分析

## 简介

目标不是“贴一张火焰图”，而是给出一条可复核的因果链：

```
场景/负载 → 瓶颈进程/线程 → 热点函数/阶段 → 机制（锁/IO/内存/算法/同步）→ 证据 → 优化方向
```

覆盖三层证据：

1. **系统/线程层**：CPU 利用率、线程分布、上下文切换、off-CPU、IO、IRQ。
2. **函数层**：perf/flamegraph 的 self/children、调用路径、用户态/内核态。
3. **微架构/阶段层**：Topdown（Front-End/Back-End/Memory Bound）、IPC、cache miss、
`performance_schema` 等待事件、InnoDB 状态计数器。

## 何时触发

- TPS/QPS 上不去、CPU 过高或过低、P95/P99 长尾、写场景越跑越慢。
- 要确认瓶颈在 SQL 层、InnoDB 读路径、锁、redo/binlog、刷脏、排序还是客户端/网络。
- 优化前后解释“收益来自哪里”，或负收益定位原因。
- 需要为 Point/RO/RW/WO 建立热点基线。
- 怀疑 `ut_delay`、`trx_sys->mutex`、`log_sys`、页 latch、`my_strnxfrm`、组提交等。

**不适用**：部署/编译 → `mysql-build-deployment`；压测口径与 A/B 判定 →
`mysql-test-verification`；改代码/调参 → `mysql-performance-optimization`；
OS/BIOS/网卡/绑核通用方法 → `kpbot-app-tuner` 对应 subskill（本域
`mysql-os-optimization` 给 MySQL 场景画像）。

## 采集前先固定场景

没有固定场景的热点没有意义。记录：

- 场景与语句（Point/RO/RW/WO、是否 LIMIT、range_selects/skip-trx）。
- 线程/时长/轮数、客户端位置与数量、是否 prepared statement。
- 部署形态（物理机/容器/虚机）、cpuset/绑核、NUMA、IRQ。
- 数据集/表数/行数、my.cnf 关键项、二进制 md5/编译参数。
- 同时记录 TPS/QPS、errors、P50/P95/P99、mysqld/整机/客户端 CPU、iowait、IRQ。

## 采集速查

```
# 0) 按所选连接解析唯一实例 PID（多实例禁止取 pidof 第一个）：
mysql --socket="${MYSQL_SOCKET}" -N -B -e "SELECT @@GLOBAL.datadir, @@GLOBAL.socket;"
# 用上面两列与各 /proc/PID/cmdline 比对，命中数不为 1 即停止；把唯一命中记为 ${PID}
# （实现见 scripts/collect_profile.sh 的 resolve_pid）

# 1) 线程分布（按 PID，不要整机 grep）
ps -Lo pid,tid,comm,psr,pcpu -p "${PID}" | sort -k5 -nr | head -40
mysql -e "SELECT THREAD_ID,NAME,TYPE,PROCESSLIST_ID FROM performance_schema.threads;"

# 2) perf 采样（窗口覆盖稳态；dwarf 需要调试符号/未 strip）
perf record -F 999 --call-graph dwarf -p "${PID}" -- sleep 20
perf report -g graph,0.5,callee --stdio | head -100

# 3) 火焰图
perf script | FlameGraph/stackcollapse-perf.pl | FlameGraph/flamegraph.pl > mysqld.svg

# 4) off-CPU（锁/IO 等待；单位 us，按累计时长而非次数）
/usr/share/bcc/tools/offcputime -p "${PID}" 20 > mysqld_offcpu.txt

# 5) 系统对照
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "${PID}" 1 30 > pidstat.txt
iostat -x 1 30 > iostat.txt
sar -n DEV 1 30 > net.txt
cat /proc/interrupts > irq_before.txt   # 结束时再取一次 cat /proc/interrupts 做增量（见 scripts/collect_profile.sh）

# 6) DB 内部证据
mysql -e "SHOW ENGINE INNODB STATUS\G" > innodb_status.txt
mysql -e "SHOW GLOBAL STATUS" > global_status.txt
mysql -e "SHOW GLOBAL VARIABLES" > global_vars.txt
# 等待事件取窗口两端快照，按 EVENT_NAME 求增量后排名（不用绝对累计值）
mysql -N -B -e "SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name" > waits_before.tsv
# ...采样窗口...
mysql -N -B -e "SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name" > waits_after.tsv
# 增量排名见 scripts/collect_profile.sh（按 SUM_TIMER_WAIT / COUNT_STAR 的 DELTA 排序）
```

采集纪律：

- 先预热再采样；采样期间不改配置/数据/绑核。
- 多实例必须按所选连接唯一映射 PID，命中不唯一即停止采集；记录采的是哪个 PID/socket/核。
- `perf` 的 `--call-graph dwarf` 需要 `-g` 编译且不 strip；`[unknown]` 高先补符号。
- 火焰图看 self 与 children 两种视图；等待型热点用 off-CPU，不要只看 on-CPU。
- `performance_schema=OFF` 时等待证据缺失，需临时开启或说明降级。

## 场景与瓶颈速查

| 场景 | 主要瓶颈位置 | 典型热点 | 备注 |
| --- | --- | --- | --- |
| Point | SQL 层 + 读路径 | 解析/优化、`row_search_mvcc`、`btr_cur_search`、`rec_get_offsets`、页 latch、Read View、网络 | 高 QPS 下小函数累积；IRQ 也可能成为瓶颈 |
| RO | 读路径 + 排序 | 上述 + `my_strnxfrm_*`、`my_hash_sort_*`、filesort、临时表 | order/distinct 占比小但处理行数可能大 |
| RW | 锁 + 日志 + 刷脏 | 行锁等待、`trx_sys`、`log_writer/log_flusher`、group commit、page cleaner、doublewrite | CPU 不一定高，P95 受同步/IO |
| WO | redo/binlog/IO | redo 写入/fsync、binlog 组提交、doublewrite、刷脏、purge | 尾延迟受盘与 fsync 策略 |
| 混合 | 场景切换 | 热点在“计算型/等待型”间迁移 | 固定绑核可能失去弹性 |

迁移规律：

- 并发↑：Point 从计算/页查找转向 IRQ/网络/锁。
- 加索引/改 SQL：热点从扫描转向排序/回表。
- 开 binlog/提高 durability：写场景从 CPU 转向 fsync/组提交。
- Buffer Pool 不足：读路径增加磁盘读与页替换。

## 归因规则

1. **先分进程/线程**：mysqld 与客户端/IRQ 谁先饱和？业务线程还是 log/page_cleaner/purge？
2. **再分路径**：
   - 读：`row_search_mvcc` → `btr_cur_search` → `page_cur_search` → `rec_get_offsets`；
   - 写：`ha_innobase::write_row` → redo → 组提交 → binlog → doublewrite → 刷脏；
   - 排序：`filesort` → `my_strnxfrm_*` / `my_hash_sort_*`。
3. **再分机制**：
   - 自旋/latch：`ut_delay`、`pthread_mutex_*`、`buf_pool`、`trx_sys->mutex`、`log_sys`；
   - 锁等待：`lock_wait`、`row_lock`、`data_lock_waits`、死锁；
   - 内存：`malloc/free`、`mem_heap`、`ut_alloc`、临时表；
   - IO：`pread/pwrite`、`fsync`、`io_submit`、doublewrite；
   - 网络：`sendmsg/recv`、`net_send`、socket 写阻塞；
   - 算法：比较、排序键、哈希、BFS/连接。
4. **用对照验证**：换线程/加索引/改 durability/加 LIMIT，看热点是否按预期迁移。
5. **区分事实与推断**：火焰图占比是事实；“因为 X 所以慢”需要对照实验或额外证据。

## 常见误判

| 误判 | 纠正 |
| --- | --- |
| “mysqld CPU 低，所以 MySQL 不是瓶颈” | 可能在等锁/IO/网络；看 off-CPU、iowait、锁等待 |
| “CPU 高就是计算瓶颈” | 可能大量自旋/`ut_delay`/内存分配 |
| “`[unknown]` 是无效数据” | 多为内核/vdso/未符号化；补符号或看内核栈 |
| “self 低就不重要” | children 可能很高；看调用路径聚合 |
| “加线程能提升 QPS” | 锁/IRQ/客户端瓶颈时加线程更差 |
| “Buffer Pool 命中率高就没问题” | latch/排序/组提交仍可能是瓶颈 |
| “写慢就是磁盘慢” | 先看 fsync 策略、组提交、doublewrite、锁 |
| “压测数字低就是服务端慢” | 先排除客户端 CPU/解码/网卡 |
| “一个场景提升可代表全部” | Point/RO/RW/WO 热点不同，必须分场景 |

## 输出契约

```
## 热点分析报告

### 场景与身份
- 场景/语句/参数、线程/时长/轮数、客户端
- 部署形态、cpuset/绑核/NUMA/IRQ
- 数据集、my.cnf 关键项、二进制 md5/编译参数

### 性能对照
- TPS/QPS、errors、P50/P95/P99、mysqld/整机/客户端 CPU、iowait、IRQ

### 线程/阶段分解
- 业务线程 vs log_writer/log_flusher/page_cleaner/purge 占比
- on-CPU/off-CPU、等待事件 Top、IO/网络

### Top 热点
| 排名 | self% | children% | 函数 | 线程 | 机制 | 证据 |

### 归因
- 瓶颈进程/线程 → 机制 → 为什么（对照/实验）
- 与性能数字的对应关系

### 优化方向
- 按“OS / 配置 / 编译 / 源码”分类，给预期收益与验证方式
- 明确哪些是假设，哪些已被对照验证
```

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/profiling-playbook.md` | perf/符号/火焰图/off-CPU/Topdown/PMU/performance_schema 采集细节 |
| `references/scenario-hotspots.md` | Point/RO/RW/WO 热点表、线程占比、瓶颈迁移、x86/ARM 差异 |
| `references/innodb-waits-and-locks.md` | Buffer Pool/latch/trx_sys/锁/redo/binlog/刷脏的机制与证据 |
| `references/topdown-and-pmu.md` | Topdown 指标、IPC/cache miss、PMU 工具、判读与限制 |
| `scripts/collect_profile.sh` | 只读采集：默认 dry-run 打印计划；`--self-check` 校验续行/输出路径契约；`--execute` 采 perf/offcpu/pidstat/mpstat/iostat/stats 并写 status.txt |
