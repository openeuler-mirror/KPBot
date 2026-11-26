# 部署、连接与配置优化

## 1. 先定位，再调参

配置杠杆只对“配置相关”的瓶颈有效：

- 连接数/排队/ClientRead → 连接池、并发上限、客户端模式。
- 构建 OOM/慢 → memory_limit/batch/源表索引。
- 写入/同步 → 模式、edge_buffer、批量写。
- 首次查询慢 → 预热/连接池/加载优化（见源码候选）。
- 单查询 CPU 高 → 算法/编译/源码，不是 PG 参数。

没有 hotspot 证据就调参，通常只是把噪声当收益。

## 2. 连接与并发

### 2.1 事实

- 每个 PostgreSQL backend 一份 `thread_local Engine`。
- 前向 CSR/NodeStore 是 mmap 共享；反向 CSR/FilterIndex 是每 backend 私有堆。
- 因此连接数增加会放大私有内存，并可能超过 L3/CCL。
- 源资料的一组容量扫描（LDBC、96 逻辑核、csr_readonly）显示 QPS 在约 64 并发见顶，
  96 并发不再增长、延迟继续上升；这是该环境的观察，不是通用阈值。

### 2.2 建议

- 用连接池限制**活跃并发**，而不是把 `max_connections` 全开。
- 先做 1→N 容量扫描，找 QPS 峰值/拐点；生产把上限设在峰值附近。
- 客户端与服务端分开绑核，避免争抢。
- Python 客户端高并发优先用多进程/异步。注：`--mp` 是站点自建验证包装器的多进程模式（契约见 `../../pggraph-test-verification/references/benchmark-harness.md`，仓库不随附该包装器）；官方 run_benchmarks.sh 只有 cold/hot 单连接口径、无并发模式，并发验证需自备客户端。
- 若大量 backend `ClientRead`，先解决客户端提交速率，不是继续加连接。

### 2.3 容量扫描记录

| C | QPS | 每查询 avg | TOTAL | 客户端 CPU | 服务端每核 CPU | 每 backend Private |
|---|-----|------------|-------|------------|----------------|--------------------|
| 1 | | | | | | |
| 8 | | | | | | |
| 32 | | | | | | |
| 64 | | | | | | |
| 96 | | | | | | |

判读见 `pggraph-hotspot-analysis/references/topdown-and-contention.md`。

## 3. PostgreSQL 参数

起点（按内存/负载调整，不是通用最优）：

```conf
max_connections = 256
shared_buffers = '8GB'
work_mem = '64MB'
maintenance_work_mem = '1GB'
effective_cache_size = '24GB'
jit = off
track_activity_query_size = 4096
```

| 参数 | 影响 | 注意 |
|------|------|------|
| `shared_buffers` | PG 页缓存 | 与 `.pggraph` mmap 是两套内存；不要混算 |
| `work_mem` | 排序/哈希/hydrate 计划 | SQL JOIN 基线与 A/B 必须固定 |
| `maintenance_work_mem` | 建索引/VACUUM | 导入/重建时有用 |
| `effective_cache_size` | 优化器估算 | 影响 JOIN 基线计划 |
| `max_connections` | backend 上限 | 每 backend 私有内存叠加；连接池优先 |
| `jit`/`jit_above_cost` | JIT | 图查询主要是 C/Rust 调用；源资料实测关闭约 1%，需复测 |
| `synchronous_commit` | WAL 同步 | 只读基准可关；生产写入不得为跑分牺牲持久性 |
| `track_activity_query_size` | 诊断 SQL 长度 | 太小会截断 query，影响定位 |

A/B 时这些参数必须两侧一致；不要一边调 `work_mem` 一边比图引擎。

## 4. pgGraph GUC

| GUC | 调优方向 | 风险/边界 |
|-----|----------|-----------|
| `graph.memory_limit_mb` | 按数据集留余量（源资料 LDBC SF1 约 830MB，默认 2048） | 太小 OOM/降级只读；太大可能挤压 PG/其他进程 |
| `graph.build_batch_size` | 增大减少往返，但提高峰值内存 | 与 memory_limit 联动 |
| `graph.oom_action` | `error` 早失败 vs 降级只读 | 降级后写/同步行为受限 |
| `graph.persist_on_build` | 开启以便复用工件 | 关闭则重启需重建 |
| `graph.auto_load` | 开启自动 mmap | 关闭需显式 load；注意首次加载成本 |
| `graph.edge_buffer_size` | 增大容纳更多增量 | 超限转 read_only；内存增加 |
| `graph.query_freshness` | `error_on_pending` 保证不读陈旧 | 增加失败率/需要及时 apply_sync |
| `graph.mutable_enabled` | 需要写/增量时开启 | 写路径成本/复杂度高于只读 |
| `graph.enforce_tenant_scope` | 多租户安全 | 遗漏 tenant 会报错，需改查询 |

修改前记录原值；修改后重启/重载并验证。不要在生产直接改 `graph.data_dir` 指向不存在路径。

## 5. build 模式

| 模式 | 优点 | 代价 | 适用 |
|------|------|------|------|
| `csr_readonly` | 查询最快、结构简单 | 源表变更靠触发器+本地覆盖层；跨 backend 可见性有限；需 vacuum/maintenance 重建 | 读多写少、基准 |
| `mutable_overlay` | 支持增量、跨 backend 可见投影段 | 写放大、投影段/manifest/GC 复杂 | 持续写入、需要近实时 |

选择前问：

- 源表是否持续写？写入频率/延迟要求？
- 是否需要其他 backend 立即看到变更？
- 能否接受定期 vacuum/maintenance 的暂停/开销？
- 基准是否要求与生产同模式？A/B 必须固定模式。

## 6. NUMA / CCL / SMT

```bash
lscpu -e
numactl -H
numastat -p "${BACKEND_PID}"
```

建议：

- 服务端内存绑本地 NUMA 节点；客户端绑另一组核。
- 鲲鹏注意 L3 tag directory/CCL 结构；同一 CCL 内的核共享缓存，跨 CCL 访问更远。
- 优先使用物理核/primary 线程，避免 SMT 争用；用 48 个 primary 线程对照 96 逻辑核。
- 不要盲目单核：单核用于定位，不代表生产吞吐。
- 绑核属于 OS/亲和性层，具体命令交给 `kpbot-app-tuner` 的 `cpu-affinity-optimization`；
  本 skill 只记录绑定并保证 A/B 一致。

## 7. 加载/预热

- 每 backend 首次查询会 mmap + CRC + 重建反向 CSR + 反序列化 FilterIndex。
- 用持久连接/连接池复用 backend；基准先 warmup，再计时。
- 不要在计时阶段重启 PG 或新建大量连接，否则把加载成本算进结果。
- 若需要 cold 数据，单独报告，并与 hot 分开。

## 8. 配置优化检查清单

- [ ] 基线身份/数据集/口径已记录。
- [ ] 连接数/客户端模式/绑核两侧一致。
- [ ] PG 参数只改了一个，并记录原值/新值。
- [ ] pgGraph GUC 改动有明确目的和边界。
- [ ] build 模式与生产/基准一致。
- [ ] 预热策略一致，加载成本未混入稳态。
- [ ] 正确性门禁通过。
- [ ] 多轮 A/B，收益/回退/噪声有结论。
- [ ] 回滚方式明确（参数原值/旧 `.so`/旧工件）。
