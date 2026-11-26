# A/B 协议与判定

## 1. 目标

回答两个问题：

1. 优化是否改变了**功能行为**？（正确性门禁）
2. 性能变化是否**超出噪声**、在目标场景可保留？（收益判定）

## 2. 冻结条件

A/B 两侧必须一致：

| 维度 | 要求 |
|------|------|
| 数据集 | 同一份数据、同一规模、同一 `metadata.json`/SHA256 |
| 图构建 | 同一 build 模式（csr_readonly/mutable_overlay）、同一注册集 |
| 工件 | 同一工件或都重建；不要一边旧工件一边新工件 |
| pgGraph | 只允许目标变量不同；其余版本/commit 一致 |
| PostgreSQL | 同一大版本/配置（shared_buffers/work_mem/jit/max_connections） |
| 客户端 | 同一脚本、同一连接模式、同一 `--mp`/threading、同一参数 |
| 查询 | 同一 SQL、同一 limit/order/seed、同一 `hydrate` |
| 绑核 | 服务端与客户端分别固定，且两侧一致 |
| 预热 | 同一 warmup 轮数；排除首次 mmap/反向 CSR 加载 |
| 轮次 | 同一 iterations；至少 3 次独立 run 或 ≥20 次 hot iteration |

任何一项不同，结论只能叫“观察”，不能叫“A/B 收益”。

## 3. 运行顺序

推荐交错，减少时间漂移：

```
for round in 1..R:
  baseline  (旧 .so / 旧参数)
  candidate (新 .so / 新参数)
```

每轮之间：

- 重启 PG 或至少重建连接，清理 backend 本地 Engine。
- 确认图工件/数据未变。
- 记录 `.so` SHA256、PG 配置、客户端参数。

不要先跑完所有 baseline 再跑 candidate；机器温度/负载/缓存可能漂移。

## 4. 指标

### 4.1 单查询

- `median`、`p95`、`min/max`（hot）。
- `server_execution_ms`（如可获得）。
- `row_count`、`result_checksum`。
- 失败数/错误类型。

### 4.2 汇总

- `TOTAL`（各查询平均之和）仅作参考；不要把它当端到端延迟。
- `throughput_qps`（多连接）。
- 每查询相对变化：`(candidate - baseline) / baseline`。
- 离散度：IQR 或 median 的置信区间；至少报告多次 run 的范围。

### 4.3 资源

- 服务端 CPU（按 NUMA/核范围）、IPC、cache miss、内存。
- 客户端 CPU（判别 GIL/客户端瓶颈）。
- 每 backend 私有内存（PSS/Private）。
- 失败/重试/超时。

## 5. 噪声判定

建议：

- 单查询收益 **<5%** 视为噪声，除非多次 run 稳定且 CI 不跨 0。
- 同一查询在不同 run 的 median 波动 >10%：先排查环境/客户端/数据，不要下结论。
- 负收益 >5%：回滚或给出明确解释（例如为其他场景让路）。
- 场景间权衡：不能用一个查询的提升掩盖另一个查询的回退；按场景分别判定。

统计学上没有万能阈值；关键是**同口径、多轮、报告离散度**。

## 6. 正确性门禁

顺序：

1. 单测/冒烟通过。
2. SQL JOIN/递归 CTE/不变量对账通过。
3. A/B checksum：同查询、同 limit/order/seed；不一致必须解释。
4. 失败查询数不增加；GQL 超限等已知限制单独列出。

任一门禁失败：

- 不看性能；
- 回滚或修复；
- 记录失败证据（查询、seed、row_count、checksum、错误）。

## 7. 典型陷阱

| 陷阱 | 后果 | 避免 |
|------|------|------|
| 用 throughput 比正确性 | 不知道结果对不对 | 用 hot + Oracle |
| 只比 checksum | 原始 bug 被保留 | 加 SQL JOIN/不变量 |
| 不同 limit | row_count/checksum 不可比 | 固定 SQL/limit/order |
| 不同 seed | 结果集不同 | 固定 seed 并记录 |
| 不同 build 模式 | 拓扑/同步语义不同 | 固定模式 |
| 不同连接数 | 排队/缓存效应混入 | 同口径，或明确做容量扫描 |
| 没预热 | 首次 mmap/反向 CSR 被算入 | warmup + 持久连接 |
| 客户端 GIL | 服务端 CPU 低、QPS 平台 | `--mp`/异步/多进程 |
| 只跑一轮 | 噪声被当收益 | 多轮/交错 |
| 混用数据集规模 | 数字不可比 | 绑定 metadata/build_output |

## 8. 报告模板

```markdown
## 结论
- 功能：通过/失败（证据）
- 性能：保留/回滚/噪声（每场景）
- 限制：数据集、连接模式、已知失败

## 身份
- pgGraph: <version/commit>, graph.so sha256: <...>
- PostgreSQL/Rust/pgrx:
- 编译参数:
- 数据集/规模/build_output:
- 图模式/工件:

## 正确性
- 单测/冒烟:
- SQL JOIN Oracle（seed/查询/结果）:
- 不变量:
- checksum A/B（查询/limit/row_count）:

## 性能
| 查询 | baseline median | candidate median | Δ% | p95 Δ% | 备注 |
|------|-----------------|------------------|----|--------|------|

- TOTAL/QPS:
- 轮次/离散度:
- 资源:

## 复现
- 命令:
- report.json 路径:
- 环境文件:
```

## 9. 保留/回滚判据

保留：

- 正确性门禁全过；
- 目标场景收益稳定 >5%，且无其他场景显著回退；
- 资源/内存/启动开销可接受；
- 有回滚方式与记录。

回滚：

- 功能失败或结果与 Oracle 不一致；
- 收益在噪声内，或只在高并发/特定配置下出现；
- 负收益/内存暴涨/启动变慢不可接受；
- 无法解释的 checksum 变化。

记录负结果同样重要：防止后来者重复踩坑。
