---
name: mariadb-hotspot-analysis
description: >
  MariaDB 10.11.16（兼容 10.3.x 案例）的热点分析与 profiling：按场景（只读/点查/只写/
  非索引更新/TPC-C）采集 perf 火焰图、on/off-CPU、Topdown 微架构指标、pidstat/mpstat/
  iostat，结合连接线程/线程池与 InnoDB 后台线程做归因；覆盖 futex 与 latch 竞争
  （lock_sys/trx_sys/buf_pool/log_sys）、跨 NUMA 原子操作、缓存未命中与伪共享、
  回表与扫描、redo/doublewrite/刷脏、binlog 组提交、purge 滞后等热点，并给出瓶颈迁移
  与优化方向。用于回答“慢在哪、为什么、证据是什么”；不修改代码/配置，不执行部署与压测
  （分别交给 mariadb-build-deployment / mariadb-test-verification /
  mariadb-performance-optimization）。
  触发词：MariaDB 热点、MariaDB profiling、MariaDB 火焰图、perf、off-CPU、Topdown、
  futex、latch、lock_sys、P99 高、CPU 利用率低、锁等待、瓶颈定位。
license: MulanPSL-2.0
compatibility: MariaDB 10.11.16；perf/FlameGraph、bcc offcputime、devkit Topdown（如可用）、INNODB_METRICS/PFS；aarch64/x86_64 实测方法通用
metadata:
  skill_type: tool_assisted
  category: diagnosis
  domain: relational_database
  version: "1.0.0"
---
# MariaDB 热点分析

## 简介

目标不是“贴一张火焰图”，而是给出一条可复核的因果链：

```
场景/负载 → 瓶颈线程/阶段 → 热点函数/等待事件 → 机制（锁/原子/内存/IO/日志/算法）
  → 证据 → 优化方向
```

本 skill 覆盖三层证据：

1. **系统/线程层**：CPU 利用率、线程角色分布、futex、上下文切换、IO、NUMA。
2. **函数层**：perf 火焰图的 self/children 热点、调用路径聚合、off-CPU 等待栈。
3. **微架构/计数器层**：Topdown（Front-End/Back-End/Memory Bound）、IPC、cache miss，
`INNODB_METRICS`/`SHOW ENGINE INNODB STATUS`/`performance_schema` 的内部计数与等待。

## 何时触发

- 已有**可采信基线**（同口径协议见 `mariadb-test-verification`）前提下的归因诉求：QPS/TPS 上不去、CPU 利用率异常（过高/过低）、P99 长尾、场景切换后性能变化——尚未建立可采信基线时，先回 `mariadb-test-verification` 建基线。
- 要确认瓶颈在 Server 层、InnoDB、IO、日志刷盘还是客户端。
- 优化前后要解释“收益来自哪里”，或负收益要定位原因。
- 需要为某个场景建立热点基线（只读/点查/只写/非索引更新/TPC-C）。
- 怀疑锁竞争（行锁/MDL/latch）、原子操作、内存分配、回表、redo/刷脏、purge。

**不适用**：部署/编译 → `mariadb-build-deployment`；压测口径与 A/B 判定 →
`mariadb-test-verification`；改代码/调参 → `mariadb-performance-optimization`；
OS/BIOS/网卡/绑核通用项 → `kpbot-app-tuner` 对应 subskill。

> 上述姊妹 skill 同属 `mariadb-optimize` 域，由 `install.sh` 平铺安装。若当前环境找不到
> 入口（未安装/未合入），不要凭空继续部署/压测/改码：把已有证据包（perf.data/folded、
> pidstat/mpstat/iostat、INNODB_METRICS/PFS 采样与窗口）连同待办交回维护者。

## 采集前先固定场景

没有固定场景的热点没有意义。记录：

- 场景与语句/工具（sysbench 哪个 lua、TPC-C 五类占比及其来源/口径、是否只读）。
- 并发/线程数、时长、warmup、数据规模（表数×行数/warehouse）。
- 部署形态、实例数、`thread_handling`、buffer pool/redo 大小、绑核/NUMA。
- 二进制 md5/commit、编译参数、分配器（jemalloc/tcmalloc/系统）。
- 同时记录 TPS/QPS、错误率、p50/p99、各进程 CPU、IO，作为热点解释的对照。

## 采集速查

