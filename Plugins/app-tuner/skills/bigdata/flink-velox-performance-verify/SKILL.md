---
name: flink-velox-performance-verify
description: >
  在 Flink 原生侧（lib 里没有 gluten/velox jar 的纯 Apache Flink）跑 nexmark 吞吐基准——固定口径
  1 亿 events / 5000 万 tps / blackhole sink / warmup 一轮，p=1/4/8/16 逐档（可裁剪），r0（object-reuse=false）
  必跑、`--object-reuse` 可加跑 r1 第二基线；skill 自备固定 conf 整份换入 $NEXMARK_HOME/conf/nexmark.yaml、
  parallelism.default 与 pipeline.object-reuse 写进 Flink 实际读的那份 conf，跑完/失败/中断都自动恢复原件。
  两段式：`plan`（默认）只探测打印矩阵与 conf 改动、`run --yes` 才执行（逐格提交作业、失败即停、证据与变更留档）；
  lib 里有 GlutenFlink jar、槽位不够、REST 不应答都直接拒绝。触发词：跑 nexmark 基准、nexmark 吞吐、
  基准测试、性能基准、1 亿 events、原生侧基线。不负责：结果集正确性比对 → nexmark 正确性校验（规划中）；
  GlutenFlink 侧与两侧加速比 → 本 skill 后续版本（当前只跑原生侧）；环境能不能跑 → 规划中的统筹检查 skill（未合入）。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需可写 $FLINK_HOME 实际生效 conf 与 $NEXMARK_HOME/conf/nexmark.yaml、可执行的 $NEXMARK_HOME/bin/run_query.sh、在跑的 Flink 集群（REST 从生效 conf 推导）；只跑原生侧（lib 不得有 gluten/velox jar）；只接受 1.19 版本线的 Flink
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# nexmark 原生侧吞吐基准

对比要有基线。GlutenFlink 相对原生 Flink 的加速比，分母是**原生侧**在固定口径下的吞吐——同一个查询集、同样的事件量与生成速率、同样的并行度档位，只有口径钉死，数字才可比。本 skill 只做原生侧这一半：把 nexmark 的固定基准 conf（1 亿 events / 5000 万 tps / blackhole / warmup 一轮，skill 自备 `conf/nexmark-bench.yaml`）整份换进 `$NEXMARK_HOME/conf/nexmark.yaml`，把 `parallelism.default` 与 `pipeline.object-reuse` 按矩阵写进 Flink **实际读的那份 conf**（两份并存只读 `flink-conf.yaml`，否则 `config.yaml`），逐格提交 `$NEXMARK_HOME/bin/run_query.sh`，捕获 runner 打出的 "Nexmark Results" 指标表与完整日志，**跑完、失败、中断都把 conf 恢复原件**。GlutenFlink 侧的对称一轮与两侧加速比是后续版本的事——两份基线（r0：`object-reuse=false`；r1：`--object-reuse` 时加跑的 `object-reuse=true`）现在就按同一口径留存，将来不用重跑原生侧。

会提交作业、会临时改两份 conf（都会恢复）；**不重启集群、不动机器其它任何东西**。矩阵与口径细则在 [bench-table.md](references/bench-table.md)。

## 使用场景

### 适用（应使用本 skill）

- GlutenFlink 优化前 / 后要量化时，先取原生 Flink 的吞吐基线
- 机器环境变了（内核、CPU 频率、Flink 补丁版本），要刷新原生侧基线数字
- 改动只涉及几个查询时，`--queries` 显式挑那几个（q0 是吞吐基线，选型对照见 references/bench-table.md）

### 不适用（不要用本 skill）

- lib 里有 gluten/velox jar（GlutenFlink 侧或半套）——先 `flink-velox-cluster-switch --to native` 切回原生侧再跑； GlutenFlink 侧的对称基准是后续版本
- 想验证**结果正确性**（两侧结果集比对）→ nexmark 正确性校验 skill（规划中）
- 想知道环境齐不齐（JDK、Flink 装没装、源码仓在不在）→ 规划中的统筹检查 skill（未合入；单项可先跑已合入的 flink-velox-env 检查家族）
- 指望本 skill 下结论说"快了 x%"——它只报数，比值与解读由人给

## 流程

