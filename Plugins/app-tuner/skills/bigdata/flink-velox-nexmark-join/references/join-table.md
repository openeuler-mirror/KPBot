# Join 家族查询表与口径（join-table）

> `flink-velox-nexmark-join` 的判据细则：固定查询集、join 语义对照、下推判定、conf 换装与恢复、拒绝与告警、输出契约。
> 事实来源：`scripts/nexmark_join.py`（实现）、`conf/nexmark-bench.yaml`（口径，与 flink-velox-performance-verify **逐字节同一份**）。

## 1. 固定口径（数字可比的前提）

| 项 | 值 | 出处 |
|---|---|---|
| events 数 | `100000000`（1 亿） | conf `nexmark.workload.suite.bench.events.num` |
| 生成速率 | `50000000`（5000 万 tps） | conf `...tps` |
| sink | blackhole（查询 DDL 自带，无 changelog 可收） | nexmark 查询定义 |
| warmup | 120s / 1 亿 events / 5000 万 tps | conf `...warmup.*` |
| object-reuse | 恒 false（r0 口径） | 每格写 `pipeline.object-reuse: false` |
| REST 端点 | 从 Flink **实际读的那份 conf** 推导 `rest.address`（缺省 localhost）/ `rest.port`（缺省 8081），写进固定 conf 的 `flink.rest.*` | 两份 conf 并存时只认 `flink-conf.yaml`，不合并（Flink 1.19 实证） |

口径文件是 performance-verify 那份的**逐字节拷贝**（suite 名 bench、1 亿 / 5000 万 / blackhole / warmup 全不动）——两个 skill 的数字跨报告可比；要改口径改那份 conf 并重新过检视，本 skill 的拷贝必须跟着逐字节同步。命令行不接受现场改数。

## 2. 固定查询集（join 家族，不接受 `--queries` 覆写）

六个查询的语义对照（供检视人核对；SQL 原文在 `$NEXMARK_HOME/queries/qN.sql`，下表按其真实谓词与键整理）：

| 查询 | join 类型 | 形状 | 键 / 谓词 | 语义焦点 | summary 分组 |
|---|---|---|---|---|---|
| q3 | regular join（双流） | auction ⋈ person | `auction.seller = person.id`；`category = 10`、`state IN (OR, ID, CA)` | regular join 吞吐 + 字符串谓词 | regular |
| q7 | window join + 回联 | tumble(bid, 10s) 最高价 ⋈ bid | `bid.price = 窗口 max(price)`；`dateTime BETWEEN window_end-10s AND window_end` | 窗口聚合结果上的 join 回联 | window-join |
| q8 | window join（对齐窗） | tumble(person, 10s) ⋈ tumble(auction, 10s) | `person.id = auction.seller`；两侧窗口对齐 | 窗口对齐的双流 join | window-join |
| q13 | temporal/lookup join | bid ⋈ side_input（维表） | `mod(bid.auction, 10000) = side_input.key`；`FOR SYSTEM_TIME AS OF p_time` | 维表 lookup（proctime） | lookup |
| q20 | inner join（宽投影） | bid ⋈ auction | `bid.auction = auction.id`；`category = 10` 过滤 | join 吞吐 + 过滤下推 | regular |
| q23 | 三流级联 join | bid ⋈ person ⋈ auction | `person.id = bid.bidder`、`auction.seller = bid.bidder` | 多路 join 级联 | cascade |

- 查询集**硬编码**在脚本里（`JOIN_QUERIES`），没有 `--queries` 参数：join 家族是本 skill 的身份，手工挑子集会悄悄破坏 per-type 汇总
- 每格一个 cell（query × parallelism）；`--parallelism` 只能裁剪档位（默认 1,4,8,16），不能动查询

## 3. 运行矩阵

