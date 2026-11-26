# 进程、线程与状态模型

## 1. PostgreSQL 进程模型

```
postmaster
  ├─ backend（每个客户端连接一个进程）
  │    └─ thread_local ENGINE: RefCell<Engine>
  ├─ background worker（异步 build/maintenance）
  └─ 其他 PG 辅助进程
```

- 没有“pgGraph 服务进程”；图计算发生在 backend 内。
- backend 之间不共享 Rust 堆；共享的是 PG 共享内存（catalog/buffer）和 `.pggraph`
  的只读 mmap（OS page cache）。
- 一个连接同一时刻只执行一条 SQL（除非用 pipeline/异步协议），所以单 backend 内
  图计算、SPI hydrate 是串行的。

## 2. Engine 的生命周期

| 事件 | 行为 |
|------|------|
| backend 启动 | 尚无 Engine/图 |
| 首次 `graph.*` 调用 | 按需 mmap `.pggraph`、CRC/校验、重建反向 CSR、反序列化 FilterIndex |
| 后续调用 | 复用 thread_local Engine；`touch_loaded_graph` 快速路径 |
| 源表 DML | 触发器写 `_sync_log`；查询前 `apply_sync` 更新覆盖层 |
| 事务内 GQL 写 | `TxGraphDelta` 提供读己写；事务结束清空 |
| backend 退出 | Engine/私有堆释放；mmap 解除；共享 page cache 保留 |

因此：

- “每连接一份图索引”不是 bug，是进程隔离 + 派生状态的实现结果。
- 重启/重编译后首次查询慢，是加载/重建成本；预热可移出计时区间。
- 连接池复用 backend 可避免反复加载，但池中每个 backend 仍各有一份私有内存。

## 3. 内存归属

| 数据 | 归属 | 是否随连接数放大 |
|------|------|------------------|
| 前向 CSR/NodeStore/Resolution 主段 | mmap（共享物理页） | RSS 是，PSS 近似不 |
| 反向 CSR | backend 私有堆 | 是 |
| FilterIndex/registry | backend 私有堆 | 是 |
| BFS visited/depth/parent/frontier | 查询级 | 是（活跃查询） |
| 路径/String/JSON | 查询级 | 是（结果规模） |
| catalog/PG buffer | PG 共享内存 | 否（有上限） |
| 投影段/同步日志 | 文件/表 | 视模式 |

高并发前先估算：`活跃 backend 数 × 每 backend 私有内存` 是否超过 L3/CCL 或内存带宽
可承受范围。不要只看总 RSS（mmap 共享部分会重复计）。

## 4. SPI 与 hydrate

- SPI 在 backend 内同步执行；`Spi::connect` → `client.select` 阻塞到查询完成。
- hydrate 的 SPI 调用链：`GetCachedPlan/BuildCachedPlan → planner → ExecutorRun →
  ReadBufferExtended`；涉及计划构建、palloc、共享缓冲页锁。
- 同一 backend 内，hydrate 不能与 BFS 并行；但不同 backend 可以并行。
- `hydrate=false` 省掉 SPI/JSON；计数、内部组合、大遍历优先关闭。
- hydrate 受 PG MVCC/RLS/ACL 约束；图拓扑与源表行在同步窗口内可能不一致。

## 5. 后台 worker

- `build_async_graph`/`maintenance` 可由 pgrx 动态后台 worker 执行；携带
  database/user/graph 元数据恢复上下文。
- worker 与普通 backend 一样受连接/资源限制；构建期间会占 CPU/IO/临时空间。
- 构建锁/任务状态在 `_build_jobs`/`_maintenance_jobs`；中断后要清理残留任务。

## 6. 连接与并发

| 模式 | 特点 | 适用 |
|------|------|------|
| 单连接 | 无并发干扰，延迟最干净 | hot 基准、单查询优化 |
| 多连接/进程 | 真实吞吐，但每 backend 私有内存叠加 | throughput、容量扫描 |
| 连接池 | 限制活跃 backend，复用加载 | 生产 |
| `max_connections` 全开 | 大量空闲 backend 也占内存 | 通常不推荐 |

并发不是越高越好：

- 先做 1→N 容量扫描，找 QPS 峰值/拐点。
- 超过峰值后，QPS 可能不增、延迟继续上升。
- 客户端 GIL/提交速率可能先于服务端饱和；用 `--mp` 或异步客户端排除。

## 7. 线程本地状态与一致性

- `ENGINE` 是 thread_local：同一 backend 内多次调用共享；不同 backend 不共享。
- `resolution_delta`/`edge_buffer`/`TxGraphDelta` 是后端/事务本地；跨后端可见性取决于
  `csr_readonly` vs `mutable_overlay` 和投影段 manifest。
- `_sync_log` 是共享表；`apply_sync` 按 high-water mark 回放。
- 因此“在 A 连接 build，B 连接查询”通常可以（工件/共享 catalog），但覆盖层/事务本地
  状态不会自动跨 backend；需要 `mutable_overlay` 或重新加载/重建。

## 8. 观测点

| 观测 | 命令/SQL | 说明 |
|------|----------|------|
| backend 状态 | `pg_stat_activity` | active/idle/ClientRead/wait_event |
| 每 backend 内存 | `/proc/<pid>/smaps_rollup` | PSS/Private |
| Engine 是否加载 | PG 日志中的 `graph: loading from` | 每 backend 一次 |
| 工件 | `find $PGDATA -name main.pggraph` | 大小/时间 |
| 构建任务 | `_build_jobs`/`_maintenance_jobs` | 残留/锁 |
| 同步积压 | `_sync_log` 行数/high-water mark | 查询前回放成本 |
| 连接数 | `ss -tnp`/`pg_stat_activity` | 客户端/服务端匹配 |

## 9. 常见误判

- **“CPU 利用率低 = 服务端没干活”**：可能 backend 在 `ClientRead` 等客户端，或
  在等 DRAM/缓存；看 wait_event + 客户端 CPU + IPC。
- **“每 backend 内存 300MB，所以 96 连接要 28GB”**：mmap 共享部分不应按私有算；
  用 PSS/Private 区分。
- **“加连接就能加吞吐”**：私有内存/缓存/客户端可能先饱和；做容量扫描。
- **“status 为 0 就是没建图”**：status 是内存元数据；用 traverse 验证。
- **“重启后变慢是回归”**：首次加载/反向 CSR/CRC；预热后再比。
