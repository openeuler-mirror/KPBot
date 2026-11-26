# 基准框架：hot、throughput 与结果口径

## 1. 两套基准的定位

| 维度 | hot benchmark（`bench`） | throughput（`throughput`） |
|------|--------------------------|----------------------------|
| 连接数 | 1（持久连接） | N（可配置） |
| 阶段 | cold（可选）/ warmup / hot | warmup + 并发计时 |
| 计时 | wall_ms + server_execution_ms（EXPLAIN ANALYZE） | 原始 wall_ms |
| 结果 | row_count + result_checksum | 无 checksum/row_count |
| 统计 | min/median/p95/max | 每类型 avg/min/max、QPS |
| 用途 | 单查询稳定性、功能回归 | 吞吐/并发/排队/扩展性 |

**不要用 throughput 做正确性验证**；它只记录耗时和是否报错。

## 2. cold / warmup / hot

官方 `run_benchmarks.py` 的三阶段：

```
COLD:   每条查询前重启 PG/容器 → 新连接 → 执行一次
        清空 shared_buffers、backend-local graph state
        但不清 OS page cache；不重新 build
WARMUP: 单一持久连接，所有查询跑一遍，不记录
        触发 .pggraph mmap、反向 CSR 重建、计划缓存
HOT:    同一持久 backend，重复 N 轮（默认 10）
        记录每次 wall_ms/server_execution_ms/row_count/checksum
```

本地 `bench` 通常只跑 warmup + hot，跳过 cold，以加快迭代；报告要注明是否含 cold。

## 3. LDBC 查询集

| # | 查询 | API | 说明 |
|---|------|-----|------|
| 1 | status | `traverse` + `status` | 预热 + 图状态 |
| 2 | person_search | `search` | first_name contains "John" |
| 3 | friend_traversal_depth_1 | `traverse` | 高度数 person 的 1-hop |
| 4 | gql_knows_scalar | `gql` | 1-hop 标量投影 |
| 5 | gql_knows_hydrated_nodes | `gql` | 返回完整节点 |
| 6 | gql_knows_coordinates | `gql` | `hydrate := false` |
| 7 | sql_join_knows_equivalent | SQL JOIN | 独立基线 |
| 8 | person_content_neighborhood | `traverse` | 高度数 person 的 2-hop |
| 9 | forum_neighborhood | `traverse` | 繁忙 forum 的 2-hop |
| 10 | post_to_tag_path | `shortest_path` | post → tag |
| 11 | tag_to_tagclass_path | `shortest_path` | tag → tagclass |
| 12 | component_stats | `component_stats` | 全图连通分量 |

不同版本/文档可能写作 13 条；以 `report.json.sql_manifest` 为准。

已知限制：GQL 的 3 条查询可能因 `GQL result row cap exceeded (10000)` 失败；
throughput 脚本通常会跳过它们，报告要列出“排除项”，不要静默丢弃。

## 4. Panama 查询集

status、entity_search、traverse_depth_2、shortest_path、gql_one_hop_scalar、
sql_join_one_hop_equivalent、component_stats、largest_component、
gql_one_hop_hydrated_nodes、gql_one_hop_coordinates、traverse_one_hop_coordinates
（共 11 条，与官方 harness 的 Panama workload 一致；完备枚举以 report.json 的
sql_manifest 为准，与 §3 LDBC 同款口径）。

Panama 是单节点/单边大图，搜索/遍历的度数分布与 LDBC 不同；不要把 LDBC 的结论直接搬过去。

## 5. 参数与命令

> **`pggraph_verify.sh` 是站点自建包装器，不随本 skill/仓库交付**：官方脚本依赖 Docker
> （见 `../pggraph-build-deployment/references/data-import-and-registration.md` 的"本地包装器"
> 说明，把 `docker exec/cp/restart` 替换为本机等价命令）。本节描述的是该包装器的
> **接口契约**（子命令/参数/输出字段），供复刻或对照；官方参考实现见 pgGraph 源码树
> `sandbox/run_benchmarks.sh`。仓库内直接执行 `pggraph_verify.sh` 会得到 No such file。