- 单元格（cell）=（query，parallelism）。执行顺序：query → parallelism；object-reuse 恒 false
- 每格 runner 限时 `--timeout`（默认 3600s）：超时 = 该格 failed（保留已完成的格与变更清单），不挂死整个矩阵；超时格与**中断格（SIGINT / SIGTERM，路径对称）**同时**尽力经推导的 REST 取消本格自己的作业，归属优先**：①runner 自己输出里点名的 jid（`Job has been submitted with ID` 行，精确取消）；②本格开始时拍的 `/jobs/overview` 作业集基线——取消**基线之外新出现**且仍 RUNNING 的作业（别人的作业即使在时间窗内也不碰）；③基线没拍到（REST 抖动）才退回裸时间窗（`PLAN_SKEW_MS` 偏余量），detail 标 `cancel by time window (imprecise)`。取消结果记进 detail（REST 不可达 / 取消失败如实记，不改变该格 failed 的判定）
- **失败即停**：第一格失败后其余格不提交，标 NOT RUN；`run` 重跑时矩阵从头再来（证据树原地刷新），不会漏口径

## 4. conf 换装与恢复纪律

| 动作 | 对象 | 纪律 |
|---|---|---|
| 运行锁 | 两份 conf 旁的 `<conf>.flink-velox-join.lock` | 进入修复 / 换装前对锁文件加**内核排它锁**（`fcntl.flock(fd, LOCK_EX\|LOCK_NB)`，fd 持有到恢复核对完成；文件内容只写 pid+时间做诊断，锁本身在内核）。另一轮持有（flock 失败）→ 拒绝（exit 2，"another run holds the lock"）；持有进程崩溃 → 内核自动释放 flock，遗留的锁文件由下一轮直接 truncate 覆写接管（**从不 unlink，不存在 check-then-unlink 竞态**；空文件、残文本都一样）；锁文件本身是符号链接 → 拒绝不穿透；plan 只读不锁 |
| 备份 | `$NEXMARK_HOME/conf/nexmark.yaml` 与 Flink 生效 conf | 整文件复制到 `<原路径>.flink-velox-join.bak`：先写**同目录临时文件** `<bak>.tmp-<pid>`、fsync 后原子 rename 就位（中断 / 盘满不留半份备份，写失败则 conf 未动、无 `.bak` 产生），进 `changes` 清单；备份时记下原文件的 uid/gid |
| 换入 | nexmark conf | 固定 conf 整份写入（REST 行已按本集群推导） |
| 逐格设置 | Flink 生效 conf | `parallelism.default: <p>`、`pipeline.object-reuse: false`（已有行替换、没有追加） |
| 恢复 | 两份 conf | `run` 结束 / 失败 / SIGINT / SIGTERM 都把 `.bak` 原地换回（`os.replace`）；恢复关键段内 SIGINT/SIGTERM 暂蔽（不被二次中断劈成两半），逐份**读回与备份逐字节核对**后置 `confs_restored`——未恢复 / 核对失败 → `false`，error 列明 `confs NOT restored: <path>`；核对通过后把**备份时记下的 uid/gid `os.chown` 回去**（`copystat` 不复制属主/属组，管理员代跑不能把 conf 留成管理员所有；chown 失败不拦恢复，记进 `changes` 的 `owner NOT restored`） |
| 崩溃遗留 | `.bak` 还在 | 下次 `run` 先自动把 `.bak` 恢复回去（那本来就是用户原件），并记进 `changes`（在运行锁保护下） |
| 原件已丢 | conf 内容就是基准口径且无 `.bak` | **拒绝执行**（提示手工恢复），绝不覆盖——含 performance-verify 崩溃留下的同款状态（两 skill 固定口径逐字节相同，谁的 `.bak` 都不认领，只拒绝） |

后缀与 performance-verify 的 `.flink-velox-perf.*` 互不干扰：两个 skill 各自的锁与备份互不认领。

## 5. GlutenFlink join 下推判定（join_analysis 的核心）

