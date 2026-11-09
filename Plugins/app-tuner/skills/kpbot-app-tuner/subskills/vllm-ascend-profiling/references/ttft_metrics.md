# TTFT 插桩指标清单

> 来源：vllm fork `rosinL/vllm@5d1d24c`（`[TTFT_DEBUG]` 前缀）+
> vllm-ascend 补丁 `vllm-project/vllm-ascend@7d73d9d63`（`[TTFT]` 前缀）
>
> 生成时间：2026-09-29　批次：20260929-135752

---

## 一、vllm fork 插桩（`[TTFT_DEBUG]`）

### 1.1 `[TTFT_DEBUG][Tokenizer]` — 分词阶段（请求级）

日志格式：

```
[TTFT_DEBUG][Tokenizer] render=<秒> tokenize=<秒> n=<数>
```

源码位置：`vllm/v1/renderers/base.py`

| 字段         | 单位 | 含义                | 聚合到 metrics.json   | 备注 |
| ------------ | ---- | ------------------- | --------------------- | ---- |
| `render`   | s    | prompt 模板渲染耗时 | ✅ tokenizer.render   |      |
| `tokenize` | s    | 分词耗时            | ✅ tokenizer.tokenize |      |
| `n`        | 个   | 本批请求数          | ✅ tokenizer.n（int） |      |

---

### 1.2 `[TTFT_DEBUG][EngineCore]` — 调度+执行核心（step 级）

日志格式：

```
[TTFT_DEBUG][EngineCore] load=<秒> forward=<秒> save=<秒>
  sched_hash=<秒> sched_ext=<秒> sched_alloc=<秒> sched_overhead=<秒>
  num_new=<数> num_total=<数> update=<秒>
```

源码位置：`vllm/v1/engine/core.py`

| 字段               | 单位 | 含义                          | 聚合到 metrics.json             | 备注                           |
| ------------------ | ---- | ----------------------------- | ------------------------------- | ------------------------------ |
| `load`           | s    | KV 加载耗时                   | ✅ engine_step.load             | 对应 Worker · KV load         |
| `forward`        | s    | 前向计算耗时                  | ✅ engine_step.forward          | 对应 Worker · prefill forward |
| `save`           | s    | KV 保存耗时                   | ✅ engine_step.save             | 对应 Worker · KV save         |
| `sched_hash`     | s    | 调度器 hash + 本地 cache 查找 | ✅ engine_step.sched_hash       |                                |
| `sched_ext`      | s    | 调度器外部 KV 查询            | ✅ engine_step.sched_ext        |                                |
| `sched_alloc`    | s    | 调度器 KV block 分配          | ✅ engine_step.sched_alloc      |                                |
| `sched_overhead` | s    | 调度器其他开销                | ✅ engine_step.sched_overhead   |                                |
| `update`         | s    | EngineCore 更新收尾           | ✅ engine_step.update           |                                |
| `num_new`        | 个   | 本步新增 token 数             | ✅ engine_step.num_new（int）   | token_stats                    |
| `num_total`      | 个   | 本步累计 token 数             | ✅ engine_step.num_total（int） | token_stats                    |

---

### 1.3 `[TTFT_DEBUG][Worker]` — Worker 侧执行（step 级）

日志格式（两行）：

```
[TTFT_DEBUG][Worker] load=<秒> forward=<秒> save=<秒>
  (defer_finalize=<bool>, wait_for_save=<bool>)
[TTFT_DEBUG][Worker] deferred_save=<秒>
```

源码位置：`vllm/v1/worker/kv_connector_model_runner_mixin.py`

| 字段               | 单位 | 含义                             | 聚合到 metrics.json | 备注                       |
| ------------------ | ---- | -------------------------------- | ------------------- | -------------------------- |
| `load`           | s    | Worker 侧 KV 加载                | ❌ 仅日志           | 与 EngineCore.load 对应    |
| `forward`        | s    | Worker 侧前向                    | ❌ 仅日志           | 与 EngineCore.forward 对应 |
| `save`           | s    | Worker 侧 KV 保存（-1 表示延迟） | ❌ 仅日志           |                            |
| `defer_finalize` | bool | 是否延迟保存                     | ❌ 仅日志           |                            |
| `wait_for_save`  | bool | 是否在等待保存完成               | ❌ 仅日志           |                            |
| `deferred_save`  | s    | 延迟保存实际耗时                 | ❌ 仅日志           |                            |

---

### 1.4 `[TTFT_DEBUG][Prometheus]` — 每请求 TTFT 链路合计（请求级）

日志格式：

```
[TTFT_DEBUG][Prometheus] req=<id>
  hash=<秒> ext=<秒> alloc=<秒> overhead=<秒>
  load=<秒> forward=<秒> save=<秒> update=<秒>
```

源码位置：`vllm/v1/engine/loggers.py`