```
PID="$(pgrep -x mariadbd | head -1)"

# 1) 线程角色与落点（按 PID 枚举）
ps -L -p "$PID" -o tid,comm,psr,pcpu,stat | sort -k4 -nr | head -40

# 2) perf 采样（按进程；覆盖稳态窗口）
perf record -F 999 -g -p "$PID" -- sleep 20
perf report -g graph,0.5,callee --stdio | head -80

# 3) 火焰图
perf script -i perf.data | FlameGraph/stackcollapse-perf.pl | FlameGraph/flamegraph.pl > mariadb.svg

# 4) off-CPU（锁/IO 等待）：按线程累计离核时长
/usr/share/bcc/tools/offcputime -p "$PID" 20 > mariadb_offcpu.txt

# 5) Topdown（若平台/工具支持）
# 使用 devkit 或 perf stat 的 topdown 指标，采集代表核 8-20s

# 6) CPU/IO/网络对照
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "$PID" -t 1 30 > pidstat_t.txt
iostat -x 1 30 > iostat.txt
sar -n DEV 1 30 > net.txt

# 7) 连接实例核对（多实例用 --socket/--port 指向目标，--port 范围 1-65535）
mariadb --port=3307 -N -e "SELECT @@pid_file"
# 核对 pid_file 里的 PID 是否等于 perf 的 --pid；不一致则跳过 SQL 并标注原因

# 8) InnoDB 内部证据（窗口两端各读一次累计计数）
mariadb -e "SHOW GLOBAL STATUS" > status.before
mariadb -e "SHOW ENGINE INNODB STATUS\G" > innodb_status.txt   # 归档前过滤死锁快照中的语句行
mariadb -e "SHOW GLOBAL STATUS LIKE 'Innodb_%'" > innodb_status_vars.txt
# 10.11.16 的 ENABLED 是数值列（1=已启用，0=未启用）；旧版若仍是 STATUS 按该版本列分支
mariadb -e "SELECT NAME,SUBSYSTEM,ENABLED,COUNT FROM information_schema.INNODB_METRICS
             WHERE ENABLED=1 ORDER BY SUBSYSTEM,NAME" > innodb_metrics.txt
mariadb -e "SELECT NAME,SUBSYSTEM,ENABLED FROM information_schema.INNODB_METRICS
             WHERE ENABLED=0 ORDER BY SUBSYSTEM,NAME" > innodb_metrics_missing.txt
# 旧版 STATUS 为字符串（enabled/disabled）；先探测 information_schema.COLUMNS 再分支
mariadb -e "SELECT TRX_ID,TRX_STATE,TRX_STARTED,TRX_WAIT_STARTED,TRX_MYSQL_THREAD_ID,TRX_WEIGHT
             FROM information_schema.INNODB_TRX ORDER BY TRX_STARTED" > innodb_trx.txt
mariadb -e "SELECT * FROM information_schema.INNODB_LOCK_WAITS" > lock_waits.txt
mariadb -e "SHOW GLOBAL STATUS" > status.after
```

采集纪律：

- 先预热再采样，窗口覆盖稳态；采样期间不要改配置/数据。
- `perf`/off-CPU/pidstat/mpstat/iostat/sar 并发启动同一 `--seconds` 窗口，记录起止时间；
  累计计数（`SHOW GLOBAL STATUS`/`INNODB_METRICS`）在窗口两端各读一次，用增量/秒解释，
  不要让系统指标和等待栈来自与 perf 不同的负载阶段。
- 多实例时分别采，记录采的是哪个 PID/核；`perf -p` 多 PID 用逗号分隔。
  SQL 连接必须用 `--socket`/`--port`/`--defaults-extra-file` 指向目标实例：先取 `@@pid_file`
  并核对其中的 PID 与目标 `--pid`，不一致或读不到就跳过 SQL 并标注原因，避免把默认 socket
  上第一个实例的 SQL 证据拼到第二个实例的 perf 上。
- `perf` 默认 `-g`（frame pointer）；确认二进制未保留 frame pointer 时改用
  `--call-graph dwarf`（`-g` 只是调用栈开关，不接收展开方式参数），dwarf 需要二进制带
  调试信息（`-g` 编译且不 strip），`[unknown]` 过高先补符号。脚本默认同样用 `-g`，可用
  `PERF_CALLGRAPH='--call-graph dwarf'` 切换；与 `references/profiling-playbook.md` §2 一致。
- 火焰图看 self 与 children 两种视图，避免把“等待”误判为“计算”。
- `INNODB_METRICS` 默认多数计数器未启用：只读已启用项并把缺项单列记录，不默认执行
  `SET GLOBAL innodb_monitor_enable`（全局状态会持续生效到采样结束之后）。10.11.16 用
  `ENABLED`（数值 1/0），旧版可能仍是 `STATUS`（enabled/disabled），按实际列分支。确需启用时
  单独确认变更范围（仅本实例、指定具体模块），采样后 `SET GLOBAL innodb_monitor_disable`
  恢复原状态，并在 A/B 两腿保持一致。
- 证据包默认不含 SQL 原文：`INNODB_TRX` 只取事务 ID/状态/等待时间等字段（不取 `TRX_QUERY`），
  死锁快照归档前过滤跟随 `MySQL thread id` 的语句行；输出目录每轮独占（非空即拒绝），
  权限 700、文件 600。

## 场景与瓶颈速查

| 场景 | 主要瓶颈位置 | 典型热点 |
| --- | --- | --- |
| 只读 | Server + InnoDB 读 | 回表、B+tree、latch、内存分配 |
| 点查 | 连接/线程 + latch | 线程调度、futex、页 hash/LRU、MDL |
| 只写 | InnoDB 写 + 日志 + IO | redo/刷盘、doublewrite、binlog 组提交 |
| 非索引更新 | 主键点查 + 行锁/redo/undo | 热点行锁、purge |
| TPC-C | 锁 + 日志 + IO | 行锁/hash 抢锁、redo、页竞争 |

