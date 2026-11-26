# 预检与基线：mem0/milvus 案例全量事实

## 1. 预检快照（脱敏示例，实机具体值见内部记录；<host> aarch64 Kunpeng）

### 容器
| 容器 | 镜像 | 状态 | 归属 |
|---|---|---|---|
| `<mem0-runner>` | `<runner-image:flatidx-seed-v1>`（`sleep infinity`） | Up | 本任务（可重建） |
| `<他方容器A>` | `<外部镜像:tag>` | Up | **外部，保护** |
| `<他方容器B>` | `<外部镜像:tag>`（`bash`） | Up | **外部，保护** |

### 环境事实（脱敏示例，具体数值见内部记录）
| 项 | 值 |
|---|---|
| CPU | 鲲鹏 AArch64，多 vCPU，支持 sve/sve2（示例值） |
| 内存 | N GB RAM（示例值） |
| NUMA | 4 节点，含 2 个无内存节点（node2/node3 有 CPU 但 size=0；示例值，实际 CPU 区间略） |
| 磁盘 | 系统盘接近满；`/home` 与 `<docker-root>` 为大容量（示例值） |
| 基线版本 | mem0 2.0.14 · milvus 2.4.5 |

> **node2/node3 有 CPU 但无内存 → 区分 CPU 亲和与内存绑定**：仅把容器 `cpuset-cpus` 绑到 node2/node3 的核时，
> 默认内存策略会从有内存的 node0/node1 **远端分配**，产生远端访存代价。预检要**分别记录**：可用 CPU 区间
> （node2/node3 的核、可用但会远端访存）、内存节点（只有 node0/node1）、以及"绑空节点 CPU 的远端访存代价"。

### 事实→决策
- 存在无内存 NUMA 节点 → mem0 绑有内存节点 A、milvus 绑有内存节点 B（相对结论）。
- 系统盘接近满 → 一切落 `<workdir>` 与 `<docker-root>`。
- 他方常驻容器 → 禁止 kill/stop/restart。
- 有 sve/sve2 → 选 SVE 加速版向量库。

## 2. 负载形态
- LOCOMO trace-stress-replay：20 用户 / 20 并发 / 每用户预热 10000 条（共 20 万向量）。
- LLM/embedding 全部离线 mock（exactly-once 回放），真实路径只剩 `replay 客户端 → mem0 → milvus`。
- 四相位：seed（20 万）→ HNSW 索引构建 → ingestion → qa。前两相位恒定开销 10–20 分钟，只有 ingestion/qa 参与对比。

## 3. 内存规格临界判定（真实事故）
- 最初 `MILVUS_MEMORY_GB=4`：加载最后一个 ~1GB segment 时 `predictMemUsage` 估算 4217–4295MB > 4096MB →
  **milvus 拒载该 segment** → 索引就绪检查卡到 1800s 超时。**这是"预测超限拒载 + 就绪超时"**，不是 OOM。
- 另一个同为 4G 的历史运行却侥幸通过（segment 尺寸略有差异）。
- 结论：4G→8G（`MILVUS_MEMORY_GB=8`）。**措辞纪律：按观测记录"拒载 + 就绪超时"**；若要判定"发生 OOM"，
  需补充内核事件（`dmesg` 的 OOM killer 记录）或容器事件（容器被杀 / 退出码 137）佐证，不能把"预测超限拒载"
  直接写成"确定性 OOM"。临界规格是定时炸弹。

## 4. 基线四件套（3 轮中位）

规格：20 用户 / 20 并发 / warmup 10000 / `MILVUS_MEMORY_GB=8` / `MEM0_MEMORY_GB=4` / NUMA(mem0 node0, milvus node1, runner node1)。

### 逐轮原始值
| 轮次 | overall retrieval req/s | overall extraction req/s | ingestion elapsed s | qa retrieval 请求数 | qa elapsed s | qa retrieval 吞吐 req/s |
|---|---|---|---|---|---|---|
| R1 | 27.05 | 5.04 | 28.43 | 23040 | 838.72 | 27.47 |
| R2 | 27.18 | 5.07 | 26.41 | 23040 | 836.98 | 27.53 |
| R3 | 27.07 | 5.05 | 27.63 | 23040 | 838.72 | 27.47 |

> **QA 吞吐的复算口径**：`qa retrieval 吞吐 = qa retrieval 请求数 ÷ qa elapsed`。QA 请求数来自
> `mem0-api-replay-summary.json` 的 qa 相位 retrieval 请求计数（exactly-once 回放，每轮固定 23040），
> qa elapsed 来自同文件的 `phase_windows.qa`（start→end）。三个中位 = 27.47 req/s。**对比口径必须给这三个
> 可复算的原始值**，不能只甩一个 27.47 中位让调用方无从复算。

### 3 轮中位
| 指标 | 中位值 |
|---|---|
| elapsed_seconds | 865.37 |
| retrieval throughput | 27.07 req/s |
| retrieval p50 / p95 / p99 | 606.0 / 712.34 / 793.22 ms |
| extraction throughput | 5.05 req/s |
| qa retrieval 吞吐（对比口径） | 27.47 req/s |
| qa retrieval p50 | 605.51 ms |
| qa retrieval p99 | 772.58 ms |
| qa elapsed | 838.72 s |

> 注意：文档里 `overall` 27.07 与 `qa` 27.47 是两个口径；对比统一用 **QA 相位**。

## 5. 决策三元组范例
- D1 runner 生命周期：`clean.sh` 会删 `<mem0-runner>`，但它是 `sleep infinity` 空闲宿主、由 benchmark 原样重建 → 属正常生命周期，不违反安全线；真正保护的是他方常驻容器。
- D2 mixed vs barrier：bundle 发售版是 mixed（无 `--phase-mode`），基线/对比统一用 mixed。
- D3 内存 4→8GB：见 §3。

## 6. 基线产物完整性
一轮 = 6 文件齐全：`milvus-seed-summary.json`、`milvus-index-ready-summary.json`、`mem0-api-request-events.jsonl`、`mem0-api-replay-summary.json`、`trace-stress-performance.json`、`milvus-storage.json`。缺任一文件该轮作废（本 mixed 模式无 `ingest-performance.json`）。
