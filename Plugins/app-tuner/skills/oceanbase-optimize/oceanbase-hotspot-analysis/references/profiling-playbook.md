# Profiling 采集手册

## 1. 采集窗口与前置

1. 固定场景（lua、线程、时长、是否 obproxy），记录 TPS/延迟/CPU。
2. 预热 ≥60s；采样窗口覆盖稳态（建议 30s，写场景避开一次完整冻结/合并窗口或明确标注）。
3. 采样期间不改配置、不重置数据、不并行其他压测。
4. 记录采样 PID/核、绑核/NUMA、OB 版本与二进制 md5。

## 2. perf 与火焰图

```
# CPU 采样（默认按 PID；-g 必须开启，否则 perf script 无法还原父调用链）
perf record -p "${OBS_PID}" -F 99 -g -- sleep 30

# 整核采集（显式选择，含核上所有任务；仅目标固定核且需对比同核其他进程时使用，产物标注 cpus 范围）
perf record -C 0-31 -F 99 -g -- sleep 30
perf report --stdio --sort comm,dso,sym | head -80
perf top -C 0-31 -- sleep 10

# 火焰图（按 PID 采样时产物名带 pid 标签，整核时带 cpus 标签）
perf script > perf.script
FlameGraph/stackcollapse-perf.pl perf.script > perf.folded
FlameGraph/flamegraph.pl --title "observer read_only" perf.folded > observer_pid${OBS_PID}.svg
```

要点：

- `-F 99` 是常用采样频率；过高会引入开销，过低会丢热点。
- 默认按 PID 采样（含子线程）：多实例共用核时 `-C` 会混入其他进程，目标不在指定核时又会漏采；确需整核时显式 `-C` 并在产物名/meta/status 单独标明范围与目标 PID。
- `-g` 是调用栈，必须开启才能用 `perf script` 还原父调用链并做 children 分析；需要展开方式时用 `--call-graph dwarf`（要求二进制带调试信息）。
- `[unknown]` 高时：编译加 `-g` 不 strip，或安装 debuginfo；否则结论不可用。
- perf/offcpu/mpstat/pidstat/iostat 并发启动、共用同一窗口，各自起止时间记入 `timing.txt`；不要串行逐个等待一个窗口，否则指标跨时段无法对照。
- 看 self（自身耗时）与 children（调用链累计）两种视图。
- 业务热点“多而分散”（单个热点函数占比都不高）时，微架构/访存优化空间有限，应优先看系统级瓶颈。

## 3. off-CPU

```
# 累计离核时长（微秒），观察等锁/等 IO
/usr/share/bcc/tools/offcputime -p "${OBS_PID}" 30 > offcpu.txt
/usr/share/bcc/tools/offcputime -df -p "${OBS_PID}" 30 > offcpu_folded.txt
FlameGraph/flamegraph.pl --color=io --title "off-CPU" offcpu_folded.txt > offcpu.svg
```

典型等待：

- `ObIOHandle::wait` / `pwrite` / `pread`：等 IO（写场景常见）。
- `ObMvccValueIterator::lock_for_read_inner_`：等事务/锁。
- `ObLatchWaitQueue` / futex：等 latch。
- 网络 `recv/send`：等对端/带宽。

## 4. Topdown / PMU

平台脚本（示例，按实际工具调整）：

```
# 全量 PMU/Topdown
bash ALL_Linux_PMU.sh "${OUT_DIR}"

# 代表核
perf stat -C 0-31 -e cycles,instructions,branches,branch-misses,cache-misses -- sleep 10
```

Topdown 指标树（鲲鹏工具输出）：

```
frontend_bound
  ├─ fetch_latency_bound（icache/itlb/branch flush）
  └─ fetch_bandwidth_bound
bad_spec
  └─ branch_mispredicts / machine_clears
retiring
backend_bound
  ├─ core_bound（exe ports / div / fsu）
  └─ memory_bound
       ├─ l1_bound（dtlb/misalign/hazard）
       ├─ l2_bound（snoop/arb/pipeline）
       ├─ l3_bound
       ├─ mem_bound（local/remote dram）
       └─ store_bound
```

