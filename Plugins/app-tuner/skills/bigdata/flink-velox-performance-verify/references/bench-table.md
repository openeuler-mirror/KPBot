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
- 每格 runner 限时 `--timeout`（默认 3600s）：超时 = 该格 failed（保留已完成的格与变更清单），不挂死整个矩阵；超时格同时**尽力经推导的 REST 取消**本格开始后仍在 RUNNING 的集群作业（取消结果记进 detail；REST 不可达 / 取消失败如实记，不改变该格 failed 的判定）
- **失败即停**：第一格失败后其余格不提交，标 NOT RUN；`run` 重跑时矩阵从头再来（证据树原地刷新），不会漏口径

## 3. conf 换装与恢复纪律

| 动作 | 对象 | 纪律 |
|---|---|---|
| 运行锁 | 两份 conf 旁的 `<conf>.flink-velox-perf.lock` | 进入修复 / 换装前**独占创建**（`O_CREAT\|O_EXCL`，内容 pid+时间），持锁到恢复完成；锁的持有进程活着 → 拒绝（exit 2，"another run holds the lock"），已死 → 视为崩溃遗留、覆盖并记进 changes；plan 只读不锁 |
| 备份 | `$NEXMARK_HOME/conf/nexmark.yaml` 与 Flink 生效 conf | 整文件复制到 `<原路径>.flink-velox-perf.bak`：先写**同目录临时文件** `<bak>.tmp-<pid>`、fsync 后原子 rename 就位（中断 / 盘满不留半份备份，写失败则 conf 未动、无 `.bak` 产生），进 `changes` 清单 |
| 换入 | nexmark conf | 固定 conf 整份写入（REST 行已按本集群推导） |
| 逐格设置 | Flink 生效 conf | `parallelism.default: <p>`、`pipeline.object-reuse: true/false`（已有行替换、没有追加） |
| 恢复 | 两份 conf | `run` 结束 / 失败 / SIGINT / SIGTERM 都把 `.bak` 原地换回（`os.replace`）；恢复关键段内 SIGINT/SIGTERM 暂蔽（不被二次中断劈成两半），逐份**读回与备份逐字节核对**后置 `confs_restored`——未恢复 / 核对失败 → `false`，error 列明 `confs NOT restored: <path>` |
| 崩溃遗留 | `.bak` 还在 | 下次 `run` 先自动把 `.bak` 恢复回去（那本来就是用户原件），并记进 `changes`（在运行锁保护下） |
| 原件已丢 | conf 内容就是本 skill 的固定口径且无 `.bak` | **拒绝执行**（提示手工恢复），绝不覆盖 |

## 4. 探测与拒绝判据（plan / run 共用，run 时再核一遍）

| 判据 | 拒绝条件 | fix 提示 |
|---|---|---|
| 原生侧门 | `$FLINK_HOME/lib/*.jar` 名含 gluten / velox | `flink-velox-cluster-switch --to native` 切回原生侧（会重启集群）再跑 |
| Flink 安装 | 目录不存在或缺 `bin/flink` / conf | 指对 `--flink-home` |
| 集群在跑 | REST（推导出的地址:端口）`/overview` 不应答 | 先起集群 |
| 槽位 | workers × `taskmanager.numberOfTaskSlots` < 最大并行度 | 调小 `--parallelism`，或调大槽位重启集群 |
| nexmark | 缺 `bin/run_query.sh` / `conf/nexmark.yaml` / 不可写 | 装 nexmark 可运行树（flink-velox-runtime-install）或指对 `--nexmark-home`；不可写同下行口径 |
| conf 可写 | Flink 生效 conf 或 nexmark conf 不可写 | 换到能写该 conf 的用户（如安装属主）运行，或请管理员调整 conf 属主/权限后重试——工具不拼装 root 命令、不预置 `--yes` |
| conf / 备份形态 | 生效 conf、`conf/nexmark.yaml` 或其 `.bak` 是符号链接 | 换普通文件（或属主手工换装 / 人工确认后移除 `.bak` 链接）再重跑；本 skill 不穿透链接换装、不把链接 rename 进位（写穿即改任意目标、恢复即毁链接） |
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
- 每格两份证据：`<evidence>/qNN.p<P>.r0.out`（指标表）与 `*.submit.log`（runner 全输出）；默认 `<项目>/e2e/perf/native/`，原地刷新。**证据路径逐文件守卫**（`--out` 同款，写入前检查）：已存在且为符号链接（会写穿任意目标）或非普通文件（fifo 会在整格跑完后挂死写入）→ 该格 failed，detail 点名路径，不写穿、不挂死
- 超时格把 runner 被杀前已打印的部分输出尽力落成该格 `*.submit.log`（无指标表，detail 指向该日志；无输出则注明）；启动失败格无输出可落
- 采集判定：runner 退出码非 0 → failed；退出 0 但无 "Nexmark Results" 表 → failed（作业多半没到 FINISHED）；表里无本查询行 → failed
- events 数 ≠ 1 亿、time 非正数、或吞吐列解析为 None（如 `n/a`）→ 该格标 `comparable: false`（detail 说明缺的关键指标，如 "throughput missing from the table"），仍留数据
- **Cores 列的已知限制**：CPU 指标经 `nexmark.metric.reporter.host:port`（本 skill 固定 conf 里 9098）从 TM 回传；当 TM 没有回连时 Cores 记 0、`Throughput/Cores` 失真（如实留 0/n）——events / time / throughput 不受影响（nexmark 源自身计数）。跑前确认 9098 没被残留进程占用（被占时 runner 直接失败）

## 6. 退出码与输出契约

| 退出码 | 含义 |
|---|---|
| 0 | plan 通过；run 所有格 done 且两份 conf 核对恢复 |
| 1 | run 有格 failed（报告点名）；或 conf 未能恢复 / 核对失败（`confs_restored=false`，error 列明路径，原件可从 `.bak` 或自有副本手工恢复） |
| 2 | 拒绝（缺前置 / 无 `--yes` / `--out` 或证据目录不合法 / **另一轮持有运行锁** / 参数不合法）；工具自身异常（`tool aborted`，`confs_restored` 按实际核对置值） |

JSON 顶层键 `flink_velox_performance_verify_result`：`success` / `checked_at` / `command` / `side`（恒 native）/ `flink_home` / `nexmark_home` / `effective_conf` / `rest` / `slots` / `calibre` / `queries` / `parallelism` / `object_reuse` / `timeout` / `evidence_dir` / `runs[]`（query、parallelism、object_reuse、status=done|failed|pending、runner_exit、duration_s、metrics、comparable、table、submit_log、detail）/ `changes[]` / `confs_restored` / `next_step` / `error_message`。中止态输出**同一套键**（观测位空值、`tool aborted:` 开头）。`runs[]` 键集在失败 / 未跑的格上同样恒定：`runner_exit` / `metrics` / `comparable` 为 null / null / false（超时格的 `submit_log` 文件只在该格有部分输出时存在，detail 落了才指）。

## 7. 与其它 skill 的关系

- 前置裁决：环境齐不齐 → 规划中的统筹检查 skill（未合入）；单项检查可先跑已合入的 flink-velox-env 检查家族
- 切栈：`flink-velox-cluster-switch`（本 skill 拒绝 GlutenFlink 侧时给出的 fix 工单）
- 修 flink-dist-check / nexmark-check 的 fail：`flink-velox-runtime-install`
- 后续：GlutenFlink 侧对称一轮与两侧比值报告 = 本 skill 后续版本；结果正确性 = nexmark 正确性校验（规划中）
