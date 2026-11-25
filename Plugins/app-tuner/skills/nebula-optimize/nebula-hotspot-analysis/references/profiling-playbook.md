# Profiling 采集手册

## 1. 准备

| 工具 | 用途 | 检查 |
|------|------|------|
| `perf` | CPU 采样、off-CPU、Topdown | `perf --version`；`kernel.perf_event_paranoid` |
| FlameGraph | 火焰图 | `stackcollapse-perf.pl`、`flamegraph.pl` |
| `pidstat` / `mpstat` / `iostat` / `ss` | CPU/IO/连接对照 | `sysstat`、`iproute` |
| devkit（可选） | Topdown/微架构 | 平台支持时优先 |
| `nebula-console` | 查询验证 | 脚本中用于确认场景 |

权限：

```bash
cat /proc/sys/kernel/perf_event_paranoid
# 建议 <=1；容器内需要 CAP_PERFMON/SYS_ADMIN 或宿主机采集
```

符号：

- Nebula 发布二进制可能已 strip；自编时保留 `-g` 或至少 `.symtab`。
- 内核态 `[unknown]` 需要 vmlinux/`/proc/kallsyms` 或 `--symfs`。
- 采不到符号时，先补符号再分析，不要对着 `[unknown]` 猜。

## 2. CPU 采样

### 2.1 按进程

```bash
PID=$(pidof nebula-graphd | tr ' ' ',')   # perf -p 多 PID 用逗号分隔；pidof 输出空格分隔须先转换
perf record -F 999 -g -p "$PID" -- sleep 20
perf report -g graph,0.5,callee --stdio | head -100
```

- `-F 999`：约 1000Hz；太高开销大，太低样本不足。常规 99–999。
- `-g`：调用栈；可加 `--call-graph dwarf`（需符号/调试信息，开销更大）。
- `-- sleep 20`：采样窗口；覆盖稳态，避开启动/预热。

### 2.2 按 CPU/全系统

```bash
# 只采代表核（避免全系统噪声）
perf record -F 999 -g -C 0-7 -- sleep 20

# 全系统（谨慎，开销大）
perf record -F 99 -a -g -- sleep 10
```

多实例时选代表实例；若要比较 graphd 与 storaged，分别采，记录 PID/核。

### 2.3 线程过滤

```bash
# 查看线程名与 CPU
ps -eLo pid,tid,comm,psr,pcpu | grep nebula

# perf report 中按线程名过滤（示例）
perf report --stdio -g graph,0.5,callee | grep -E 'executor|reader|netio|io'
```

线程名是归因的关键：`executor-pri3-*` 是 thrift worker（**graphd 与 storaged 同名前缀，
跨进程须按 PID 区分**）；storaged 读池是 `reader-pool-pri3-*`（配 io 时 `reader-pool-N`），
网络 IO 是 `IOThreadPool<N>`（folly 默认命名）；不存在 `storage-worker` 线程。

## 3. 火焰图

```bash
perf script > perf.unfold
stackcollapse-perf.pl perf.unfold > perf.folded
flamegraph.pl perf.folded > flamegraph.svg
```

两种视角：

- **self**：函数自身耗时（不含子调用），适合找“最热的叶子”。
- **children**：调用链总耗时，适合找“哪条路径整体最重”。

```bash
# 调用路径看普通火焰图（flamegraph 按完整折叠栈聚合，--title 只改标题，
# 图框宽度仍包含子调用的样本，不能当“自身占比”用）：
flamegraph.pl --title "graphd call tree" perf.folded > graphd_calltree.svg

# 自身占比看 perf report 的 Self 列（--no-children）或按栈叶聚合：
perf report -i perf.data --stdio --no-children | head -60
```

判读：

- 宽而平的栈：热点分散，可能是内存分配/锁/调度。
- 窄而深的栈：某个调用链深，可能是算法/序列化/逐层处理。
- `[unknown]` 块：结合线程名与调用者推断（sendmsg/recv/epoll/futex）。

## 4. off-CPU 分析

用途：线程不在 CPU 上时在等什么（锁、IO、RPC、调度）。