1. `plan`（默认）：探测 Flink / nexmark、核对**原生侧**（lib 无 gluten/velox jar）、从生效 conf 推导 REST 并探活、数槽位（workers × taskmanager.numberOfTaskSlots ≥ 最大并行度），打印矩阵与 conf 改动预览；有任一前置不满足直接拒绝并给 fix
2. `run --yes`：取两份 conf 旁的**运行锁**（独占创建，另一轮在跑则拒绝；plan 只读不锁）→ 备份两份 conf（同目录临时文件写完 fsync 后原子就位，不留半份备份）→ 换入固定 conf（REST 行按本集群推导值）→ 逐格设 `parallelism.default` / `pipeline.object-reuse` → 提交 runner、限时等待（超时格尽力经 REST 取消本格开始后仍在 RUNNING 的作业）→ 指标表与 `*.submit.log` 落证据树 `<项目>/e2e/perf/native/`（跨任务沉淀、原地刷新，逐文件带 `--out` 同款守卫：symlink / 非普通文件的证据路径直接判该格 failed，不写穿、不挂死）→ **失败即停**（未跑的格标 NOT RUN，重跑续矩阵）→ 无论如何恢复 conf
3. 结果 JSON（`flink_velox_performance_verify_result`）记每格的 events / time / throughput / 证据路径与 conf 恢复确认；events 数与口径不符、或 time / throughput 缺失非正的格标 not comparable（detail 说明缺什么）；`confs_restored` 按逐份读回与备份**逐字节核对**的结果如实置值，未恢复的路径在 error 里点名

## 铁律

- **口径钉死**：事件量、tps、sink、warmup 只来自 `conf/nexmark-bench.yaml`，不接受现场改数——要改口径先改这份 conf 并重新过检视
- **只跑原生侧**：lib 里有 gluten/velox jar 一律拒绝，防止把 GlutenFlink 的数字当原生基线
- **conf 必归位**：两份 conf 的原件在跑完/失败/中断后都必须回到原位；上次崩溃留下的备份自动修复，原件丢失（备份也没了）时拒绝执行而不是覆盖；conf 或其 `.bak` 是符号链接也拒绝——不穿透链接换装、不把链接 rename 进位；备份经同目录临时文件**原子就位**；两份 conf 旁有**运行锁**（`<conf>.flink-velox-perf.lock`），另一轮在跑时拒绝启动；恢复逐份读回核对，`confs_restored` 如实置值、不实恢复的路径在 error 点名；证据文件同样逐文件守卫（symlink 不写穿、fifo 不挂死）
- **作业须到 FINISHED**：runner 成功但没打出指标表视为失败，不让空表进对比
- **只报数不下结论**：吞吐、耗时照录，可比性标注到格；加速比与结论由人结合环境噪声给

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`runs`（含 `status` / `metrics` / `comparable` / 证据路径）、`changes`、`confs_restored`、`next_step`、`error_message` 必须保留（下例 run 单格，节选；plan 的 `runs` 为空、`confs_restored` 缺席）：

```json
{
  "flink_velox_performance_verify_result": {
    "success": true,
    "checked_at": "2026-11-24T17:30:00",
    "command": "run",
    "side": "native",
    "flink_home": "/usr/local/flink-1.19.2",
    "nexmark_home": "/usr/local/nexmark",
    "rest": {"address": "localhost", "port": 8081},
    "slots": {"tms": 1, "per_tm": 8, "total": 8},
    "calibre": {"events": 100000000, "tps": 50000000},
    "runs": [
      {"query": "q0", "parallelism": 1, "object_reuse": false, "status": "done",
       "runner_exit": 0, "duration_s": 63.2,
       "metrics": {"query": "q0", "events_num": 100000000, "cores": 8, "time_s": 42.5,
                    "throughput": 2352941.2, "throughput_per_cores": 294117.6},
       "comparable": true, "flink": {"parallelism.default": 1, "pipeline.object-reuse": false},
       "table": "e2e/perf/native/q0.p1.r0.out", "submit_log": "e2e/perf/native/q0.p1.r0.submit.log",
       "detail": "100000000 events in 42.5s, 2352941.2 events/s"}
    ],
    "changes": [
      {"action": "backed up", "detail": "/usr/local/flink-1.19.2/conf/config.yaml -> …/config.yaml.flink-velox-perf.bak"},
      {"action": "restored", "detail": "…/nexmark.yaml.flink-velox-perf.bak -> /usr/local/nexmark/conf/nexmark.yaml"}
    ],
    "confs_restored": true,
    "next_step": "1 cell(s) done; the metrics are in e2e/perf/native and in this result",
    "error_message": ""
  }
}
```

## 脚本工具

- `scripts/performance_verify.py` — 主脚本：`plan` / `run --yes`；`--flink-home` / `--nexmark-home` / `--queries` / `--parallelism` / `--object-reuse` / `--timeout` / `--evidence-dir` / `--out` / `--json`

## 参考资料

- `references/bench-table.md` — 口径与矩阵细则、指标表格式、conf 换装与恢复纪律、拒绝判据
- `conf/nexmark-bench.yaml` — 固定基准口径（唯一事实来源）
