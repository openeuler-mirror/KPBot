---
name: go-gc-bottleneck-diagnosis
description: >
  用 gctrace + GOMEMLIMIT 诊断 Go GC 是不是瓶颈：注入 `GODEBUG=gctrace=1` 后核对堆 live 峰值、next-GC goal、
  GOGC 是否被 gc_tuner 钉死、STW 停顿，判断 GOMEMLIMIT 是否真正触发；并用"生效验证 + 1 计时轮"排除"注入没生效"和"改了没对比"。
  适用：怀疑 Go 服务 GC 是瓶颈想先证伪、要判断 GOMEMLIMIT 是否值得开、或要核对 GC 注入/透传是否真的生效。
  不适用反例：瓶颈已确认在别的层（C++/knowhere 等，Go GC 参数影响不到）；或只是要全量 pprof CPU/heap 分析（用 go-pprof-analysis）。
---

# Go GC 瓶颈诊断（gctrace + GOMEMLIMIT 证伪）

## 何时使用

- 怀疑 Go 服务（如 Milvus 的 Go 侧）GC 是性能瓶颈，想先低成本证伪再决定投入。
- 要判断 `GOMEMLIMIT` 是否值得开、会不会有效。
- 要核对"GC 相关 env 注入是否真的透传进容器、真的生效"。

## 核心原则

1. **先证伪、后投入**：gctrace + 堆峰值是最便宜的证伪证据，先证明"GC 不是瓶颈"再决定是否调 GC。
2. **注入生效必须独立验证**：注入没生效时，任何"无增益"结论都是空的。
3. **分清 Go GC 与 C++/本地依赖**：热点若是 C++/knowhere + jemalloc（`LD_PRELOAD`），Go GC 参数影响不到。
4. **别把 run-to-run 噪声当信号**：1 计时轮 -5% 在噪声内，不能当"更差"。

## 步骤

### 1. 注入 gctrace + GOMEMLIMIT

```
GOMEMLIMIT=6GiB
GODEBUG=gctrace=1
```

注入点选**宿主 bind-mount 的单文件**（编排脚本里"生成 compose 配置"的代码块之后追加 env 注入），
而非镜像内文件（改镜像内要重建）。
**注入目标必须选实际运行被测 Go 进程的 service**：本例要诊断的是 Milvus 的 Go 侧，就应命中
`name` 为 Milvus 的 service（或该角色的标签），**不要只按 `name.startswith("mem0")` 过滤**——否则 Milvus
服务名为 `milvus*` 时，重建后被测进程仍沿用原配置，注入因选错目标而静默不生效。注入后**在同一进程**
核对生效的 `GOMEMLIMIT` 与 gctrace 日志，别在别的容器里核对。核对手段用
`/proc/<pid>/environ`（直接读被测进程的环境）或 `docker exec <被测容器> env | grep -E 'GOMEMLIMIT|GODEBUG'`
（**不要用 `go env GOMEMLIMIT`**：`go env` 读的是 go 命令自身进程的环境、且其变量表里没有 GOMEMLIMIT，
恒输出空行，既不能证实也不能否认注入）。

### 2. 先验证注入生效（否则对比无效）

- gctrace 输出出现 = `GODEBUG=gctrace=1` 生效；容器 `env` 确认 `GOMEMLIMIT` 已透传。
- 透传坑：env 要过 forward 列表（如 `RUNNER_FORWARD_ENV_NAMES`）才进容器，漏转发 = 静默不生效。

### 3. 读 gctrace 判 GC 是否瓶颈

| 判据 | 取值来源 | 含义 |
|---|---|---|
| 堆 live 峰值（如 637MB） | 各 gctrace 行 `live` 字段取峰值 | 远小于 GOMEMLIMIT 上限 |
| next-GC goal 峰值（如 1.9GB） | 各 gctrace 行 goal 字段取峰值 | 远小于 soft limit → GOMEMLIMIT 从不触发 |
| GOGC 是否被 gc_tuner 钉死（如 200） | **gctrace 里没有 GOGC 字段**，用 goal÷live 比值反推（1.9GB÷637MB≈3.0 ⇒ GOGC≈200），或从 gc_tuner 配置/日志核对 | 调 GOMEMLIMIT 前先看 GOGC 是否已被别处控制 |
| STW 停顿 p99（如 5.95ms） | 每行 gctrace 的 `clock` 段第 1、3 两段是 STW（sweep termination + mark termination，第 2 段是并发标记非 STW）——汇总**所有周期这两段**再取 p99（gctrace 每周期给两个 STW 段，无现成 p99） | 停顿小 = GC 非瓶颈 |
| **GC assist + 并发标记 CPU**（易漏） | 同一计时窗口内采样请求 goroutine 的 assist 时间 + 并发标记 CPU（`cpu` 段第 2 段）；**行首 `6%` 是自进程启动以来 GC 的累计时间占比，不是本窗口占比**，不能拿末行 % 当 QA 窗口 GC CPU | STW p99 低**不等于** GC 无 CPU 成本：并发标记与请求 goroutine 承担的 assist 仍可能占大量 CPU |

