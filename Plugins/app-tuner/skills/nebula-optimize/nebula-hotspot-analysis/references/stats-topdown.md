# HTTP 统计与 Topdown 微架构

## 1. HTTP /stats 端点

| 服务 | 常见端口 | 内容 |
|------|---------|------|
| graphd | 19669（3.8.0 默认；19670 是已弃用 HTTP/2，不要当默认；或配置的 `ws_http_port`） | 查询、RPC、会话、执行器、错误 |
| storaged | 19779（或配置的 `ws_http_port`） | get_neighbors、scan、写入、Raft、KV |
| metad | 19559（或配置的 `ws_http_port`） | 心跳、Raft |

```bash
curl -s http://127.0.0.1:19669/stats | head -40
curl -s http://127.0.0.1:19779/stats | head -40
```

先看实际输出格式；指标名随版本变化，不要硬编码不存在的名字。

采集方法：

- 压测稳态后，1s 一次采 5 次，取稳定窗口。
- 记录窗口内 QPS；否则速率指标无法解释。
- 多实例分别采，或采代表实例并注明。
- 可用 `?format=json`（如支持）便于脚本解析。

## 2. graphd 关键指标

| 指标族 | 含义 | 用途 |
|--------|------|------|
| `num_queries` / `num_slow_queries` | 查询数/慢查询 | 负载与长尾 |
| `num_rpc_sent_to_storaged` / `num_rpc_sent_to_metad` | 发往 storaged/metad 的 RPC（3.8.0 分开计数） | 判断每查询 RPC 次数 |
| `query_latency_us` | 端到端查询延迟 | 与 k6 latency 对照 |
| `num_active_sessions` / `num_opened_sessions` | 会话 | 并发与泄漏 |
| `num_query_errors` | 错误 | 成功率下降 |
| executor 相关 | 执行器耗时/队列 | DAG/调度 |

## 3. storaged 关键指标

| 指标族 | 含义 | 用途 |
|--------|------|------|
| `num_get_neighbors` / `get_neighbors_latency_us` | 邻居查询数/延迟 | 读路径核心 |
| `num_get_prop` / `num_scan_vertex` / `num_scan_edge` | 属性/扫描 | 查询形态 |
| `num_add_vertices` / `num_add_edges` | 写入数 | 写吞吐 |
| `num_delete_vertices` / `num_delete_edges` | 删除 | 维护 |
| KV `num_...` | RocksDB 读写/缓存 | 存储层 |
| Raft `...` | 日志/复制/延迟 | 写入抖动/选举 |

## 4. 时间消耗分解

用 stats 把一次查询拆成：

```
端到端延迟
├─ graphd 队列/调度
├─ graphd 解析/校验/优化/执行
├─ graphd → storaged RPC（网络 + 序列化 + 排队）
├─ storaged 处理（RocksDB + 组装 + 序列化）
└─ 响应返回/客户端解码
```

**口径边界**：/stats 返回时间窗内的服务级聚合值。多查询并发、单查询跨分区并行发 RPC 时，
“graphd 查询延迟 − storaged 平均处理延迟”无法还原单次查询的排队、网络与客户端解码耗时——
上图分解是**可观测量 + 未测阶段**的近似：graphd/storaged 两段可测，RPC 排队/网络/客户端解码
合在残差里。需要单次分解时，用请求级追踪（slow query 日志、query profiling 输出）或客户端
分段计时（发送/首响应/收完）作依据，不要把聚合差值当单请求结论。

实测特征（2g+2s，SF10）：

- **所有读场景的 RPC 同步开销占大头（约 69–91%）**；Go1 最严重（约 91%）。
- storaged 内部处理延迟（如 Go1 的 get_neighbors）通常远小于端到端延迟。
- 查询复杂度增加，瓶颈从 graphd 的队列迁移到 storaged 的 RocksDB。
- 写入场景的时间在 nGQL 解析 + WAL fsync + compaction，而不是“写 RocksDB”本身。

判读：

- `num_rpc_sent_to_storaged.sum.60 / num_queries.sum.60` ≈ 每查询发往 storaged 的 RPC 次数
  （必须同窗口、同实例；分母为 0 的零查询窗口无意义，跳过不要除零）；Go1≈1，Go2≈2–11，Go3≈更多。
- 端到端延迟高而 storaged 处理延迟低 → 瓶颈在排队/网络/客户端。
- storaged 处理延迟高 → 看 RocksDB/结果组装/内存。
- 延迟分布看 p50/p99；均值会掩盖长尾。

## 5. Topdown 指标

| 指标 | 含义 | 高值说明 |
|------|------|---------|
| Front-End Bound | 取指/解码受限 | icache、分支、代码布局、指令流 |
| Back-End Bound | 执行/访存受限 | 计算或内存 |
| ├ Memory Bound | L1/L2/L3/DRAM | 数据局部性差、cache miss |
| └ Core Bound | 执行单元/依赖链 | 计算密集、串行依赖 |
| Retiring | 有效退休指令 | 高说明流水线利用好 |
| Bad Speculation | 分支预测失败/错误路径 | 分支多、数据依赖 |
| IPC | 每周期指令数 | 低 IPC 通常有 stall |
| L1I/L1D/L2/L3 miss | 缓存未命中 | 判断访存局部性 |
| Branch miss | 分支失败 | 判断控制流 |