判读：

- OB 只读常见：Backend Bound 很高、Memory Bound 占大头、L3 Bound 高。
- 950 vs 920B：950 前端更低、L3 Bound 更高（950 高于 920B，原百分比已脱敏），与 L3 private/cluster 时延相关。
- 只写：core_bound 与 memory_bound 都高，另有 off-CPU 等 IO。
- IPC 低（0.4–0.6）+ 高 L3 Bound ⇒ 访存延迟受限，不是指令吞吐受限。

## 5. SPE / topdelay（如可用）

```
# 采集指令级时延（工具名以现场为准）
spe record -C 0-31 -- sleep 20
spe report --top-delay 1000
```

关注：

- `ldaddal/ldadd/cas` 等原子指令的高时延（跨 cluster 共享）。
- `recv_request` 路径里的 barrier + load（950 实测平均端到端时延高于 920B）。
- serialize 相关的随机数组访问（ApplySrv）。

## 6. sockmap / eBPF（采集只查状态，启用另走授权 A/B）

采集流程**只读检查当前状态**，不挂载 cgroup2、不启动 sockmap：否则主机会留下挂载和后台进程，
且测到的是启用后的网络路径，无法代表现状。

```
# 只读状态检查（不改任何状态）
mount | grep -w cgroup2 || echo "no cgroup2 mount"
pgrep -a sockmap || echo "sockmap not running"
# 观察现有路径：sql_nio CPU 占比、网络栈深度、火焰图 vfs_read 层级
```

### 6.1 单独授权的 A/B 流程（会改主机状态，须审批后手工执行）

仅在获得变更授权、明确回退步骤时执行；先留基线，再与 SMC-D/hisock 成对 A/B，结束后必须退出、卸载并核验恢复。

```
CGP2=/tmp/cgp2

# 启用（需授权）
mkdir -p "${CGP2}"
mount -t cgroup2 -o nosuid,nodev,noexec none "${CGP2}"
./sockmap "${CGP2}" &
SOCKMAP_PID=$!

# 在授权窗口内采集 A/B 两组数据（同一场景、同一口径）...

# 退出与卸载（必须做）
kill "${SOCKMAP_PID}" 2>/dev/null || true
wait "${SOCKMAP_PID}" 2>/dev/null || true
pgrep -a sockmap || echo "sockmap stopped"
umount "${CGP2}" 2>/dev/null || true
mount | grep -w "${CGP2}" || echo "cgroup2 unmounted"
rmdir "${CGP2}" 2>/dev/null || true
```

恢复核验：`mount | grep cgroup2` 与启用前一致、`pgrep sockmap` 无残留，网络路径（`sql_nio` CPU、
中断绑核、火焰图 `vfs_read` 层级）回到基线；否则按现场回退步骤处理后再判读数据。

注意：

- sockmap 与 SMC-D/hisock 收益重叠，需成对 A/B。
- 开 sockmap 后网卡中断绑核的收益可能变小（实测无影响）。
- 用 eBPF 需要内核支持与权限；生产启用需评审。

## 7. 系统与线程

```
# 整机与目标核
mpstat -P ALL 1 30
pidstat -p "${OBS_PID},${PROXY_PID},${SYSBENCH_PID}" 1 30
sar -n DEV 1 30
iostat -x 1 30

# 线程级
top -H -p "${OBS_PID}"
ps -Lo pid,tid,comm,psr,pcpu -p "${OBS_PID}"
```

判断：

- 目标核是否打满（idle 3–5% 与 10%+ 的差异会解释 7–8% 性能差）。
- 偶数核/奇数核不均衡通常是 SMT 选核顺序，不一定是问题。
- observer、obproxy、sysbench 是否在同一 NUMA/P。

## 8. 数据库内部指标

### 8.1 GV$SYSSTAT（实测常用 STAT_ID）

