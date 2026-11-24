# 基准口径与纪律表（bench-table）

> `flink-velox-performance-verify` 的判据细则：矩阵、口径、conf 换装与恢复、拒绝判据、指标与契约。
> 事实来源：`scripts/performance_verify.py`（实现）、`conf/nexmark-bench.yaml`（口径）。

## 1. 固定口径（数字可比的前提）

| 项 | 值 | 出处 |
|---|---|---|
| events 数 | `100000000`（1 亿） | conf `nexmark.workload.suite.bench.events.num` |
| 生成速率 | `50000000`（5000 万 tps） | conf `...tps` |
| sink | blackhole（查询 DDL 自带，无 changelog 可收） | nexmark 查询定义 |
| warmup | 120s / 1 亿 events / 5000 万 tps | conf `...warmup.*` |
| REST 端点 | 从 Flink **实际读的那份 conf** 推导 `rest.address`（缺省 localhost）/ `rest.port`（缺省 8081），写进固定 conf 的 `flink.rest.*` | 两份 conf 并存时只认 `flink-conf.yaml`，不合并（Flink 1.19 实证） |

口径只改 `conf/nexmark-bench.yaml` 这一处（改了要重新过检视）；命令行不接受现场改数。

## 2. 运行矩阵

- 单元格（cell）=（query，parallelism，object-reuse）。执行顺序：query → parallelism → r0（reuse=false）→ r1（`--object-reuse` 时，reuse=true）
- 默认 `--queries q0`（吞吐基线），全集 = 固定 conf 里的 `q0–q23`（除 q6，上游标注不可运行）；越界查询 plan 就拒绝
- 默认 `--parallelism 1,4,8,16`；最大档必须 ≤ 槽位总数
- 每格 runner 限时 `--timeout`（默认 3600s）：超时 = 该格 failed（保留已完成的格与变更清单），不挂死整个矩阵
- **失败即停**：第一格失败后其余格不提交，标 NOT RUN；`run` 重跑时矩阵从头再来（证据树原地刷新），不会漏口径

## 3. conf 换装与恢复纪律

| 动作 | 对象 | 纪律 |
|---|---|---|
| 备份 | `$NEXMARK_HOME/conf/nexmark.yaml` 与 Flink 生效 conf | 整文件 `copy2` 到 `<原路径>.flink-velox-perf.bak`，进 `changes` 清单 |
| 换入 | nexmark conf | 固定 conf 整份写入（REST 行已按本集群推导） |
| 逐格设置 | Flink 生效 conf | `parallelism.default: <p>`、`pipeline.object-reuse: true/false`（已有行替换、没有追加） |
| 恢复 | 两份 conf | `run` 结束 / 失败 / SIGINT / SIGTERM 都把 `.bak` 原地换回（`os.replace`），结果 JSON 记 `confs_restored` |
| 崩溃遗留 | `.bak` 还在 | 下次 `run` 先自动把 `.bak` 恢复回去（那本来就是用户原件），并记进 `changes` |
| 原件已丢 | conf 内容就是本 skill 的固定口径且无 `.bak` | **拒绝执行**（提示手工恢复），绝不覆盖 |

## 4. 探测与拒绝判据（plan / run 共用，run 时再核一遍）

| 判据 | 拒绝条件 | fix 提示 |
|---|---|---|
| 原生侧门 | `$FLINK_HOME/lib/*.jar` 名含 gluten / velox | `flink-velox-cluster-switch --to native` 切回原生侧（会重启集群）再跑 |
| Flink 安装 | 目录不存在或缺 `bin/flink` / conf | 指对 `--flink-home` |
| 集群在跑 | REST（推导出的地址:端口）`/overview` 不应答 | 先起集群 |
| 槽位 | workers × `taskmanager.numberOfTaskSlots` < 最大并行度 | 调小 `--parallelism`，或调大槽位重启集群 |
| nexmark | 缺 `bin/run_query.sh` / `conf/nexmark.yaml` / 不可写 | 装 nexmark 可运行树（flink-velox-runtime-install）或指对 `--nexmark-home` |
| conf 可写 | Flink 生效 conf 不可写 | 换用户或按 sudo 提示重跑 |
| 查询集 | 请求的查询不在固定 conf 的 suite 里 | 从固定集合里挑 |
| `--out` / 证据目录 | realpath 不在项目目录（cwd）内 / symlink / 已存在且非普通文件 / 已存在未带 `--overwrite` | 退出 2，**任何动作之前**完成校验 |