```bash
# 注意：对 sched_switch 直接 perf report 得到的是按“切换事件次数”的占比，
# 大量短等待会排在少数长等待之前，不能据此判断哪条路径累计阻塞最久。
# 判“累计阻塞”用 offcputime（bcc），按目标 PID 累计离核时长，输出单位为微秒（us），
# 每行“调用栈; 累计离核 us”，头部即累计阻塞最久的路径：
/usr/share/bcc/tools/offcputime -p "$(pidof nebula-storaged | awk '{print $1}')" 20 > storaged_offcpu.txt

# 只有 perf 时：按目标 PID 过滤 sched_switch，切出/切入事件按时间戳配对、
# 以时间差加权累计（单位 us），不要用事件次数占比替代时长。
perf record -e sched:sched_switch -p "$(pidof nebula-storaged | awk '{print $1}')" -g -- sleep 20
perf script -i perf.data > sched.txt   # 自行按 prev_pid/pid 过滤并累计时间差
```

也可用 `offcputime`（bcc）或 `perf sched`。关注：

- `futex_wait` / `pthread_cond_wait`：锁/条件变量；
- `epoll_wait` / `recv`：等网络；
- `fdatasync` / `fsync` / `io_schedule`：等磁盘；
- `nanosleep` / `sched_switch`：等待/调度。

off-CPU 与 on-CPU 结合：CPU 不高但延迟高，通常是 off-CPU 等待。

## 5. Topdown / 微架构

平台支持时（鲲鹏 devkit 或 perf topdown）：

```bash
# perf stat 示例（指标名随平台/内核版本变化）
perf stat -C 0-7 -e cycles,instructions,cache-misses,branch-misses -- sleep 10

# devkit 采集（按工具实际命令）
# devkit topdown --pid <pid> --duration 20 ...
```

关注指标：

| 层级 | 指标 | 含义 |
|------|------|------|
| 一级 | Front-End Bound | 取指/解码受限（icache、分支、指令流） |
| 一级 | Back-End Bound | 执行/访存受限 |
| 二级 | Memory Bound | L1/L2/L3/DRAM 受限 |
| 二级 | Core Bound | 执行单元/依赖链受限 |
| 基础 | IPC | 每周期指令数；低 IPC 常伴随 stall |
| 缓存 | L1I/L1D/L2/L3 miss | 判断访存局部性 |
| 分支 | Branch miss | 分支预测失败 |

判读原则：

- Topdown 说明“CPU 在等什么”，不直接说明“业务为什么慢”。
- 必须结合火焰图/线程池/HTTP stats；单看 Memory Bound 不能得出“换内存”的结论。
- 采样核要覆盖热点线程所在核；采到空闲核会得到错误结论。

## 6. HTTP /stats

Nebula 每个服务有 HTTP stats 端点（端口见配置文件）：

| 服务 | 常见端口 | 内容 |
|------|---------|------|
| graphd | 19669（默认；多实例用 `${GRAPH_HTTP_PORT_N}`） | QPS、RPC、延迟、会话、执行器 |
| storaged | 19779 | get_neighbors、scan、写入、Raft、KV |
| metad | 19559 | 心跳、Raft |

```bash
curl -s http://127.0.0.1:19669/stats > graphd_stats.txt
curl -s http://127.0.0.1:19779/stats > storaged_stats.txt

# 过滤关键指标
grep -E 'num_queries|num_rpc|latency|num_get_neighbors|num_slow_queries' graphd_stats.txt
grep -E 'get_neighbors|scan|add_vertices|latency|raft' storaged_stats.txt
```

采集方法：

- 压测进入稳态后，隔 1s 采 5 次，取稳定窗口；不要只采一个瞬时点。
- 记录采集时间窗口与 QPS，便于把指标和负载对应。
- 指标名随版本变化，先 `curl | head` 看实际格式，再 grep。
- 用 `/stats?format=json`（如支持）便于解析；否则按文本解析。

时间分解常用指标（名称以实际版本为准）：

- graphd：`num_queries.sum.60`、`num_rpc_sent_to_storaged.sum.60`（3.8.0 把发往 storaged 与
  metad 的 RPC 分开计数，没有单独的 `num_rpc_sent`；比值计算必须用同窗口、同实例的指标，
  如 `num_rpc_sent_to_storaged.sum.60 / num_queries.sum.60`，零查询窗口分母为 0，跳过）、
  `query_latency_us.avg`；