```bash
# 只准备，不跑查询
pggraph_verify.sh prepare ldbc

# hot：10 轮
pggraph_verify.sh bench ldbc --iterations 10

# hot：20 轮
pggraph_verify.sh bench ldbc --iterations 20

# throughput：1 连接
pggraph_verify.sh throughput ldbc --iterations 20 --concurrency 1

# throughput：多连接，多进程避免 Python GIL
pggraph_verify.sh throughput ldbc --iterations 20 --concurrency 64 --mp
```

官方 Docker：

```bash
sandbox/run_benchmarks.sh ldbc --yes --hot-iterations 10 --build-mode csr_readonly
```

## 6. 结果字段

`report.json` 关键字段：

| 字段 | 含义 |
|------|------|
| `run_id` | UTC 时间戳，归档键 |
| `machine` | CPU/OS/容器等 |
| `datasets[].prepared` | archive/sha256/metadata/load_seconds/build_seconds/build_output/status |
| `sql_manifest` | 每条查询的 SQL + SHA256；复现依据 |
| `results[]` | phase/iteration/wall_ms/server_execution_ms/row_count/result_checksum/ok |
| `summary` | 按 `phase:query` 的 min/median/p95/max |
| `throughput_qps` | 成功查询数 / 总 wall 时间 |
| `total_avg_ms` | 各查询平均耗时之和（一轮的近似总和） |
| `type_averages` | 每查询 avg/min/max |

注意：

- `TOTAL` 是各查询平均之和，不是端到端一次请求的延迟；并发场景要结合 QPS。
- `wall_ms` 含客户端/EXPLAIN/checksum 开销；`server_execution_ms` 更接近服务端。
- cold 只有 1 次，p95 没有统计意义；不要用 cold 的 p95 做结论。
- `ok=true` 只表示没抛异常。

## 7. 客户端 GIL 与 `--mp`

Python threading + psycopg 在查询执行期间会释放 GIL，但查询构造/结果解析仍受 GIL；
高并发时可能限制吞吐并制造“服务端 CPU 不高”的假象。

判别：

- Python 客户端进程 CPU 接近 1 核。
- 并发增加但 QPS 平台，`pg_stat_activity` 中大量 backend 处于 `ClientRead`。
- 每查询 min 延迟不随并发增加（服务端执行没变），但 avg 大幅上升（排队）。
- 用 Little's Law 估算 GIL 等待：`W_total = C / λ`，与实测延迟比较。

处理：

- `--mp` 多进程（每 worker 独立进程，无 GIL）。
- 或 `asyncio + psycopg`、Go/Rust/C 客户端。
- 或降低并发到吞吐峰值附近，用连接池排队。

注意：`--mp` 是多进程客户端，不是多线程；报告要写清模式，不能和 threading 模式横比。

## 8. 容量扫描

固定数据集/查询集/轮次，改变并发：

```
C = 1, 2, 4, 8, 16, 32, 48, 64, 96, ...
记录：QPS、每查询 avg、TOTAL、失败数、客户端 CPU、服务端 CPU/内存
```

判读：

- 线性区：QPS 随 C 线性增长，效率接近 100%。
- 拐点：效率开始下降，出现排队/缓存争用。
- 峰值：QPS 最大；之后增加 C 可能 QPS 不增或下降、延迟继续上升。
- 生产建议：用连接池把活跃并发限制在峰值附近，而不是 `max_connections` 全开。

## 9. 数据集规模口径

源资料对 LDBC SF1 有两种量级记录：

- 约 3,181,724 节点 / 17,256,038 原始边 / 34,512,076 双向边 / CSR ~830MB。
- 约 100K 节点 / 500K 边（部分优化报告使用的图）。

这不是可以互换的同一口径，可能来自不同生成/过滤/子集。**任何性能对比必须绑定
`metadata.json`、`build_output` 和 `report.json` 的实际数字**；跨文档数字不直接横比。

## 10. 归档

```
${RESULTS_DIR}/<run_id>/
  report.json
  server.log / client.log（如有）
  env.txt（lscpu、numactl -H、pg_config、.so SHA256、git commit）
  notes.md（口径、异常、排除项、结论）
```

基线目录只读保留；候选 run 单独目录，不覆盖。
