# 线程、队列与归因模型

## 1. 线程命名（实测抓取）

| 名称 | 示例 | 职责 |
| --- | --- | --- |
| `sql_nio<N>` | `sql_nio0..11` | 收包、协议解析、投递请求 |
| worker | 名称随版本/池 | SQL 执行 |
| `T<tenant>_ApplySrv<N>` | `T<tenant>_ApplySrv1..4` | 提交回调、推进可见性 |
| `T<tenant>_ReplaySrv<N>` | `T<tenant>_ReplaySrv`、`T1_ReplaySrv10..17` | 备副本日志回放 |
| `T<tenant>_L0_G0` | `T<tenant>_L0_G0` | 租户 worker（处理该租户大部分请求；数量 2+min_cpu*cpu_quota_concurrency），CPU 随 QPS 自然波动 |
| `T<tenant>_MINOR_EXE` | `T<tenant>_MINOR_EXE` | minor merge |
| `T<tenant>_TenantMet` | `T<tenant>_TenantMet` | 租户元数据 |
| DagWorker / LSFreeze | 版本相关 | 转储/合并/迁移的执行线程 |
| `T1_*` | — | 系统租户（ID=1） |
| PX | 版本相关 | 并行执行 |
| IO | 版本相关 | 异步 IO |

> 线程名是诊断入口，不是稳定 API。版本升级后名称可能变化，以 `GV$OB_THREAD` 实际输出为准。

> `T*_L0_G0` 由 ObThWorker 的请求执行循环命名（`L%d_G%ld`），是租户 worker 线程组；转储/合并/迁移由独立的 DagWorker 线程组（LSFreeze 负责冻结动作）执行。不要把 L0_G0 的 CPU 峰值判为冻结/转储证据。

## 2. 请求队列

```
sql_nio 收包
  → ObMultiTenant::recv_request(tenant_id, req)
    → ObTenant::recv_request()
      ├─ req_queue_（quick/request 各三档：QQ_HIGH/QQ_NORMAL/QQ_LOW、RQ_HIGH/RQ_NORMAL/RQ_LOW；用户 SQL → RQ_NORMAL）
      ├─ multi_level_queue（按请求嵌套层级分 10 级，MULTI_LEVEL_THRESHOLD=2）
      └─ 资源组队列（按 Resource Group 分流）
        → worker 取出执行
```

要点：

- 4.x 不再沿用 3.x 的「HP/NP/LP 三级」笼统映射：`req_queue_` 有 quick/request 各三档共六个槽位，另有按请求嵌套层级分流的 10 级 `multi_level_queue` 与资源组队列。
- DDL 是低优先级（priority 10）且不走 `ObTenant::recv_request` 常规入口；该入口的高优先级留给 server task。用户 SQL 走 RQ_NORMAL。排查「DDL 挤占用户 SQL」不能按 3.x 的 HP 映射下结论。
- 队列长度、等待时间可从 `GV$OB_SQL_AUDIT` 的 `QUEUE_TIME` 观察。
- 轻查询高 QPS 时，队列/调度开销占比上升；`sql_nio` 单线程接近饱和说明网络侧先瓶颈。

## 3. sql_nio → worker 的交互

- `sql_nio` 负责读 socket、解析 MySQL 协议、构造请求；worker 负责执行。
- 950 的 insert 问题中，`recv_request`（sql_nio 路径）存在高时延 barrier/load；
去掉 obproxy 后背景流减少，热点下降。
- 多队列优化：`sql_nio` 与 worker 按奇偶/分组绑定到不同 CPU 半区，减少 L3 共享与调度开销；
实测 read/select +3%~5%、update +3%~10%，insert 需叠加 ApplySrv 优化。
- 判读：`sql_nio` CPU 高但 worker 不满 ⇒ 网络/协议/队列；worker 满 ⇒ 执行/存储。

## 4. ApplySrv 与 ReplaySrv

### 4.1 ApplySrv