- storaged：`num_get_neighbors.rate`、`get_neighbors_latency_us.avg`、
  `num_scan_vertex.rate`/`num_scan_edge.rate`、`scan_vertex_latency_us.avg`、`num_add_vertices.rate`；
- 写入：`num_add_edges`、`num_add_vertices`、`raft` 相关延迟/日志。

## 7. 系统对照

```bash
# CPU 各核
mpstat -P ALL 1 30 > mpstat.txt

# 进程 CPU/内存/线程（pidstat -p 的多 PID 用逗号分隔；pidof 输出空格分隔，
# 必须先转换，且多实例时先从配置确认要采的实例，别把别的实例混进来）
GPIDS=$(pidof nebula-graphd   | tr ' ' ',')
SPIDS=$(pidof nebula-storaged | tr ' ' ',')
pidstat -p "${GPIDS},${SPIDS}" 1 30 > pidstat.txt
pidstat -t -p "$(pidof nebula-graphd | awk '{print $1}')" 1 10 > graphd_threads.txt

# 磁盘
iostat -x 1 30 > iostat.txt

# 网络连接/队列（端口从实例配置读取：graphd 默认 9669、storaged 默认 9779；
# 旧写法 ':967[0-9]' 匹配不到默认端口 9669，会统计成 0）
GP=$(grep -oP '(?<=^--port=)[0-9]+' "${CONF_DIR}/graphd.conf"   2>/dev/null | head -1); GP=${GP:-9669}
SP=$(grep -oP '(?<=^--port=)[0-9]+' "${CONF_DIR}/storaged.conf" 2>/dev/null | head -1); SP=${SP:-9779}
ss -s
# 末尾词边界：端口 800 不能误命中 8000（:80 后跟数字不算）
ss -tn | grep -E ":(${GP}|${SP})[^0-9]" | wc -l
```

用途：

- 确认 CPU 是否打满、是否只有少数核忙（绑核/调度问题）。
- 确认磁盘 await/%util 是否饱和（写入长尾）。
- 确认连接数是否异常（客户端瓶颈/连接泄漏）。

## 8. 采样窗口建议

| 场景 | 预热 | 采样窗口 | 说明 |
|------|------|---------|------|
| Go1Step | 10–20s | 20–30s | 高 QPS，样本充足 |
| Go2Step | 20–30s | 20–30s | 结果多，避免客户端干扰 |
| Go3Step | 30–60s | 30–60s | 查询重，窗口要覆盖多轮 |
| FindShortestPath | 20–30s | 20–30s | BFS 延迟高 |
| InsertPerson | 10–20s | 20–30s | 写路径，注意 compaction 周期 |

Topdown/HTTP stats 与 perf 可并行，但记录各自窗口；不要在窗口内改数据/配置。

## 9. 证据归档

每次分析保存：

```
artifacts/<date>_<scenario>/
├── env.txt            # uname/lscpu/numastat/进程参数/二进制 md5
├── perf.data          # 原始采样
├── perf.folded        # 折叠栈
├── graphd.svg         # 火焰图
├── storaged.svg
├── pidstat.txt / mpstat.txt / iostat.txt
├── graphd_stats.txt / storaged_stats.txt
├── k6_result.txt      # QPS/延迟/成功率
└── notes.md           # 场景、窗口、异常、结论
```

没有这些，后续无法复核“为什么得出这个热点结论”。

## 10. 注意事项

- 采样本身有开销（尤其 dwarf/高频/全系统），先小窗口试，确认不影响被测指标。
- 容器内 perf 可能受限；优先宿主机采集，或给足 capability。
- 多租户环境先确认没有其他负载；否则热点会混入无关进程。
- `[unknown]` 高时先补符号/内核符号，不要直接下结论。
- 火焰图占比是采样比例，不是精确耗时；跨轮比较要看趋势与对照。
- 任何“优化后应提升”的推断，都要回到 `nebula-test-verification` 做同口径 A/B。