- **只跑当前侧**：lib 里有无 gluten/velox jar 只决定 `side`（gluten / native），不设门——两侧都能跑；**GlutenFlink 侧**才做下推判定，native 侧恒 `not_applicable`（基线侧没有 gluten 节点这一说）
- 判定对象：本格 runner 结束后（含超时取消后）经 REST `GET /jobs/<jid>/plan` 取**本格开始后启动**（`PLAN_SKEW_MS` 偏余量）且未被更早格认领（job 与格一一对应，跨格去重）的作业 plan
- **gluten 节点 = 引擎字段匹配**（与 flink-velox-e2e-verify 的 `gluten_mention` 同法）：节点 `operator` / `operator_strategy`，或其 input 的 `ship_strategy` / `exchange` 提到 "gluten"（不区分大小写；实测形态如 ship strategy `GlutenHASH`、算子名 `Gluten cal`）。**绝不读 `description`**——它嵌用户 SQL 文本，表名带 gluten、查询名带 join 都会造成假判定
- **join 相关 gluten 节点**：在 gluten 节点之上，`operator` / `operator_strategy` 再提到 "join"——即 join 本体下推了；只有 calc / filter 形的 gluten 节点不算
- 判定结果进 `join_analysis[].pushdown`：`yes`（有 join 相关 gluten 节点）/ `no`（查过的 plan 都没有 → 回退原生算子，人读报告标 **FAILED**：未下推即未加速）/ `unknown`（REST 取不到 plan，如实说明原因）/ `not_applicable`（native 侧）/ `not_run`（矩阵没跑到）
- 下推判定是**观察不是门**：`no` 不改退出码、不判格失败——分析口径照报，裁决留给人

## 6. join_analysis 与 summary 契约

- `join_analysis[]` 每查询一条：`query` / `join_type` / `sides` / `key_columns` / `predicate`（语义字段硬编码自查询定义，§2 表即同款）/ `group`（summary 分组）/ `throughput_avg`（该查询 **comparable 格**的吞吐均值，events/s）/ `cells_done` / `cells_comparable` / `pushdown` / `pushdown_detail`
- `summary[]` 每 join 类型一条：**组内 = 各查询均值的均值**（regular = q3、q20；window-join = q7、q8；lookup = q13；cascade = q23）；无可比格的查询不进组均值，组内全无 → `avg: null`；`cell_count` 记组内参与计算的 comparable 格数
- 人读报告：每查询一行（q / join 类型 / avg 吞吐 K/s / 下推状态），`pushdown: no` 的行带 **FAILED** 前缀；末尾四行 summary（"regular avg X K/s …"）

## 7. 探测与拒绝判据（blocking）与告警（warn，不拦）

**blocking（拒绝，exit 2）**：

| 判据 | 拒绝条件 | fix 提示 |
|---|---|---|
| Flink 安装 | 目录不存在或缺 `bin/flink` / conf | 指对 `--flink-home` |
| nexmark | 缺 `bin/run_query.sh` / `conf/nexmark.yaml` | 装 nexmark 可运行树（flink-velox-runtime-install）或指对 `--nexmark-home` |
| conf 可写 | Flink 生效 conf 或 nexmark conf 不可写 | 换到能写该 conf 的用户（如安装属主）运行，或请管理员调整属主/权限后重试——工具不拼装 root 命令、不预置 `--yes` |
| conf / 备份形态 | 生效 conf、`conf/nexmark.yaml` 或其 `.bak` 是符号链接 | 换普通文件（或属主手工换装 / 人工确认后移除 `.bak` 链接）再重跑；不穿透链接换装、不把链接 rename 进位 |
| 崩溃遗留 | conf 内容就是基准口径且无 `.bak`（本 skill 或 performance-verify 的都算） | 手工恢复 conf/nexmark.yaml 后重跑 |
| `--out` | 解析后 realpath 不在项目目录（cwd）内 / 带 symlink 组件（含指回项目内的，`link/../x` 词法折叠看不出越界） / 已存在且非普通文件 / 已存在未带 `--overwrite` | 退出 2，**任何动作之前**完成校验；判定与落盘同用解析后的真实路径（内核语义：先跟 symlink 再解释 `..`） |
| 运行锁 | 另一轮正持有运行锁（内核 flock） | 等在跑的那轮结束并恢复 conf 后重跑 |
| 参数 | `--queries` 一律 unrecognized（exit 2）；`--parallelism` 非正整数 | 固定查询集不接受覆写；档位只能裁剪 |

**warn（`warnings[]`，检查尽力不拦——REST 抖动 / 槽位偏紧不误 blocked，格子的成败自己说话）**：

| 判据 | 告警条件 | 提示 |
|---|---|---|
| REST | 推导端点 `/overview` 不应答 | 起集群——没有它格会失败，下推判定也取不到 plan |
| 槽位 | workers × `taskmanager.numberOfTaskSlots` < 最大并行度 | 调低 `--parallelism`，或调大槽位重启集群 |