**采集前提**：上表 Front-End/Back-End/Memory/Core Bound 等 Topdown 桶需要平台的 Topdown
指标组支持（鲲鹏 devkit topdown、perf 的 tma 指标组等）。采集前先用
`perf list | grep -iE 'topdown|tma'`（或 devkit 对应命令）确认本机支持的指标组，并按确认的
命令与预期输出采集。仅支持通用事件（cycles/instructions/cache-misses/branch-misses）时只能
算 IPC 与 miss 率，**不得**推算 FE/BE/Memory Bound 占比——按实际可得的指标报告，本机不支持
的桶明确写“不支持，未采集”。

实测摘要：

| 场景 | graphd IPC | 主要特征 |
|------|-----------|---------|
| Go1Step | ~0.52 | Back-End/Memory Bound 高，futex 导致低 IPC |
| Go2Step | 中等 | RPC 等待 + 内存 |
| Go3Step | ~2.6 | graphd 计算密度高（但瓶颈在 storaged） |
| FSP | 中等 | 哈希/字符串/内存 |
| Insert | 中等 | 前端/解析，storaged I/O bound |

注意：

- IPC 低不等于“计算重”；futex/等待/内存 stall 都会拉低 IPC。
- Topdown 是 CPU 侧视角；I/O bound 场景（Insert）看磁盘 await/%util。
- 采样核要覆盖热点线程；采到空闲核会得到“很闲”的错误结论。

## 6. 缓存未命中

实测所有场景 L3D miss 约 30–46%，说明 Nebula 的访问模式对 L3 不友好：

- 图遍历的随机访存；
- Value/字符串/容器对象多；
- RocksDB block/index 访问；
- 多实例/多线程竞争。

优化含义：

- 增大 block cache / 改善数据局部性（SoA、flat_map、减少对象）比“换更大 L3 的 CPU”更可控。
- 预取（prefetch）对规律访存有效，对随机指针追逐收益有限。
- NUMA 绑定错误会显著放大 miss 与延迟。

## 7. 优化优先级（实测建议）

| 优先级 | 场景 | 方向 | 预期 |
|--------|------|------|------|
| P0 | Go1Step | 减少单队列 futex（多 graphd/少 worker） | +30–50% QPS（部署级） |
| P1 | 所有读 | 改善数据局部性（容器/对象/缓存） | +10–15% |
| P1 | Insert | 预编译/批量 + WAL/compaction 调优 | +20–30%（写） |
| P2 | Go3Step | RocksDB 批量/扫描优化 | +10–20% |
| P2 | FSP | 减少 Value 拷贝/BFS 容器 | +5–15% |
| P3 | Go2Step | 增加 storaged/读池 | +5–10% |

这些是方向性估计；实际收益必须在目标环境用 A/B 验证（见 `nebula-test-verification`）。

## 8. 采集脚本示例

```bash
#!/usr/bin/env bash
# 采 5 次稳态 stats，保存原始文件（端口从实例配置确认，默认 graphd 19669 / storaged 19779）
# 与 collect_profile.sh 同口径：目录已存在即拒绝（防止覆盖/混入上轮样本），默认带时间戳
OUT="${1:-stats_$(date +%Y%m%d_%H%M%S)}"
if [ -e "$OUT" ]; then
  echo "ERROR: 输出目录已存在（防覆盖上轮样本，每轮独立目录）: $OUT" >&2
  exit 3
fi
mkdir -p "$OUT"
for i in 1 2 3 4 5; do
  curl -s --proto '=http,https' --max-time 5 -- "http://127.0.0.1:19669/stats" > "$OUT/graphd_${i}.txt"
  curl -s --proto '=http,https' --max-time 5 -- "http://127.0.0.1:19779/stats" > "$OUT/storaged_${i}.txt"
  sleep 1
done
```

分析时：

- 比较 5 次之间的稳定性；波动大说明窗口不稳。
- 用同一时间窗口的 QPS 做分母，算 RPC/查询、延迟/查询。
- 把 stats 结论与 perf 火焰图对照；两者矛盾时优先相信可复现的对照实验。

## 9. 常见误区

| 误区 | 纠正 |
|------|------|
| “storaged 处理快，所以 storaged 不是瓶颈” | 可能 RPC/排队/客户端是瓶颈 |
| “graphd CPU 低，所以 graphd 没问题” | 可能都在等 RPC |
| “QPS 高就是好” | 成功率/延迟/长尾同样重要 |
| “延迟均值下降就是优化成功” | 看 p50/p99 与分布 |
| “Memory Bound 就加内存” | 可能是 cache miss/数据结构 |
| “IPC 低就是代码差” | 可能是 futex/等待/内存 stall |
| “指标名固定不变” | 随版本变化，先看实际输出 |
