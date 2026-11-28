# Profiling 采集手册

## 1. 采样前检查

```
# 调试符号与 perf 权限
cat /proc/sys/kernel/perf_event_paranoid      # 建议 <=1（按安全策略）
file "$(command -v mysqld)"
readelf -S "$(command -v mysqld)" | grep -i debug

# 版本/线程
mysqld --version
mysql -e "SELECT VERSION(), @@performance_schema;"
# 多实例先按所选连接锁定唯一 PID（见 scripts/collect_profile.sh 的 resolve_pid）
ps -Lo pid,tid,comm,psr,pcpu -p "${PID}" | head
```

- 同机多实例时禁止直接取 `pidof mysqld` 的第一个 PID：先由所选连接读 `@@GLOBAL.datadir`/
  `@@GLOBAL.socket`，与各 `/proc/PID/cmdline` 比对；命中数不为 1 即停止采集，避免拼接
  不同实例的调用栈与等待事件。
- release 二进制可能被 strip；`[unknown]` 多时用带 `-g` 的构建或 debug 符号包。
- 容器/虚机内 perf 受宿主权限与 cpuset 影响；必要时在宿主用 `perf -p HOST_PID` 采对应实例。
- `performance_schema=OFF` 时无法看等待事件；定位阶段临时开启（需重启）或说明降级。

## 2. perf on-CPU

以下命令的 `${PID}` 一律由 §1 的 resolve_pid 按所选连接唯一解析后传入；不要在示例里
直接取 `$(pidof mysqld)`，多实例时拼接会违反 §1 的采集纪律。

```
# 按进程采样 20s
perf record -F 999 --call-graph dwarf -p "${PID}" -- sleep 20
perf report -g graph,0.5,callee --stdio | head -100
perf report --stdio --sort comm,dso,symbol | head -60

# 禁止把多个实例的 PID 逗号拼接进 -p：会把不同实例的调用栈混进同一份 perf.data
# 多实例先按 §1 / resolve_pid 锁定唯一 ${PID}；确需多实例时逐个独立采样，不要合并。
```

- `-F 999` 是采样频率；高频更细但开销大。
- `--call-graph dwarf` 依赖调试信息；`-g` 单独写会把 `dwarf` 当命令。
- 关注 self 与 children；`perf report -g graph,0.5,callee` 看调用者视角。
- 内核态热点（`sendmsg`/`futex`/`fsync`）需 `perf` 能读内核符号。

## 3. 火焰图

```
perf script > out.perf
FlameGraph/stackcollapse-perf.pl out.perf > out.folded
FlameGraph/flamegraph.pl --title "mysqld" out.folded > mysqld.svg
```

- 火焰图宽度=采样占比；上下是调用栈，不是时间顺序。
- 看“平台”（宽而平）判断热点函数；看分叉判断调用路径。
- 对 off-CPU 也可生成类似图（用 offcputime 输出折叠）。

## 4. off-CPU

```
/usr/share/bcc/tools/offcputime -p "${PID}" 20 > offcpu.txt
# 只看内核栈
/usr/share/bcc/tools/offcputime -K -p "${PID}" 20 > offcpu_kernel.txt
```

- off-CPU 统计“离开 CPU 的累计时长”，适合锁/IO/网络等待。
- 不要用 `sched_switch` 次数替代时长；次数多不等于等待久。
- 与 on-CPU 对照：on-CPU 高=计算/自旋；off-CPU 高=等待。
- 需要 root/`CAP_BPF`；容器内需宿主权限。

## 5. 系统对照

```
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "${PID}" 1 30 > pidstat.txt
pidstat -d -p "${PID}" 1 30 > pidstat_io.txt
iostat -x 1 30 > iostat.txt
sar -n DEV 1 30 > net.txt
vmstat 1 30 > vmstat.txt
```

判读：

- `mpstat`：业务核是否满、IRQ 核是否满、iowait/steal。
- `pidstat`：mysqld 用户态/内核态占比、上下文切换。
- `iostat`：`%util`、`await`、队列深度；区分数据盘/日志盘。
- `vmstat`：`r` 队列、`si/so`（swap）、`cs`。

## 6. DB 内部证据

```
-- 线程
SELECT THREAD_ID,NAME,TYPE,PROCESSLIST_ID FROM performance_schema.threads;

-- 等待事件：窗口两端各取一次累计快照，按 EVENT_NAME 求增量后排名
-- 窗口开始：
SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name;
-- 窗口结束后以相同列再取一次，按 EVENT_NAME 关联并计算：
--   DELTA_TIME  = SUM_TIMER_WAIT(after) - SUM_TIMER_WAIT(before)
--   DELTA_COUNT = COUNT_STAR(after) - COUNT_STAR(before)
--   ORDER BY DELTA_TIME DESC, DELTA_COUNT DESC LIMIT 30
-- 只按本轮增量排名；直接 ORDER BY SUM_TIMER_WAIT DESC 会把历史累计等待误当成本轮热点。

-- 锁等待
SELECT * FROM performance_schema.data_lock_waits;
SELECT * FROM performance_schema.data_locks LIMIT 50;

-- 长事务
SELECT trx_id,trx_state,trx_started,trx_mysql_thread_id,trx_query
FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
```

```
SHOW ENGINE INNODB STATUS\G
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool%';
SHOW GLOBAL STATUS LIKE 'Innodb_log%';
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SHOW GLOBAL STATUS LIKE 'Threads_%';
SHOW GLOBAL STATUS LIKE 'Com_%';
```

慢日志：

```
SHOW VARIABLES LIKE 'slow_query_log';
SHOW VARIABLES LIKE 'long_query_time';
-- 分析：pt-query-digest 或手工按 Query_time/Lock_time/Rows_examined 排序
```

## 7. Topdown / PMU

```
# 若平台/工具支持（devkit、perf stat、pcm、pmu-tools）
perf stat -p "${PID}" -- sleep 10
# 关注 cycles/instructions、cache-misses、branch-misses
```

- Topdown 适合判断前端/后端/内存瓶颈；不能直接告诉你是哪个 SQL。
- 虚拟化/容器可能禁用 PMU；先确认可用性。
- PMU 计数是辅助证据，不能替代调用栈归因。

## 8. 采样窗口与复现

- 预热后采样，窗口覆盖稳态；至少 10–20s，波动大时延长。
- 记录采样开始/结束时间、QPS/TPS、线程数、绑核、IRQ。
- 同一结论至少两个证据源（如 perf + off-CPU + 状态计数器）。
- 等待事件排名用窗口增量（见 §6）；累计快照只用于求差，不用于归因。
- 优化后按同一窗口/场景复采，比较热点迁移而不是只看总 CPU。