`--skip-check` 把这两项探测整个跳过（连告警都不出）；REST 地址推导仍来自生效 conf（换装固定 conf 需要）。

## 8. 退出码与输出契约

| 退出码 | 含义 |
|---|---|
| 0 | plan 通过；run 所有格 done 且两份 conf 核对恢复（下推 `no` 不影响 0——那是观察，不是失败） |
| 1 | run 有格 failed（报告点名）；或 conf 未能恢复 / 核对失败（`confs_restored=false`，error 列明路径） |
| 2 | 拒绝（缺前置 / 无 `--yes` / `--out` 不合法 / **另一轮持有运行锁** / 参数不合法含 `--queries`）；工具自身异常（`tool aborted`，`confs_restored` 为 `null`——崩溃时无法核对，不猜值，next_step 提示人工核对） |

JSON 顶层键 `flink_velox_nexmark_join_result`：`success` / `checked_at` / `command` / `side`（native\|gluten，探测自 lib）/ `gluten_jars` / `flink_home` / `nexmark_home` / `effective_conf` / `rest` / `slots` / `calibre` / `queries`（恒六查询）/ `parallelism` / `timeout` / `skip_check` / `evidence_dir` / `runs[]`（query、parallelism、object_reuse 恒 false、status=done\|failed\|pending、runner_exit、duration_s、metrics、comparable、gluten_nodes、join_gluten_nodes、plan_checked、plan_jids、plan_problem、table、submit_log、flink、detail）/ `join_analysis[]`（§6）/ `summary[]`（§6）/ `changes[]` / `warnings[]` / `confs_restored`（**run 恒有**：正常态按逐字节核对如实置值；拒绝态未触碰即原件 → `true`；中止态 `null` 无法核对；plan 缺席）/ `next_step` / `error_message`。中止态输出**同一套键**（观测位空值、`tool aborted:` 开头、`join_analysis` / `summary` 为空数组）。`runs[]` 键集在失败 / 未跑的格上同样恒定：`runner_exit` / `metrics` / `comparable` / `gluten_nodes` / `join_gluten_nodes` / `plan_checked` 为 null / null / false / null / null / false（超时格的 `submit_log` 文件只在该格有部分输出时存在，detail 落了才指）。

证据树 `<项目>/e2e/perf/join/`（跨任务沉淀、原地刷新）：`qNN.p<P>.out`（指标表）与 `*.submit.log`（runner 全输出），**逐文件带 `--out` 同款守卫且在格开始与每次写回时都查，判定与写回同用解析后的真实路径（内核语义：先跟 symlink 再解释 `..`，词法折叠看不出 `link/../x` 越界——写回打开的是守卫判定过的解析后路径，不是原始入参）**：带 symlink 组件一律不写穿（含指回项目内的——上级目录是指向项目外的 symlink 直接判该格 failed，指回项目内的同样拒绝）、解析后路径必须落在项目目录内、fifo / 非普通文件不挂死；**格中途被换成 symlink / fifo 的路径在写回时复检拒绝**，不穿透不挂死，该格 failed。指标表解析与 performance-verify 同口径（千分位逗号、K/M/G/T/P/E 单元、Total 行不算数、n/a 记 None）；events ≠ 1 亿、time 非正、吞吐 n/a → 该格 `comparable: false`（detail 点名缺什么，数据仍留）。

## 9. 与其它 skill 的关系

- `flink-velox-performance-verify`：同口径的原生侧全量 / 自选查询基准（裸指标表口径）；本 skill 是 **Join 类集 + join 专项注解 + 类型汇总**的分析口径，两份报告数字可比（口径文件逐字节同一份）
- `flink-velox-e2e-verify`：单条 SQL 的两侧正确性（本 skill 只报吞吐与 plan 观察，不做结果比对）；本 skill 的 gluten 节点判定与它共用引擎字段匹配法
- `flink-velox-cluster-switch`：切栈工单（本 skill 不设侧门、不重启集群，跑在当前侧）
- `flink-velox-runtime-install`：nexmark 可运行树的安装来源
- nexmark 正确性校验（规划中）：查询集语义的验证