## 5. 指标表与证据

runner 结束时在 stderr 打出（事件数模式；真实捕获样例——注意**千分位逗号、带单位单元格与 Total 行**，解析按此口径）：

```
-------------------------------- Nexmark Results --------------------------------

+------+-----------------+--------+----------+-----------------+--------------+-----------------+
| Query| Events Num      | Cores  | Time(s)  | Cores * Time(s) | Throughput    | Throughput/Cores|
+------+-----------------+--------+----------+-----------------+--------------+-----------------+
|q0    |100,000,000      |0       |595.598   |0.000            |167.9 K/s     |9.22 E/s         |
|Total |100,000,000      |0.000   |595.598   |0.000            |167.9 K/s     |9.22 E/s         |
+------+-----------------+--------+----------+-----------------+--------------+-----------------+
```

- 数字单元格解析规则：千分位逗号去除、`K/M/G/T/P/E` 后缀按 1000 进制换算、`/s` 后缀忽略；解析不出（如 `n/a`）记 None。`Total` 行不作为数据行。
- 每格两份证据：`<evidence>/qNN.p<P>.r0.out`（指标表）与 `*.submit.log`（runner 全输出）；默认 `<项目>/e2e/perf/native/`，原地刷新
- 采集判定：runner 退出码非 0 → failed；退出 0 但无 "Nexmark Results" 表 → failed（作业多半没到 FINISHED）；表里无本查询行 → failed
- events 数 ≠ 1 亿 → 该格标 `comparable: false`（人读报告注明 not comparable），仍留数据
- **Cores 列的已知限制**：CPU 指标经 `nexmark.metric.reporter.host:port`（本 skill 固定 conf 里 9098）从 TM 回传；当 TM 没有回连时 Cores 记 0、`Throughput/Cores` 失真（如实留 0/n）——events / time / throughput 不受影响（nexmark 源自身计数）。跑前确认 9098 没被残留进程占用（被占时 runner 直接失败）

## 6. 退出码与输出契约

| 退出码 | 含义 |
|---|---|
| 0 | plan 通过；run 所有格 done |
| 1 | run 有格 failed（报告点名，conf 已恢复） |
| 2 | 拒绝（缺前置 / 无 `--yes` / `--out` 或证据目录不合法 / 参数不合法）；工具自身异常（`tool aborted`） |

JSON 顶层键 `flink_velox_performance_verify_result`：`success` / `checked_at` / `command` / `side`（恒 native）/ `flink_home` / `nexmark_home` / `effective_conf` / `rest` / `slots` / `calibre` / `queries` / `parallelism` / `object_reuse` / `timeout` / `evidence_dir` / `runs[]`（query、parallelism、object_reuse、status=done|failed|pending、runner_exit、duration_s、metrics、comparable、table、submit_log、detail）/ `changes[]` / `confs_restored` / `next_step` / `error_message`。中止态输出**同一套键**（观测位空值、`tool aborted:` 开头）。

## 7. 与其它 skill 的关系

- 前置裁决：环境齐不齐 → `flink-velox-env-check`（统筹）
- 切栈：`flink-velox-cluster-switch`（本 skill 拒绝 GlutenFlink 侧时给出的 fix 工单）
- 修 flink-dist-check / nexmark-check 的 fail：`flink-velox-runtime-install`
- 后续：GlutenFlink 侧对称一轮与两侧比值报告 = 本 skill 后续版本；结果正确性 = nexmark 正确性校验（规划中）