```
SELECT /*+read_consistency(weak)*/ NAME, VALUE
FROM GV$SYSSTAT
WHERE STAT_ID IN (
  10000,10001,10002,10003,10005,10006,      -- rpc packet in/out/delay
  140002,140003,                            -- max memory size / memory usage（租户内存上限/使用量）
  40006,40007,40008,40009,                  -- sql update/delete count/time
  40010,40011,40012,                        -- sql local/remote/distributed count
  50000,50001,50008,50009,                  -- row cache / block cache
  60000,60001,60002,60003,60004,60005,      -- io read/write count/delay/bytes
  60019,60020,60021,60022,60023,60024,      -- memstore lock/wait
  80057                                     -- clog trans log size
) AND CON_ID > 1000;
```

| 指标 | 含义 | 判读 |
| --- | --- | --- |
| `rpc packet in/out`、`rpc net delay` | RPC 量与时延 | 跨机/跨 Zone 越多越大 |
| `sql local/remote/distributed count` | 本地/远程/分布式执行 | 远程多 ⇒ DAS/网络瓶颈 |
| `sql update/delete count/time` | 更新/删除语句量与耗时 | 写负载放大/冲突时上升 |
| `row/block cache hit/miss` | 缓存命中 | miss 高 ⇒ 读 IO 增加 |
| `io read/write count/delay/bytes` | IO 量与时延 | 写延迟高 ⇒ redo/转储/盘 |
| `memstore read/write lock`、`wait ... lock time` | MemTable 锁等待 | 等待高 ⇒ 热点行/长事务 |
| `max memory size` / `memory usage` | 租户内存 | 接近上限 ⇒ 洗缓存/失败 |
| `clog trans log total size` | redo 量 | 与写入量/事务大小相关 |

### 8.2 线程状态

```
-- 列名以版本为准；示例与实测抓取一致
SELECT SVR_IP, SVR_PORT, TENANT_ID, TID, TNAME, STATUS,
       LATCH_WAIT, LATCH_HOLD, TRACE_ID, CGROUP_PATH
FROM GV$OB_THREAD;
```

用途：

- 识别 `sql_nio*`、`T<tenant>_ApplySrv*`、`T<tenant>_ReplaySrv*`、DagWorker/LSFreeze、`MINOR_EXE`；`T*_L0_G0` 是租户 worker，出现时按请求处理归因，不据此判冻结/转储。
- 看 LATCH_WAIT/LATCH_HOLD 判断是否卡在 latch。
- 看活跃线程数：只写场景 AMD 约 160+，920B 约 120+（920B 更早遇瓶颈）。

### 8.3 SQL 审计

```
SELECT /*+read_consistency(weak)*/ SVR_IP, TENANT_ID, PLAN_ID, QUERY_SQL,
       ELAPSED_TIME, QUEUE_TIME, EXECUTE_TIME, RETURN_ROWS, RET_CODE
FROM GV$OB_SQL_AUDIT
ORDER BY ELAPSED_TIME DESC LIMIT 20;
```

用于确认：慢在排队（QUEUE_TIME）还是执行（EXECUTE_TIME），是否远程/分布式。

## 9. obproxy

- 用独立 PID 采火焰图/off-CPU/Topdown，避免和 observer 混在一起。
- 关注 `read_from_net` / `write_to_net` / 协议解析 / epoll 占比。
- 火焰图形状在不同架构可能“区块平移”（两块合并/拆分），不代表功能差异。
- 去掉 obproxy 是诊断实验，不是优化结论；必须记录口径变化。

## 10. 采集产物清单

```
outdir/
  meta.txt              # 场景、版本、md5、绑核、租户、窗口、perf_scope/perf_label
  timing.txt            # 各采集器 start/end/rc（同一窗口，可核对是否同段对照）
  perf.data / perf.folded / observer_<pid 或 cpus 标签>.svg
  offcpu.txt / offcpu.svg
  mpstat.txt / pidstat.txt / iostat.txt
  topdown.txt / pmu.txt
  sysstat_start.txt / sysstat_end.txt   # 窗口起止两次快照，按 STAT_ID 求差
  gv_ob_thread.txt / sql_audit_top.txt（如 obclient 可用）
  status.txt            # perf/offcpu/mpstat/pidstat/iostat/ob_sysstat_start/ob_sysstat/gv_ob_thread/sql_audit 各通道 ok/missing；无有效样本时脚本 exit 非零
```

