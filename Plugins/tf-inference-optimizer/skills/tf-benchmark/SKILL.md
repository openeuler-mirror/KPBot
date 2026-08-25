---
name: tf-benchmark
description: 运行 TF 推理端到端压测（benchmark_test.sh），后台 nohup 跑 + 轮询 results.csv，解析 QPS/P99/CPU，并做 QPS 容量扫描找饱和点。触发：benchmark、压测、端到端、QPS、P99、容量上限、start_benchmark、results.csv、吞吐对比。
---

# TF 推理优化 - 端到端压测

端到端压测用于对比优化前后（或不同版本）的延迟（P99）、CPU 和容量上限。这是最接近业务真实负载的验证口径。

## 1. 压测脚本与版本对照

`benchmark_test.sh` 是主脚本，通过环境变量配置；`start_benchmark_*.sh` 是不同版本的一行封装：

| 脚本 | BIN_SERVER | 用途 |
|---|---|---|
| `start_benchmark_<MODEL>_opt.sh` | `<BIN_SERVER_BASELINE>` | opt 对照 |
| `start_benchmark_<MODEL>_opt_1.sh` | `<BIN_SERVER_OPT>` | 含新优化点 |
| `start_benchmark_<MODEL>_baseline.sh` | baseline binary | 无加速基线 |

> ⚠️ **对照口径（易错）**：对比时「上一版本（before）」始终是**当前已合入的最新优化状态**（带加速 flag 的 opt binary），「本版本（after）」是叠加新优化点后的产物。「无加速基线」只用于首次评估整体加速比，不是逐优化点归因的对照起点。**不要按脚本名里的 baseline/opt 字样判断对照关系**。

参数（环境变量）：`RUN_MODES`、`DURATION`、`QPS_LIST`、`PREWARM_QPS`、`PREWARM_DURATION`、`SERVER_STARTUP_TIMEOUT`、`BIN_SERVER`、`BIN_CLIENT`、`DATA`、`MODEL_DIR`。

## 2. 后台跑（推荐，避免 expect 卡住 press）

**通过 expect 直跑长时 benchmark，press 客户端会在 qps 阶段卡住**（PREWARM 后无 sent 行、run.log 停在 PREWARM）。必须用 `setsid nohup ... &` 后台跑，再轮询结果：

```powershell
# 启动后台压测（完整约需 4-5 分钟）
.\remote-exec.ps1 -Command "cd <APP_DIR> && setsid nohup bash start_benchmark_<MODEL>_opt.sh > /tmp/bench.log 2>&1 < /dev/null & echo started"

# 等待后读结果（results.csv 逐轮追加）
.\remote-exec.ps1 -Command "cat <APP_DIR>/tmp/<MODEL>_benchmark_*/results.csv"
```

用固定路径或 `ls -dt <APP_DIR>/tmp/<MODEL>_benchmark_*/` 找最新目录。避免在 `-Command` 里用 `$(...)` 变量（会被本地 PowerShell 展开）。

## 3. 结果解析

results.csv 格式：`mode,dnn,target_qps,actual_qps,failure,p99_latency_us,server_cpu_pct_avg`

```
opt,true,290,284,0,5450,62.38
```

- `actual_qps` ≈ `target_qps` → 未过载，数据可信；明显落后 → 已饱和。
- 限 QPS 场景下，优化收益体现在**延迟降低**和**CPU 降低**（同样 QPS 用更少 CPU → 可支撑更高 QPS 上限）。

## 4. QPS 容量扫描

找容量上限时，用自定义 `QPS_LIST` 逐档升高（如 `290 300 310 350 380 400`）：

```powershell
.\remote-exec.ps1 -Command "cd <APP_DIR> && RUN_MODES='opt' QPS_LIST='350 380 400' DURATION=60 PREWARM_QPS=50 PREWARM_DURATION=10 SERVER_STARTUP_TIMEOUT=300 BIN_SERVER=<BIN_SERVER_OPT> BIN_CLIENT=<PRESS> DATA=<TEST_DATA> MODEL_DIR=<MODEL_DIR> setsid nohup sh benchmark_test.sh > /tmp/scan.log 2>&1 < /dev/null & echo started"
```

**饱和三特征**（同时出现即饱和）：
1. `actual_qps < target_qps`（落后目标）。
2. CPU 逼近 80% 红线。
3. P99 延迟跳高（相对前一档 +10% 以上）。

## 5. 对比口径注意

- 不同 binary 除了目标优化点，其他优化实现可能有版本差异，端到端差异不能 100% 归因于单一优化点——严格隔离用同一 binary + flag 开关。
- 跨时段对比有系统负载噪声，严格 A/B 建议同一时段各跑一次。
- 单点异常（如某档 P99 突然偏高）先复测确认噪声，再下结论。

## 6. 踩过的坑速查

- **expect 直跑卡 press**：见上，必须后台 nohup。
- **必须 `cd <APP_DIR>`**：`start_benchmark.sh` 用相对路径 `sh benchmark_test.sh`。
- **残余进程占用端口**：跑前 `pkill -9 -f predictor_server`。
- **-Timeout 单位是秒**：`remote-exec.ps1 -Timeout 490`，不是 ms。