| 字段         | 单位 | 含义                   | 聚合到 metrics.json | 备注                |
| ------------ | ---- | ---------------------- | ------------------- | ------------------- |
| `hash`     | s    | hash + 本地 cache 查找 | ✅ → request_chain | 对应 sched_hash     |
| `ext`      | s    | 外部 KV 查询           | ✅ → request_chain | 对应 sched_ext      |
| `alloc`    | s    | KV block 分配          | ✅ → request_chain | 对应 sched_alloc    |
| `overhead` | s    | 调度其他开销           | ✅ → request_chain | 对应 sched_overhead |
| `load`     | s    | KV 加载                | ✅ → request_chain | 对应 load           |
| `forward`  | s    | 前向计算               | ✅ → request_chain | 对应 forward        |
| `save`     | s    | KV 保存                | ✅ → request_chain | 对应 save           |
| `update`   | s    | 更新收尾               | ✅ → request_chain | 对应 update         |

> **TTFT 定义**：上述 8 段求和 = 每请求 TTFT（取 p50），**不含排队等待 / 分词 / 输出阶段**。
> `metrics.json` 中 `ttft_ms` = `request_chain.p50_ms`。

---

### 1.5 `[TTFT_DEBUG][Stats]` — 每请求统计（请求级）

日志格式：

```
[TTFT_DEBUG][Stats] req=<id>-<step>
  is_prefilling=<bool>
  step_fwd=<秒> step_save=<秒> step_load=<秒>
  cum_fwd=<秒> cum_save=<秒> cum_load=<秒>
```

源码位置：`vllm/v1/engine/stats.py`

| 字段              | 单位 | 含义                  | 聚合到 metrics.json | 备注 |
| ----------------- | ---- | --------------------- | ------------------- | ---- |
| `is_prefilling` | bool | 是否处于 prefill 阶段 | ❌ 仅日志           |      |
| `step_fwd`      | s    | 本步前向耗时          | ❌ 仅日志           |      |
| `step_save`     | s    | 本步 KV 保存耗时      | ❌ 仅日志           |      |
| `step_load`     | s    | 本步 KV 加载耗时      | ❌ 仅日志           |      |
| `cum_fwd`       | s    | 累计前向耗时          | ❌ 仅日志           |      |
| `cum_save`      | s    | 累计 KV 保存耗时      | ❌ 仅日志           |      |
| `cum_load`      | s    | 累计 KV 加载耗时      | ❌ 仅日志           |      |

---

## 二、vllm-ascend 补丁插桩（`[TTFT]`）

### 2.1 `start_load_kv cost time` — KV 加载细分（step/rank 级）

日志格式：

```
start_load_kv cost time <总秒> buildkey <秒> get <秒>
```

源码位置：`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py`

| 字段         | 单位 | 含义                  | 聚合到 metrics.json | 备注 |
| ------------ | ---- | --------------------- | ------------------- | ---- |
| `total`    | s    | KV 加载总耗时         | ✅ kv_load.total    |      |
| `buildkey` | s    | 构造 key / 收集 chunk | ✅ kv_load.buildkey |      |
| `get`      | s    | 后端传输（get 操作）  | ✅ kv_load.get      |      |

---

### 2.2 `[TTFT] wait_for_save handle_requset` — KV 保存细分（step/rank 级）

日志格式：

```
[TTFT] wait_for_save handle_requset total <秒>
  build_key <秒> lookup <秒> prepare <秒> sync <秒> put <秒>
```

源码位置：`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py`

| 字段          | 单位 | 含义                 | 聚合到 metrics.json  | 备注 |
| ------------- | ---- | -------------------- | -------------------- | ---- |
| `total`     | s    | KV 保存总耗时        | ✅ kv_save.total     |      |
| `build_key` | s    | key 构造             | ✅ kv_save.build_key |      |
| `lookup`    | s    | 查重 lookup          | ✅ kv_save.lookup    |      |
| `prepare`   | s    | 地址准备             | ✅ kv_save.prepare   |      |
| `sync`      | s    | NPU event 同步       | ✅ kv_save.sync      |      |
| `put`       | s    | 后端写入（put 操作） | ✅ kv_save.put       |      |

---

### 2.3 `[TTFT] wait for save end` — 保存等待（step/rank 级）

日志格式：

```
[TTFT] wait for save end, cost time <秒> waiting <秒>
```

源码位置：`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py`

| 字段        | 单位 | 含义           | 聚合到 metrics.json      | 备注 |
| ----------- | ---- | -------------- | ------------------------ | ---- |
| `cost`    | s    | 等待保存总耗时 | ✅ wait_for_save.cost    |      |
| `waiting` | s    | 纯队列等待耗时 | ✅ wait_for_save.waiting |      |

---

### 2.4 `KV pool worker prepare get` — Token 命中统计（请求级）

日志格式：

```
KV pool worker prepare get req=<id>-<step>
  token_len_chunk=<数> get_token_len=<数>
  vllm_cached=<数> kvpool_cached=<数>
  groups=[...] load_async=<bool>
```

源码位置：`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py`