`scripts/collect_profile.sh` 每次要求 `--out` 为空目录（默认带时间戳新建）：非空直接拒绝，避免上次未运行采集器的旧产物被计作本轮成功。

## 11. 归因规则与常见误判（从 SKILL.md 下沉的完整口径）

归因顺序：

1. **先分进程**：observer、obproxy、sysbench 谁先饱和？observer 目标核是否打满？
2. **再分线程**：`sql_nio`、`worker`（含 `T*_L0_G0`）、`T*_ApplySrv`、`T*_ReplaySrv`、DagWorker/LSFreeze/`MINOR_EXE`
   各占多少？热点是否集中在某一类？
3. **再分机制**：
   - 锁/队列：`ObLatchWaitQueue`、`ObBucketLock`、`ObLatch::wrlock`、`memstore wait lock`；
   - 内存：`ObArenaAllocator::alloc`、`PageArena`、`ObTenantCtxAllocator`、`ObjectSet`；
   - 存储：微块解码、索引树预取、Bloom Filter、KV Cache、DAS 归并；
   - 算法：排序/字符比较、聚合、distinct；
   - 写入：ApplySrv/serialize、redo/PALF、冻结/合并；
   - 访存：L3 Bound、跨 NUMA/跨 P、cache miss/MPKI。
4. **用对照验证**：换绑核/去 obproxy/换盘/加 net 线程/换 LSE/NOLSE，看热点是否按预期迁移。
5. **区分事实与推断**：火焰图占比是事实；“因为 L3 Bound 所以慢”需要对照实验或 PMU 证据。

| 误判 | 纠正 |
| --- | --- |
| “observer CPU 没满，所以不是瓶颈” | 可能在等 IO/锁/网络；看 off-CPU 与延迟 |
| “worker 越多越好” | 轻查询/写场景瓶颈常在 sql_nio/ApplySrv/锁 |
| “CPU 占比高就是计算瓶颈” | 可能在自旋/latch/内存分配/跨 P 等待 |
| “L3 Bound 高就要换内存/加 cache” | 先看是否跨 cluster/NUMA、是否随机访存 |
| “火焰图里排序最宽，就是 OS 问题” | 排序来自 SQL/collation，属查询语义 |
| “QPS 下降 + L0_G0 峰值 ⇒ 冻结/合并” | `T*_L0_G0` 是租户 worker，CPU 随 QPS 天然波动；冻结/合并要看 DagWorker/LSFreeze/`MINOR_EXE` 峰值 + 周期性 QPS 下降 |
| “obproxy 一定是瓶颈，去掉就快” | 平台相关（920B 去掉更快，AMD 可能更慢） |
| “备节点 CPU 低所以没事” | 只写场景备节点有复制/回放开销 |
| “压测数字低就是服务端慢” | 先排除 sysbench CPU/结果解码/绑核 |

## 12. 热点分析报告模板

```
## 热点分析报告

### 场景与身份
- 场景/lua/是否经 obproxy、线程/时长
- 部署形态/规格/租户 Unit/primary_zone
- 绑核/NUMA、OB 版本、observer/obproxy md5、编译参数

### 性能对照
- TPS/QPS、成功率、avg/P95/P99、observer/obproxy/sysbench CPU

### 线程/阶段分解
- sql_nio / worker / ApplySrv / ReplaySrv / 冻结合并占比
- off-CPU 等待（IO/锁）、跨 NUMA、IO 延迟

### Top 热点
| 排名 | self% | children% | 函数 | 线程 | 机制 | 证据 |

### 微架构/数据库指标
- Backend/Memory/L3 Bound、IPC、L1/L2/L3 miss、MPKI
- GV$SYSSTAT：rpc/io/memstore 关键项与变化

### 归因
- 瓶颈进程/线程 → 机制 → 为什么（对照/实验）
- 与性能数字的对应关系

### 优化方向
- 按“部署/OS / 配置 / 编译 / 源码 / 查询语义”分类，给预期收益与验证方式
- 明确哪些是假设，哪些已被对照验证
```