四个判据（live / goal / GOGC / STW）一致指向"GC 不是瓶颈"时，仍需补第 5 项**标记+assist CPU**核对：
STW 低但 assist CPU 高的场景同样构成 GC 瓶颈。全部排除后才判 GOMEMLIMIT 无意义、放弃。

**判据 5 的 GC CPU 口径坑**：gctrace 行首的 `6%` 是**自进程启动以来的累计 GC 时间占比**（不是本周期、更不是
QA 窗口占比）；四相位长跑里读末行 `%` 当 QA 窗口 GC CPU，会把 seed/ingestion 的 GC 一起算进去，结论失真。
要按窗口求 GC CPU，应改用 `runtime/metrics`（如 `/gc/cpu_fraction:gc` 或 `/sched/pauses/total:seconds`）做
窗口首尾**差分**，不是读 gctrace 行首累计值。

**GOMEMLIMIT 是否触发**的判定式：`GOMEMLIMIT` 的计量口径是 Go 运行时 **Sys − HeapReleased**（不是堆 live），
堆目标还可能被软限调低——所以"goal 1.9GB 远 < 6GiB 软限"**不能单独排除软限已起作用**。要对照**有效软限**
与运行时管理的内存（Sys / HeapReleased），并比较同一负载下的 goal 峰值与 GC 频率（goal 是否被压低、GC 是否变密）。

### 4. 补一个计时轮兜底

- 1 计时轮相对基线中位，若方向为负且落在基线轮间波动内（如 -5% vs 基线 ±噪声），结论写"**本轮未检出收益**"
  而非"更差"——候选只跑一轮且结果落在基线波动范围内时，小幅收益和随机波动都符合这次观察，不能据此判
  "更差"或"有负收益"。
- 若要判断"增益低于门限"，需**预设最小有意义收益**，交错重复基线与候选并报告差值区间；单轮观察不足以
  下该结论。
- 结论：增益 < 门禁（未检出收益），环境还原，注入方式仅记录不默认启用。

## 门禁

- [ ] gctrace 输出出现，注入已核实生效（在**同一被测进程**核对 GOMEMLIMIT 与 gctrace）
- [ ] 注入目标命中实际运行被测 Go 进程的 service（非按 mem0 前缀误选）
- [ ] env 透传经 forward 列表，非静默不生效
- [ ] 堆 live / goal / GOGC（反推或 gc_tuner 侧核对）/ STW p99 四判据已读，且 GOGC 非 gctrace 字段已注明
- [ ] GC assist + 并发标记 CPU 已核对（STW 低不等于无 GC CPU 成本）
- [ ] GOMEMLIMIT 触发按 Sys−HeapReleased 口径对照，未单靠 goal<limit 排除
- [ ] 1 计时轮结论写"本轮未检出收益"，未误判"更差"
- [ ] 放弃后环境已还原

## 结果（标定案例形态）

- Milvus 容器注入 `GOMEMLIMIT=6GiB` + `gctrace=1`：堆 live 峰值 637MB、goal 1.9GB（远 < 6GiB），
  GOGC 被 gc_tuner 钉 200（goal÷live≈3.0 反推，gctrace 不含 GOGC 字段），STW p99 5.95ms → GOMEMLIMIT
  从不触发，GC 非瓶颈；并补核对标记+assist CPU。1 计时轮 -5.16%（噪声内）结论"本轮未检出收益"，
  判定放弃。索引构建是 C++/knowhere + jemalloc，Go GC 参数影响不到 seed 抖动；GOMEMLIMIT 口径按
  Sys−HeapReleased 补证，未单靠 goal<limit 下结论。

## 参考

- `references/gc-diagnosis.md`：注入点选择（镜像内 vs 宿主 bind-mount）、透传坑、gctrace 逐字段读法、
  GOMEMLIMIT 触发条件、与 go-pprof-analysis 的分工。