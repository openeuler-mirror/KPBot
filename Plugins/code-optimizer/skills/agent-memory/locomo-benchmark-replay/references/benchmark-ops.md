# LOCOMO benchmark 运维手册（标定案例）

## 1. bundle 布局

```
modules/benchmark/
├── mem0_locomo.sh        # 主编排入口
├── run.sh / run-single.sh / clean.sh
├── manifest.yaml         # ★ 四角色 tag 单一来源
├── runner/               # seed_milvus_baseline.py / replay_openclaw_mem0_trace.py / wait_milvus_index_ready.py
├── scripts/              # generate_scaleout_compose.py / runner_bootstrap.sh / lib_manifest.sh
├── constants/phase_markers.sh
└── results/
```

## 2. 四类容器

| 容器 | 镜像/职责 | 网络 | NUMA |
|---|---|---|---|
| mem0 | `<mem0-server-image:${tag}>`，uvicorn(2 worker)，add/search/delete，SQLite WAL | agent_net+storage_net | node0 |
| milvus | `<milvus-image:sve-runtime-v1>`，HNSW/L2 M16 efC200 | storage_net | node1 |
| mock | LLM + Embedding 回放 | agent_net | — |
| runner | 离线编排器，挂 docker.sock | host | node1 |

## 3. 入口与 env

```bash
./mem0_locomo.sh test trace-stress-replay --bundle-dir <bundle> --dataset locomo1
```

宿主 exec 基准子仓自带的 `runner_bootstrap.sh`（位于 `modules/benchmark` 下，本仓不随包分发）：load runner 镜像 → 确保 runtime 卷 → 起/复用 runner →
`docker exec` 在容器内跑主编排。`RUNNER_SCRIPT_HOST` 让 runner 用宿主最新脚本（改 `mem0_locomo.sh`
即生效，无需重建 runner）。

关键 env（`run-single.sh` 默认 `run 3 10000 top2`）：

| 变量 | 值 |
|---|---|
| SCALE_OUT_MODE / PIN_CPUS | true / true |
| MEM0/MILVUS/RUNNER_NUMA_NODE | 0 / 1 / 1 |
| TRACE_REPLAY_USERS / PARALLEL | 20 / 20 |
| WARMUP | 10000 |
| MEM0_CPU/MEM/WORKERS | 2 / 4G / 2 |
| MILVUS_CPU/MEM | 2 /（基线 8G） |
| MOCK_REPLAY_CONCURRENCY/BACKLOG/LOCK_STRIPES | 64/1024/64 |

换 mem0 镜像只改 `manifest.yaml` 的 `mem0.tag`。

## 4. 四相位时序

1. seed：每用户插 10000 条 1024 维向量（共 20 万），产 `milvus-seed-summary.json`。
2. HNSW 索引构建+就绪：轮询 index（HNSW/L2/M16/efC200/load=100/state=Finished/pending=0/indexed≥total），
   连续 3 次 stable、1800s 超时。产 `milvus-index-ready-summary.json`。
3. replay ingest/qa：20 lane，每 lane 19 ingestion → 199 QA，用户间并发。产 `request-events.jsonl` + `replay-summary.json`。
4. 汇总+校验：`trace-stress-performance.json` + `milvus-storage.json`。

## 5. 结果文件

- `trace-stress-performance.json`：overall/ingestion/qa，`elapsed_seconds`、`retrieval.{successful_requests,error_requests,busy_wall_seconds,throughput_req_s,latency.avg_ms/p50_ms/p95_ms/p99_ms}`、`extraction.*`、`memory_write.*`、`client_http.*`、`per_mem0.*`。
- `mem0-api-replay-summary.json`：`phase_windows.{ingestion,qa}`、`requests.{attempted,successful,failed}`、每用户计数。
- `mem0-api-request-events.jsonl`：逐请求 `request_id/user_id/phase/operation/elapsed_ms/status_code/success/response_result_count`。

## 6. 复现命令

```bash
cd modules/benchmark
sh run-single.sh 3 10000 base   # 3 轮基线
sh run-single.sh 1 10000 base   # 单轮
```

- 完整日志 `<run.log>`（每轮 tee 覆盖）。
- 原始结果 `<docker-root>/volumes/<runtime-volume>/_data/results/<ts>-.../`。
- 归档 `modules/benchmark/results/<ts>-...-onedocker-warmup10000-<tag>/`。

## 7. 坑点

1. **mixed 非 barrier**：未设 `TRACE_REPLAY_PHASE_MODE`（默认 mixed），20 用户各自 19 ingest→199 QA 串行、用户间并发，两相位墙钟重叠；
   精确窗口看 `phase_windows`；相位隔离（barrier）另见 `benchmark-phase-isolation`（独立 PR 待合入）。
2. **无 `index_settle`（mixed/replay 模式）**：唯一 `stable_polls` 来自 seed 后的 index 等待；
   barrier 模式会额外产出 `index_settle` 进 summary（见 `benchmark-phase-isolation`，独立 PR 待合入）。
3. **无 `ingest-performance.json`**：本模式产出 `trace-stress-performance.json`。
4. **clean.sh 删 runner/卷**：每轮开头执行。**原始结果保存在 runtime 卷里**（`<docker-root>/volumes/<runtime-volume>/_data/results/...`，见 §6），
   clean 删卷会连带删掉尚未归档的结果。启动多轮回放前，先列出清理目标（卷名 + 归属容器），核实：① 该卷由本任务
   独占（非共享、非他人容器挂载）；② 所需结果已归档到 `modules/benchmark/results/...`（宿主机，不在 runtime 卷内）。
   归属不明时**停止清理**，不要盲删。runner 本身可重建（sleep infinity 空闲宿主），确认是本任务的即可。
5. **单轮 10–20 分钟不可压缩**：只看 ingest/qa 相位，多轮取中位。
6. **tee 每轮覆盖**：要留上一轮日志需提前另存。
7. **RUNNER_SCRIPT_HOST 决定生效脚本**：改宿主脚本即生效。
8. **归档名带 `onedocker-warmup<WARMUP>-<tag>`**：靠标签区分避免覆盖。