| 字段                | 单位 | 含义                  | 聚合到 metrics.json                   | 备注 |
| ------------------- | ---- | --------------------- | ------------------------------------- | ---- |
| `token_len_chunk` | 个   | chunk 内 token 长度   | ✅ prepare_get.token_len_chunk（int） |      |
| `get_token_len`   | 个   | 实际获取的 token 长度 | ✅ prepare_get.get_token_len（int）   |      |
| `vllm_cached`     | 个   | vllm 本地命中数       | ✅ prepare_get.vllm_cached（int）     |      |
| `kvpool_cached`   | 个   | KV pool 命中数        | ✅ prepare_get.kvpool_cached（int）   |      |
| `groups`          | list | 涉及的 rank 组        | ❌ 仅日志                             |      |
| `load_async`      | bool | 是否异步加载          | ❌ 仅日志                             |      |

---

## 三、metrics.json 聚合输出结构总览

| metrics.json 字段                         | 来源                 | 含义                                  |
| ----------------------------------------- | -------------------- | ------------------------------------- |
| `ttft_ms`                               | request_chain.p50_ms | 每请求 TTFT 链路合计的 p50（ms）      |
| `ttft_breakdown.matched_lines`          | 全部                 | 匹配到的插桩行总数                    |
| `ttft_breakdown.sections.tokenizer`     | 1.1                  | render / tokenize 统计                |
| `ttft_breakdown.sections.engine_step`   | 1.2                  | 8 段调度+执行统计                     |
| `ttft_breakdown.sections.kv_load`       | 2.1                  | KV 加载 3 段细分                      |
| `ttft_breakdown.sections.kv_save`       | 2.2                  | KV 保存 6 段细分                      |
| `ttft_breakdown.sections.wait_for_save` | 2.3                  | 保存等待 2 段                         |
| `ttft_breakdown.composition`            | 1.1+1.2              | 10 段链路均值占比（报告条形图）       |
| `ttft_breakdown.kvpool_rows`            | 2.1+2.2+2.3          | KV pool 11 行细分表（报告表格）       |
| `ttft_breakdown.token_stats`            | 1.2+2.4              | token 计数 + 命中统计                 |
| `ttft_breakdown.request_chain`          | 1.4                  | 每请求 TTFT 链路分布                  |
| `ttft_definition`                       | —                   | TTFT 计算口径说明                     |

---

## 四、本次采集数据示例（20260929-135752）

### 4.1 170 prefill（kv_producer）

| 指标                                | count | mean (ms)        | p50 (ms)         | p95 (ms) | max (ms) | 占比             |
| ----------------------------------- | ----- | ---------------- | ---------------- | -------- | -------- | ---------------- |
| Tokenizer · render                 | 202   | 20.96            | 5.06             | 77.21    | 397.16   | 1.87%            |
| Tokenizer · tokenize               | 202   | 175.58           | 114.80           | 518.95   | 1002.68  | 15.71%           |
| Scheduler · hash                   | 634   | 0.00             | 0.00             | 0.00     | 0.01     | ~0%              |
| Scheduler · 外部KV查询             | 634   | 22.56            | 0.00             | 127.27   | 954.26   | 2.02%            |
| Scheduler · KV block分配           | 634   | 1.07             | 1.22             | 1.62     | 2.94     | 0.10%            |
| Scheduler · 其他开销               | 634   | 0.20             | 0.12             | 0.68     | 2.26     | 0.02%            |
| Worker · KV load                   | 634   | 9.98             | 0.34             | 19.02    | 904.18   | 0.89%            |
| **Worker · prefill forward** | 634   | **812.61** | **879.01** | 1106.39  | 2231.70  | **72.69%** |
| Worker · KV save                   | 634   | 73.92            | 71.44            | 154.20   | 341.81   | 6.61%            |
| EngineCore · update                | 634   | 0.97             | 0.07             | 5.69     | 21.80    | 0.09%            |

- **TTFT (p50) = 1912 ms**（200 请求，mean 3652 ms，p95 11625 ms）
- KV save 细分：key 构造 42ms (p50) > 后端写入 put 9.6ms > 查重 1.0ms
- Token 命中：kvpool_cached 均值 30636 tokens，vllm_cached = 0

### 4.2 144 decode（kv_consumer）

| 指标                            | count | mean (ms)        | p50 (ms)        | p95 (ms) | max (ms) | 占比             |
| ------------------------------- | ----- | ---------------- | --------------- | -------- | -------- | ---------------- |
| Tokenizer · render             | 208   | 7.25             | 4.08            | 29.99    | 51.19    | 5.03%            |
| **Tokenizer · tokenize** | 208   | **133.65** | **88.47** | 373.60   | 671.36   | **92.78%** |
| Worker · KV load               | 1467  | 1.60             | 0.20            | 0.26     | 859.80   | 1.11%            |
| Worker · prefill forward       | 1467  | 1.55             | 0.00            | 11.45    | 38.21    | 1.08%            |
| Worker · KV save               | 1467  | 0.00             | 0.00            | 0.00     | 0.03     | ~0%              |

- TTFT (p50) = 0 ms（decode 侧不触发 Prometheus 链路汇总）
- KV load/save 细分全为 0（consumer 侧 KV 传输走 MooncakeHybridConnector，不在 ascend_store 补丁覆盖内）