`oltp_update_non_index` 用 `UPDATE ... WHERE id=?` 走主键点查，“non_index”指被更新的
`c` 列无索引，不是无索引 WHERE；只有 `WHERE c=?` 才走全表扫描。迁移规律（点查→线程/
futex，回表→buffer pool，只写→日志刷脏，跨 NUMA→亲和）见 `references/scenario-hotspots.md` §6。

## 归因规则

1. **先分进程/线程**：连接线程、线程池 worker、page cleaner、purge、log writer、
IO 线程各占多少 CPU？热点是否集中在某一类？
2. **再分机制**：
   - futex/锁：`futex`、`pthread_mutex_*`、`lock_sys`、`trx_sys`、`buf_pool`、`log_sys`；
   - 原子/伪共享：`__aarch64_ldadd`/`cas`、`std::atomic`、同一 cache line 多线程写；
   - 内存：`malloc`/`free`、`memcpy`、`ut_malloc`、页分配；
   - 存储：B+tree 查找、回表、页读、doublewrite、change buffer、AHI；
   - 日志：redo 拷贝/刷盘、binlog 写、组提交等待；
   - 算法/SQL：filesort、临时表、全表扫描、子查询。
3. **用对照验证**：换并发、换索引、加 `LIMIT`、换绑核、只改一个参数，看热点是否按预期迁移。
4. **区分事实与推断**：火焰图占比是事实；“因为 futex 所以慢”需要对照实验或计数佐证。
5. **版本校准**：10.3 的 `lock_word`/`s_lock_word`、`hash_lock` 热点在 10.11 已变化
（`rw_lock` 改为 `std::atomic`），旧结论只能作为方向，必须重新采样确认。

## 常见误判

CPU 没满不等于不是瓶颈（看 off-CPU/等待事件）；futex 高不等于数据库 bug（先看能否
减少共享写）；self 低不等于不重要（看 children）；P99 高不一定是慢查询（checkpoint/
purge/刷脏/网络）；Topdown Memory Bound 不等于要加内存（可能 cache miss/伪共享）；
10.3 的热点补丁不能直接套 10.11（锁实现已重写）。完整误判表与纠正见
`references/profiling-playbook.md` §11。

## 输出契约

报告至少含：场景与身份（版本/md5/并发/绑核/线程模型）、性能对照（TPS/QPS/错误率/
p50/p99/CPU/IO）、线程/阶段分解、Top 热点表（self%/children%/函数/线程角色/机制/
证据）、锁与刷脏等待、归因链（瓶颈线程 → 机制 → 为什么）、优化方向与验证方式，
并标注哪些是假设、哪些已被对照验证。完整模板见 `references/profiling-playbook.md` §12。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/profiling-playbook.md` | 采集命令、符号、火焰图、off-CPU、Topdown、INNODB_METRICS/PFS、窗口与注意事项 |
| `references/scenario-hotspots.md` | 五类场景热点表、Server/InnoDB 分类占比、瓶颈迁移、x86/ARM 差异、扩展实验 |
| `references/innodb-lock-and-thread-model.md` | 行锁/MDL/latch、rw_lock/srw_lock/sux_lock 演进、线程角色，解释热点为何出现 |
| `references/stats-and-topdown.md` | 状态计数器、INNODB_METRICS/PFS、Topdown 指标、IPC/cache miss、优化优先级 |
| `scripts/collect_profile.sh` | 只读采集：默认 dry-run 打印计划；`--execute` 并发采 perf/火焰图/offcpu/pidstat/mpstat/iostat/sar 与窗口两端累计计数（各通道含 sar 均登记 `.channel_status`） |

### `collect_profile.sh` 参数与变量

| 参数/变量 | 含义 |
| --- | --- |
| `--pid` | 目标 mariadbd PID（默认自动发现）；`--port` 仅 1-65535、`--seconds` 仅 1-600，非法值 exit 2 |
| `--out` | 每轮独占目录，仅不存在或空目录可写，权限 700；非空 exit 2，避免复用旧产物 |
| `SOCKET` / `MARIADB_BIN` / `MARIADB_DEFAULTS_FILE` | 客户端连接设置，须指向 `--pid` 对应实例（核对 `@@pid_file` 内容里的 PID） |
| `FLAMEGRAPH_DIR` | FlameGraph 脚本目录 |
| `PERF_CALLGRAPH` | perf 栈展开方式；默认空（脚本用 `-g`/frame pointer），可设 `--call-graph dwarf`（仅接受 dwarf/fp 受控值） |
| `METRIC_FLAG` | 内部变量：INNODB_METRICS 实际状态列，10.11=ENABLED，旧版=STATUS |
| `SQL_OK` / `SQL_SKIP_REASON` | 内部变量：SQL 通道是否启用、跳过原因（写入 `sql_skip.txt`） |
| `CH_STATUS_FILE` / `record_status` / `VALID` | 内部变量/函数：逐通道退出状态，命令成功且非占位数据才计入有效通道数 |
| `SAR_BG` | 内部变量：sar 后台采集 PID，与其他通道一样经 `wait_channel net.txt` 登记状态 |