```
事务提交 → 回调入队 → ApplySrv 处理 → 推进 max_applied_scn → 客户端可见
```

- insert 高负载时 ApplySrv 可接近饱和，worker 反而在等。
- 热点：`serialize` 相关（随机数组索引）、事务回调管理。
- 优化：增加 ApplySrv 工作线程（自适应），实测 insert +12%；不要只加 worker。

### 4.2 ReplaySrv

- 备副本回放 redo；写场景备节点 CPU/网络明显升高属正常。
- 只读场景备节点流量低；若只读备节点也很忙，检查是否主副本分布/路由异常。
- 回放落后会影响副本可用性与切主时间。

## 5. 冻结/转储/合并

| 线程 | 触发 | 影响 |
| --- | --- | --- |
| Freezer（Timer） | 每 2s 检查 `active_memstore` | 触发冻结（仅触发器，口径无误） |
| LSFreeze | 冻结动作 | 推进 memstore 冻结 |
| DagWorker | 转储、mini/minor/major merge、迁移 | 后台 CPU/IO |
| `T*_MINOR_EXE` | minor merge | 同上 |

判读：

- 冻结/转储/合并的执行证据看 DagWorker/LSFreeze/`T*_MINOR_EXE` 的 CPU 峰值 + 写场景 QPS 周期性下降。
- `T*_L0_G0` 是租户 worker，CPU 天然随 QPS 升降；「QPS 下降 + L0_G0 峰值 ⇒ 冻结/合并」是假阳性，应指向 worker 饱和/请求处理，不能作为冻结证据。
- 内存盘测试只写仍只有 3%~6% 提升 ⇒ 瓶颈不只在盘，也在 CPU/合并。
- 调 `freeze_trigger_percentage`/`memstore_limit_percentage` 属配置优化，见 optimization skill。

## 6. 锁与等待归因

| 证据 | 含义 | 下一步 |
| --- | --- | --- |
| `ObLatchWaitQueue`、`ObLatch::wrlock` 高 | latch 争用（KV Cache/表锁） | 看具体调用路径；不要盲目调自旋 |
| `memstore wait read/write lock time` 高 | MemTable 锁等待 | 热点行/长事务/并发写 |
| `lock_for_read_inner_` 高 | 读等待事务状态 | 长事务、未提交事务 |
| futex 占比高 | 用户态等待 | 结合 off-CPU 看谁在等 |
| `ObIOHandle::wait` 高 | 等 IO | iostat/await、redo/数据盘 |

## 7. cgroup 与绑核的线程视角

- observer 有上千线程；启动时分布全机 NUMA，内存就近分配。
- 启动后 `taskset` 只迁移线程 CPU，不迁移内存 ⇒ 跨 P。
- cgroup 限定 cpuset + memory node 时，线程与内存在域内分配，跨 P 减少。
- 观察：`/proc/<pid>/numa_maps`、`numastat -p <pid>`、`migratepages` 前后对比。

## 8. 线程归因模板

```
场景：insert 32U
observer CPU：目标核 CPU 占比（填实测值）
线程占比：ApplySrv / worker / sql_nio / ReplaySrv（备）分别填实测 self% 与 children%
off-CPU：等 IO / lock_for_read 分别填实测累计离核时长与等待栈
结论：瓶颈在 ApplySrv（提交回调/序列化），不是 worker/IO
验证：只加 ApplySrv 线程 → insert +12%，其他场景无劣化
```

## 9. 常见误判

| 误判 | 纠正 |
| --- | --- |
| “worker 没满所以服务端没瓶颈” | ApplySrv/sql_nio/IO 都可能先瓶颈 |
| “ReplaySrv 忙就是异常” | 写场景备副本回放忙是正常 |
| “线程越多越好” | 线程过多增加调度/L3 共享，反而下降 |
| “锁热点必须优化” | 实测锁占比很小（火焰图 self 不构成主要热点）时，锁微调无收益 |
| “cgroup 配了就一定生效” | 需核验 cpuset/memory 实际生效与线程落点 |
