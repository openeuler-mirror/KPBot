---
name: locomo-benchmark-replay
description: >
  运行 LOCOMO trace-stress-replay 记忆基准并解读结果：四类容器、四相位
  （seed→索引构建→ingestion→qa）、结果文件字段与对比口径、复现命令与坑点。
  适用：要跑记忆系统的基线或候选镜像对比、要改 mem0/milvus 镜像做同环境对照、
  要解读 trace-stress-performance.json / replay-summary / request-events，或用 LOCOMO 记忆基准做 QA。
  不适用反例：只是采集性能数据（用阶段门控采集基建，独立 PR 待合入）；只是实现某个优化代码
  （用对应的具体优化 skill）；非记忆/Agent 负载的通用微基准（用对应测例框架）。
---

# LOCOMO trace-stress-replay 基准复现

## 何时使用

- 需要基线数字或候选镜像的对比数字。
- 需要改 `manifest.yaml` 换一个角色镜像（只改一处）。
- 需要读懂结果目录里的 6 个文件。

## 前置条件（benchmark bundle 是必需输入）

本 skill 只给"怎么跑/怎么读"，**不包含 benchmark 工程本身**。入口 `modules/benchmark/mem0_locomo.sh`、
`run-single.sh`、`manifest.yaml` 来自 mem0/milvus 联调项目的 benchmark 子仓（`AgentMemoryWorkload` fork 的
runner 演进，固定版本随子仓 manifest 钉死），需先解包到 `<workdir>/modules/benchmark` 才能执行下文命令。

解包后逐项检查（缺一即不能开跑）：

- 入口脚本存在：`modules/benchmark/mem0_locomo.sh`、`run-single.sh`、`clean.sh`。
- 四角色镜像已 pull 且 tag 与 `manifest.yaml` 一致（`docker images | grep -E '<mem0-server-image>|<milvus-image>|<mock-image>|<runner-image>'`）。
- 数据就绪：`--dataset locomo1` 对应的 trace 回放数据（`locomo1`）在 `--bundle-dir` 下可解析，否则 seed/replay
  相位会空转。
- 若拿不到该 bundle（未提供来源/固定版本），把"已解包的 bundle"列为外部必需输入并向使用者索要，不要臆造路径。

## 核心原则

1. **被测路径只有 `replay 客户端 → mem0 → milvus`**；LLM/embedding 全部离线 mock（exactly-once 回放），
   服务端成本确定可比。
2. **四相位**：`seed（20 万底库）→ HNSW 索引构建 → ingestion → qa`。**seed + 索引是恒定开销
   （10–20 分钟），只有 ingestion/qa 参与对比**。
3. **mixed 模式**：每用户"先 19 ingest 再 199 QA"串行、用户间并发，ingestion 与 qa 墙钟重叠；
   精确窗口以 `phase_windows` 为准，不要假设干净边界。
4. **四角色版本由 `manifest.yaml` 单一提供**，换镜像只改一处，不覆盖旧 tag。

## 步骤

### 1. 四类容器与职责

| 容器 | 职责 | NUMA |
|---|---|---|
| mem0 | 被测，uvicorn(2 worker) 跑 `compat_main:app`，双网络 | node0 |
| milvus | 向量库 standalone（HNSW/L2, M16 efC200），SVE 版 | node1 |
| mock | LLM + Embedding 回放 | — |
| runner | 离线编排器（挂 docker.sock 拉起 compose） | node1 |

### 2. 入口命令

```bash
cd <workdir>/modules/benchmark
./mem0_locomo.sh test trace-stress-replay --bundle-dir . --dataset locomo1
```

宿主机会自动 `exec scripts/runner_bootstrap.sh`：加载 runner 镜像 → 起 runner → 在容器内跑主编排。
`run-single.sh` 默认 `run 3 10000 top2`（轮数/warmup/归档标签），每轮开头 `clean.sh`。

### 3. 跑 N 轮 / 单轮

```bash
sh run-single.sh 3 10000 base    # 3 轮基线，标签 base
sh run-single.sh 1 10000 base    # 单轮（debug/采集用）
```

### 4. 换镜像（只改一处）

编辑 `<workdir>/modules/benchmark/manifest.yaml` 的对应角色 `tag`（如 `mem0.tag` / `milvus.tag`），
再跑入口命令；不要改脚本或覆盖旧 tag。

### 5. 解读结果

```
<run>/
├── milvus-seed-summary.json        # seed 汇总
├── milvus-index-ready-summary.json # 索引就绪（stable_polls）
├── mem0-api-request-events.jsonl   # 逐请求事件（防假提速锚）
├── mem0-api-replay-summary.json    # phase_windows / 请求计数
├── trace-stress-performance.json   # ★ 性能汇总
└── milvus-storage.json             # 落库校验
```

`trace-stress-performance.json` 顶层 `overall/ingestion/qa`，每档含 `elapsed_seconds`、
`retrieval.{throughput_req_s, latency.avg_ms/p50_ms/p95_ms/p99_ms}`、`extraction.*`、`memory_write.*`。

```bash
python3 - <<'PY'
import json
p=json.load(open("<run>/trace-stress-performance.json"))
for ph in ("ingestion","qa"):
    s=p[ph]
    print(ph, "elapsed", s["elapsed_seconds"],
          "retr_tps", s["retrieval"]["throughput_req_s"],
          "p50", s["retrieval"]["latency"]["p50_ms"],
          "p99", s["retrieval"]["latency"]["p99_ms"])
PY
```

## 门禁

- [ ] 四角色镜像 tag 来自 `manifest.yaml`，未覆盖旧 tag
- [ ] 只看 ingestion/qa 相位指标，排除 seed/索引恒定开销
- [ ] 精确相位窗口用 `phase_windows` 复核
- [ ] 请求计数与基线逐位对比（防假提速）
- [ ] **逐请求 request_id 对齐**：两次回放按 `request_id` 逐条核对 `success` 与 `response_result_count`
      （出现差异即暂停收益判断——检索每次都成功返回空集时，请求总数和成功数仍可与基线相同）
- [ ] 单轮只用于 debug/采集，结论用 N 轮中位

## 产物

- `<workdir>/modules/benchmark/results/<时间戳>-...-onedocker-warmup10000-<tag>/`
- 兜底日志 `<run.log>`（每轮 tee 覆盖）

## 参考

- `references/benchmark-ops.md`：`run-single.sh` 关键 env、四相位时序、3 个核心结果文件字段、
  8 条坑点（mixed 非 barrier、无 ingest-performance.json、clean.sh 删 runner、tee 覆盖等）。