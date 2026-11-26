# 指标与 Topdown 判读

## 1. GV$SYSSTAT 字段速查

下表为实测抓取中出现的指标（STAT_ID 以现场版本为准）。

| NAME | 含义 | 高值说明 |
| --- | --- | --- |
| `rpc packet in/out` | RPC 包数 | 跨机/跨 Zone 交互多 |
| `rpc packet in/out bytes` | RPC 字节数 | 大结果集/大事务 |
| `rpc net delay` | RPC 网络时延累计 | 网络或对端慢 |
| `rpc net frame delay` | 帧处理时延 | 协议层/调度 |
| `sql local count` | 本地执行 SQL | 正常 |
| `sql remote count` | 远程执行 SQL | DAS/跨机 |
| `sql distributed count` | 分布式执行 SQL | PX/多分区 |
| `row cache hit/miss` | 行缓存 | miss 高 ⇒ 点查变慢 |
| `block cache hit/miss` | 微块缓存 | miss 高 ⇒ 读 IO |
| `io read count/delay/bytes` | 读 IO | 延迟高 ⇒ 读盘慢 |
| `io write count/delay/bytes` | 写 IO | 延迟高 ⇒ redo/转储/盘 |
| `memstore read/write lock succ/fail` | MemTable 锁 | fail 高 ⇒ 争用 |
| `memstore wait read/write lock time` | 锁等待时间 | 高 ⇒ 热点行/长事务 |
| `clog trans log total size` | redo 总量 | 写放大/事务大 |
| `max memory size` / `memory usage` | 租户内存 | 接近上限 ⇒ 洗缓存/失败 |

采集 SQL 见 `profiling-playbook.md` §8.1。`scripts/collect_profile.sh` 在窗口起点落 `sysstat_start.txt`、窗口终点（perf 后处理之前）落 `sysstat_end.txt`；按 STAT_ID 对两份快照求**增量**而非看时点绝对值，起止时间记入 `timing.txt`/`status.txt`。

## 2. 用增量解释性能

```
Δmetric / Δtime  → 每秒 RPC/IO/redo
与 TPS 对照       → 每次事务的 RPC/IO 次数
```

例：

- insert TPS 下降但 `io write count` 不变、`io write delay` 上升 ⇒ 盘变慢/争用。
- read TPS 下降且 `block cache miss` 上升 ⇒ 缓存不足/工作集变大。
- `sql remote count` 上升 ⇒ 路由变化/主副本漂移/索引变化。

## 3. Topdown 指标树

```
Retiring          指令正常退休（真正做事的比例）
Frontend Bound    取指/解码受限
  ├─ Fetch Latency（icache/itlb/flush）
  └─ Fetch Bandwidth
Bad Speculation   分支预测失败/机器清空
Backend Bound     后端执行/访存受限
  ├─ Core Bound（执行端口、除法、FSU）
  └─ Memory Bound
       ├─ L1 Bound（DTLB、misalign、hazard）
       ├─ L2 Bound（snoop、arb、pipeline）
       ├─ L3 Bound
       ├─ Mem Bound（local/remote DRAM）
       └─ Store Bound
```

判读规则：

| 组合 | 结论 | 优化方向 |
| --- | --- | --- |
| Backend 高 + Memory 高 + L3 高 | 访存延迟受限 | 提升局部性、减少随机访问、绑 NUMA、数据结构 |
| Backend 高 + Core 高 | 计算/端口受限 | 算法、指令、并行度 |
| Frontend 高 + icache miss 高 | 代码体积/热函数分散 | 热函数重排、LTO、减少模板膨胀 |
| Bad Speculation 高 | 分支不可预测 | 分支友好代码、PGO |
| IPC 低 + MPKI 高 | 访存瓶颈 | 同上 |

OB 实测：

- 只读（950）：Backend 高、Memory 占大头、L3 高；920B 同为 Backend/Memory 主导，L3 相对更低。
- 只写（920B）：Backend 高、Memory 高、L3 中；另有 off-CPU 等 IO。
判读以“高/中/低”与同口径对照为准；原实测百分比已脱敏，不要拿定性结论当阈值。
- 编译器优化后：前端 Bound 下降，后端 Bound 相对加剧（因为前端不再是瓶颈）。

## 4. Cache/TLB 指标

| 指标 | 含义 | 高值含义 |
| --- | --- | --- |
| `l1i/l1d missrate` | L1 指令/数据 miss 率 | 代码/数据局部性差 |
| `l2i/l2d missrate` | L2 miss 率 | 工作集超 L2 |
| `l3 missrate`（uncore） | L3 miss 率 | 工作集超 L3 或跨 cluster |
| `l1d/l2d MPKI` | 每千指令 miss 数 | 访存压力 |
| `dtlb walk rate` | 页表遍历 | 大页/页表压力（64K 页可改善） |

实测：950 只读 L1D MPKI 56.7、L1D miss 率高于 920B；920B MPKI 27.19。950 访存更重（miss 率原值已脱敏，按高低判读）。

## 5. 系统指标

| 指标 | 命令 | 判读 |
| --- | --- | --- |
| CPU idle | `mpstat -P ALL` | 目标核 idle 很低（个位数）与明显偏高（10%+）的差异会解释性能差 |
| 每线程 CPU | `top -H`、`pidstat -t` | 找 sql_nio/ApplySrv/worker |
| 上下文切换 | `pidstat -w` | 过多 ⇒ 锁/调度 |
| IO await/util | `iostat -x` | 写场景看 redo 盘 |
| 网络 | `sar -n DEV`、`ss -s` | 带宽/连接/重传 |
| NUMA | `numastat -p <pid>`、`/proc/<pid>/numa_maps` | 跨节点内存比例 |

## 6. 区分“计算/访存/IO/锁”

```
CPU 满 + IPC 低 + L3 高        → 访存
CPU 满 + IPC 高 + Core 高      → 计算
CPU 不满 + off-CPU 等 IO 高    → IO
CPU 不满 + off-CPU 等锁高      → 锁/事务
CPU 不满 + 客户端 CPU 满       → 客户端
CPU 不满 + 网络/队列高         → 网络/协议
```

## 7. 报告中的指标呈现

- 同时给绝对值与增量（例如 30s 内 `io write delay` 增加多少、对应 TPS）。
- Topdown 给关键分支（Backend/Memory/L3/Core），不要只给一个总分。
- 火焰图给 Top 10 self + Top 5 children，并标注线程。
- 任何百分比都带“环境/场景/版本”，避免跨文档横比。
