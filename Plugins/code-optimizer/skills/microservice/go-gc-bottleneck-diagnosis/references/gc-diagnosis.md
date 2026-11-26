# Go GC 瓶颈诊断全量（标定案例）

## 1. 标定案例结论

在 flatidx 基线之上向 Milvus 容器注入 `GOMEMLIMIT=6GiB` + `GODEBUG=gctrace=1`（注入已核实生效，目标命中
Milvus service 而非 mem0 前缀）：
gctrace 显示 Go 堆 live 峰值 637MB、next-GC goal 峰值 1.9GB（远 < 6GiB soft limit）、GOGC 被 gc_tuner 钉死 200
（goal÷live≈3.0 反推，gctrace 不含 GOGC 字段）、STW 停顿 p99 5.95ms，并补核对标记+assist CPU → **GC 不是瓶颈**。
1 计时轮 qa retrieval 吞吐 -5.16%（方向为负但落在基线轮间波动内），结论"本轮未检出收益"；GOMEMLIMIT 口径按
Sys−HeapReleased 对照，未单靠 goal<limit 排除触发。
结论：放弃，环境已还原，注入方式仅记录不默认启用。

## 2. 注入点选择

| 候选 | 代价 | 选择 |
|---|---|---|
| `generate_scaleout_compose.py` | 镜像内文件（`/workspace/scripts/`），改它要重建 runner 镜像 | ✗ |
| `mem0_locomo.sh` | 宿主 bind-mount 进 runner 容器的单文件（`docker inspect` 证实 `Mode=ro`），改宿主即生效 | ✓ |

在 `mem0_locomo.sh` 的 `generate_scaleout_config()` 里、生成 compose 配置（组装各 service 环境变量）的
代码块之后追加，对**实际运行被测 Go 进程的 service** `setdefault("environment", ...)` 注入。本例诊断的是
Milvus 的 Go 侧，应命中 Milvus 角色
（`name` 含 `milvus`），**不要只按 `name.startswith("mem0")` 过滤**——Milvus 服务名为 `milvus*` 时，
按 mem0 前缀选目标会漏掉 Milvus，重建后被测进程仍沿用原配置，注入静默不生效。注入后在**同一进程**
核对生效的 `GOMEMLIMIT`（`/proc/<pid>/environ`）与 gctrace 日志。

## 3. 透传坑

- env 要过 forward 列表（`RUNNER_FORWARD_ENV_NAMES`）才进容器，漏转发 = 静默不生效（不是报错）。
- 生成器在镜像内、编排脚本在宿主 bind-mount：改错层 = 重建镜像 vs 改宿主即生效，先 `docker inspect` 分清。

## 4. gctrace 判读（四判据 + 逐字段读法）

先给一行真实 gctrace 样例并逐字段标注（Go runtime 的 `GODEBUG=gctrace=1` 每周期一行）：

```
gc 1 @0.241s 6%: 0.055+1.2+0.018 ms clock, 0.44+0/1.2/0.02+0.36 ms cpu, 45->46->31 MB, 46 MB goal, 1.2 MB stacks, 3.4 MB globals, 16 P
   └@0.241s   └6%        └STW1(sweep)+并发标记+STW2(mark)   └cpu 时间（assist/并发标记等）      └堆 start->end->live   └goal └stacks └globals └P 数
```

> 注：Go 1.20 起每行必含 `stacks` / `globals` 两字段（标定镜像 go.mod 要求 go ≥ 1.20，真实输出必然有此两段）；
> 样例里这两段是示意值，只用于说明字段位置。三段 `clock` 时间里只有第 1 段（sweep termination）与第 3 段
> （mark termination）是 STW，第 2 段（上面的 `1.2 ms`）是并发标记与扫描，**不是 STW**；gctrace 每周期
> 给的是两个 STW 段，不是"单次 STW"。

字段映射到判据：

| 判据 | gctrace 取值字段 | 标定值 | 结论 |
|---|---|---|---|
| 堆 live 峰值 | 各周期 `->live` 段（如上 `...->31 MB`）取峰值 | 637MB | 远 < 6GiB soft limit |
| next-GC goal 峰值 | 各周期 `N MB goal` 段取峰值 | 1.9GB | goal 远 < soft limit |
| GOGC | **gctrace 无 GOGC 字段**；用 goal÷live 反推（1.9GB÷637MB≈3.0 ⇒ GOGC≈200）或从 gc_tuner 配置/日志核对 | 200（gc_tuner 钉死） | 调 GOMEMLIMIT 前先看 GOGC 是否被别处控制 |
| STW p99 | 每行 `clock` 段第 1、3 两段（sweep termination + mark termination，第 2 段并发标记非 STW）**汇总取 p99**（gctrace 每周期两个 STW 段，无现成 p99） | 5.95ms | 停顿小 = 非瓶颈 |
| 标记+assist CPU | `cpu` 段（assist/并发标记第 2 段）在同一计时窗口内累计；**行首 `6%` 是自进程启动以来 GC 的累计时间占比，非本窗口占比**——要按窗口求 GC CPU 用 `runtime/metrics` 做窗口首尾差分 | — | STW 低但 assist CPU 高仍可能是瓶颈 |

**STW p99 取数口径**：gctrace 每周期一行，STW 是其中两段（第 1、3 段）时长；要得到 p99，需把所有周期的
**第 1、3 两段**时长收集起来（正则抓 `@...s ...ms clock` 里的 stop-the-world 段，两段都要抓、勿只抓第一段），
再取 p99 分位——只抓第一段会漏掉往往更大的 mark termination 段，把并发标记段算进去又会把 STW 高估数十倍。

**GOMEMLIMIT 是否触发**的判定式：GOMEMLIMIT 的计量口径是运行时 **Sys − HeapReleased**（不是堆 live），
堆目标本身还可能被软限调低——所以"goal 1.9GB 远 < 6GiB 软限"**不能单独排除软限已起作用**。需对照
**有效软限**与运行时管理的内存（Sys / HeapReleased），并比较同一负载下的 goal 峰值与 GC 频率
（goal 是否被压低、GC 是否变密）后才能下"GOMEMLIMIT 是 no-op"的结论。

## 5. 计时轮兜底

- 1 计时轮 qa retrieval 吞吐 -5.16%（72.84 vs 76.80 req/s）、p50 +10.9%、p99 +2.7%。
- 判读：方向为负但完全落在基线轮间波动内 → 结论写"**本轮未检出收益**"（噪声内），不是"更差"。
  候选只跑一轮且落在基线波动范围内时，小幅收益和随机波动都符合这次观察；要判"增益低于门限"需预设
  最小有意义收益并交错重复基线与候选、报告差值区间，单轮观察不足以支撑。

## 6. 分清 GC 与本地依赖

- Milvus 的索引构建是 C++/knowhere + jemalloc（`LD_PRELOAD`），Go 的 GC 参数影响不到 seed 抖动。
- 若热点在 C++ 内核，GC 诊断无意义，转去组件级分析（如 vectordb 域）。

## 7. 与 go-pprof-analysis 的分工

- `go-pprof-analysis`：全量 CPU/heap/goroutine/block/mutex profile，定位堆分配热点/泄漏。
- `go-gc-bottleneck-diagnosis`（本 skill）：GC 是否是瓶颈的证伪 + GOMEMLIMIT 是否值得开的判断。
- 先用本 skill 证伪"GC 非瓶颈"，再决定是否需要 go-pprof-analysis 的 heap profile 深挖。