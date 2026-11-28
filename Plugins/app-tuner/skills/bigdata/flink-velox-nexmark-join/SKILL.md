---
name: flink-velox-nexmark-join
description: >
  nexmark Join 类查询吞吐基准：固定跑 q3/q7/q8/q13/q20/q23 六个 join 语义查询（不接受 --queries 覆写），
  口径与 flink-velox-performance-verify 逐字节同一份（1 亿 events / 5000 万 tps / blackhole / warmup，
  conf 文件直接复制，跨 skill 数字可比），p=1/4/8/16 逐档（可裁剪），跑在当前侧（lib 有无 gluten/velox jar
  只决定 side，不设门）。产出 join 专项分析报告而非裸指标表：join_analysis 每查询注解 join 类型 / 边 /
  键列 / 谓词与 GlutenFlink 下推判定（REST plan 引擎字段匹配，GlutenFlink 侧跑时无 join 相关 gluten 节点
  即回退，人读报告标 FAILED）；summary 按 regular / window-join / lookup / 级联四类给平均吞吐。
  skill 自备固定 conf 整份换入 $NEXMARK_HOME/conf/nexmark.yaml、parallelism.default 与
  pipeline.object-reuse(false) 写进 Flink 实际读的那份 conf，跑完/失败/中断都自动恢复原件。
  两段式：plan（默认）只探测打印矩阵与 join 家族、run --yes 才执行（逐格提交作业、失败即停、证据与变更留档）。
  REST / 槽位预检是告警不是门（--skip-check 可整个跳过）。触发词：join 基准、join 吞吐、nexmark join、
  q3 q7 q8 q13 q20 q23、join 下推、多流 join 性能。不负责：全量 / 自选查询的裸基线 →
  flink-velox-performance-verify；结果集正确性 → nexmark 正确性校验（规划中）；切栈 →
  flink-velox-cluster-switch；两侧加速比结论 → 由人结合两份报告给。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需可写 $FLINK_HOME 实际生效 conf 与 $NEXMARK_HOME/conf/nexmark.yaml、可执行的 $NEXMARK_HOME/bin/run_query.sh、在跑的 Flink 集群（REST 从生效 conf 推导；GlutenFlink 侧的下推判定需 REST 可取 plan）；只接受 1.19 版本线的 Flink
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# nexmark Join 类查询吞吐基准

GlutenFlink 对 join 的加速要有 join 自己的口径：全量套件的平均数会把 join 的故事稀释掉，单查询的裸数又没有类型间的对照。本 skill 固定跑 nexmark 里**六个 join 语义查询**（q3 regular / q7 窗口回联 / q8 对齐窗 / q13 维表 lookup / q20 宽投影 inner / q23 三流级联——语义对照表在 [join-table.md](references/join-table.md)，供检视人对着 `$NEXMARK_HOME/queries/qN.sql` 核对），口径与 `flink-velox-performance-verify` **逐字节同一份**（`conf/nexmark-bench.yaml` 直接复制：1 亿 events / 5000 万 tps / blackhole / warmup，`pipeline.object-reuse` 恒 false 的 r0 口径），跑在**当前侧**（lib 里有无 gluten/velox jar 只决定 side 报告值，不设门）。产出不是裸指标表而是 **join 专项分析报告**：每查询注解 join 类型 / 边 / 键列 / 谓词，GlutenFlink 侧跑时从 REST plan（引擎字段匹配，**不读 description**）判定 join 是否下推——无 join 相关 gluten 节点即回退原生算子，人读报告标 **FAILED**（未下推即未加速）；末尾按 regular / window-join / lookup / 级联四类汇总平均吞吐。

会提交作业、会临时改两份 conf（都会恢复）；**不重启集群、不切栈、不动机器其它任何东西**。矩阵、判定与契约细则在 [join-table.md](references/join-table.md)。

## 术语

这套栈的官方叫法是 **GlutenFlink**（gluten-flink + velox4j + velox）；本 skill 的 JSON `side` 取值用 `gluten` / `native`（引擎侧报告值），行文一律写 GlutenFlink，不用裸缩写。join 相关术语（regular / window / temporal-lookup / 级联 join、下推、回退）按 nexmark 查询定义与 flink-velox-e2e-verify 的既有口径。

## 使用场景

### 适用（应使用本 skill）

- 要量化 GlutenFlink 对 **join 本身**的加速：GlutenFlink 侧跑一轮看下推判定与吞吐，原生侧跑一轮作分母（同口径直接可比）
- join 改动（hash join 调优、shuffle 策略、维表 lookup 路径）前后要回归对比：六个查询覆盖 regular / 窗口 / lookup / 级联四种形态
- 要 join 类型间的吞吐对照（哪类 join 是瓶颈）：summary 四行 per-type 平均

### 不适用（不要用本 skill）

- 要全量 / 自选查询的**裸基线指标表** → `flink-velox-performance-verify`（同口径，报告是原始指标口径；本 skill 是 Join 类集 + 专项注解的分析口径，两个 skill 分工不重叠）
- 要验证**结果集正确性**（两侧 join 输出比对）→ `flink-velox-e2e-verify` / nexmark 正确性校验（规划中）；本 skill 只报吞吐与 plan 观察
- 想换查询集（加 q2、只跑 q20）——查询集固定是本 skill 的身份，手工挑子集会悄悄破坏 per-type 汇总；单查询深入用 performance-verify 的 `--queries`
- 要切栈再跑 → 先 `flink-velox-cluster-switch`（须用户同意），本 skill 不设侧门、跑在当前侧
- 指望本 skill 下"快了 x%"的结论——它报数与下推观察，比值与解读由人给

## 流程

1. `plan`（默认）：探测 Flink / nexmark、判定**当前侧**（lib 有无 gluten/velox jar → side，不拒绝）、从生效 conf 推导 REST 并探活、数槽位（这两项是**告警不是门**，`--skip-check` 可整个跳过），打印矩阵（6 查询 × 并行度档）、join 家族与 conf 改动预览；blocking 前置（安装缺失 / conf 不可写或为链接 / 崩溃遗留原件已丢）直接拒绝并给 fix
2. `run --yes`：取两份 conf 旁的**运行锁**（内核 `flock` 排它，另一轮在跑则拒绝；崩溃者的锁随进程消散、遗留锁文件直接覆写接管，从不 unlink；plan 只读不锁）→ 备份两份 conf（同目录临时文件写完 fsync 后原子就位，不留半份备份；记下原件 uid/gid）→ 换入固定 conf（REST 行按本集群推导值）→ 逐格设 `parallelism.default` / `pipeline.object-reuse: false` → 提交 runner、限时等待（超时 / SIGINT / SIGTERM 格都**按归属尽力取消本格的集群作业**：优先 runner 输出里点名的 jid，其次本格开始后新出现的作业，取不到基线才退时间窗并标 imprecise）→ GlutenFlink 侧在每格结束后经 REST 取该格作业的 plan（引擎字段判定 join 相关 gluten 节点，跨格去重）→ 指标表与 `*.submit.log` 落证据树 `<项目>/e2e/perf/join/`（逐文件带 `--out` 同款守卫：**判定与写回同用解析后的 realpath（内核语义：先跟 symlink 再解释 `..`，词法折叠看不出 `link/../x` 越界）**，带 symlink 组件一律不写穿（含指回项目内的）、解析后须在项目内、fifo 不挂死，格开始与写回时都查）→ **失败即停**（未跑的格标 NOT RUN，重跑续矩阵）→ 无论如何恢复 conf（恢复后把原 uid/gid `chown` 回去，`copystat` 不复制属主）
3. 结果 JSON（`flink_velox_nexmark_join_result`）：`runs[]`（与 performance-verify 同键集，另加 plan 证据键）+ `join_analysis[]`（每查询 join_type / sides / key_columns / predicate / throughput_avg / pushdown 及 detail）+ `summary[]`（regular / window-join / lookup / cascade 四组均值，组内 = 各查询均值的均值）+ `warnings[]`（REST / 槽位告警）+ `confs_restored`（逐份读回逐字节核对，如实置值）

## 铁律

- **查询集钉死**：六个 join 查询硬编码，无 `--queries` 参数——覆写直接 unrecognized（exit 2）；`--parallelism` 只裁档不动集
- **口径钉死且跨 skill 可比**：口径文件是 performance-verify 那份的逐字节拷贝（事件量 / tps / sink / warmup / r0 object-reuse），不接受现场改数；要改口径先改源头 conf 并重新过检视、本 skill 拷贝逐字节跟同步
- **跑当前侧，不设侧门**：lib 里的 jar 只决定 side 报告值；GlutenFlink 侧才有下推判定，native 侧恒 not_applicable
- **下推判定只认引擎字段**：operator / operator_strategy / input 的 ship_strategy / exchange 提 gluten（join 相关再加 join）；**绝不读 description**（嵌 SQL 文本，表名带 gluten / 查询名带 join 都会假判定）；判定是观察不是门——`no`（回退）不改退出码，人读报告标 FAILED
- **conf 必归位**：两份 conf 的原件在跑完 / 失败 / SIGINT / SIGTERM 后都必须回到原位；上次崩溃留下的备份自动修复，原件丢失（备份也没了）时拒绝执行而不是覆盖（performance-verify 崩溃留下的同款状态同样拒绝——口径同款分不清谁家，只认自家 `.flink-velox-join.bak`）；conf 或其 `.bak` 是符号链接也拒绝；备份经同目录临时文件**原子就位**；两份 conf 旁有**运行锁**（`<conf>.flink-velox-join.lock`，内核 flock），另一轮在跑时拒绝启动，崩溃者的遗留锁文件直接接管、从不 unlink；恢复逐份读回核对后把原 uid/gid chown 回去（管理员代跑不把 conf 留成管理员所有，chown 失败不拦恢复、如实记 changes），`confs_restored` 如实置值（拒绝态 true——未触碰即原件；tool aborted 态 null——崩溃无法核对不猜值）；证据文件同样逐文件守卫（**判定与写回同用解析后的 realpath，内核语义先跟 symlink 再解释 `..`**——带 symlink 组件一律不写穿（含指回项目内的）、解析后须在项目内、fifo 不挂死，格开始与写回时都查）
- **作业须到 FINISHED**：runner 成功但没打出指标表视为失败，不让空表进汇总
- **检查尽力不拦**：REST 不应答 / 槽位不够是 `warnings[]` 不是门（`--skip-check` 连探测都跳）——join 矩阵不因 REST 抖动误 blocked，格子的成败自己说话
- **只报数与观察不下结论**：吞吐、下推状态照录，可比性标注到格；加速比与解读由人结合两侧报告给

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`side`、`runs`（含 `status` / `metrics` / `comparable` / plan 证据键 / 证据路径）、`join_analysis`（含 `pushdown`）、`summary`、`warnings`、`changes`、`confs_restored`、`next_step`、`error_message` 必须保留（下例 run 一格，节选；plan 的 `runs` 为空、`confs_restored` 缺席，run 的拒绝态恒为 `true`（未触碰即原件）、中止态为 `null`（无法核对））：

```json
{
  "flink_velox_nexmark_join_result": {
    "success": true,
    "checked_at": "2026-11-26T10:00:00",
    "command": "run",
    "side": "gluten",
    "gluten_jars": ["gluten-flink-1.19.2.jar", "velox4j-1.0.jar"],
    "flink_home": "/usr/local/flink-1.19.2",
    "nexmark_home": "/usr/local/nexmark",
    "rest": {"address": "localhost", "port": 8081},
    "slots": {"tms": 1, "per_tm": 8, "total": 8},
    "calibre": {"events": 100000000, "tps": 50000000},
    "queries": ["q3", "q7", "q8", "q13", "q20", "q23"],
    "runs": [
      {"query": "q3", "parallelism": 1, "object_reuse": false, "status": "done",
       "runner_exit": 0, "duration_s": 63.2,
       "metrics": {"query": "q3", "events_num": 100000000, "cores": 8, "time_s": 42.5,
                    "throughput": 2352941.2, "throughput_per_cores": 294117.6},
       "comparable": true, "flink": {"parallelism.default": 1, "pipeline.object-reuse": false},
       "gluten_nodes": 3, "join_gluten_nodes": 1, "plan_checked": true, "plan_jids": ["abc123"],
       "plan_problem": null,
       "table": "e2e/perf/join/q3.p1.out", "submit_log": "e2e/perf/join/q3.p1.submit.log",
       "detail": "100000000 events in 42.5s, 2352941.2 events/s"}
    ],
    "join_analysis": [
      {"query": "q3", "join_type": "regular join（双流）", "sides": "auction ⋈ person",
       "key_columns": "auction.seller = person.id",
       "predicate": "auction.category = 10 AND person.state IN (OR, ID, CA)",
       "group": "regular", "throughput_avg": 2352941.2, "cells_done": 1,
       "cells_comparable": 1, "pushdown": "yes",
       "pushdown_detail": "join-related gluten nodes in the plan of 1 of 1 checked cell(s)"}
    ],
    "summary": [
      {"group": "regular", "queries": ["q3", "q20"], "avg": 2352941.2, "cell_count": 1},
      {"group": "window-join", "queries": ["q7", "q8"], "avg": null, "cell_count": 0},
      {"group": "lookup", "queries": ["q13"], "avg": null, "cell_count": 0},
      {"group": "cascade", "queries": ["q23"], "avg": null, "cell_count": 0}
    ],
    "changes": [
      {"action": "backed up", "detail": "/usr/local/flink-1.19.2/conf/config.yaml -> …/config.yaml.flink-velox-join.bak"},
      {"action": "restored", "detail": "…/nexmark.yaml.flink-velox-join.bak -> /usr/local/nexmark/conf/nexmark.yaml"}
    ],
    "warnings": [],
    "confs_restored": true,
    "next_step": "1 cell(s) done; the metrics are in e2e/perf/join and the join analysis is in this result",
    "error_message": ""
  }
}
```

## 脚本工具

- `scripts/nexmark_join.py` — 主脚本：`plan` / `run --yes`；`--flink-home` / `--nexmark-home` / `--parallelism` / `--timeout` / `--skip-check` / `--out` / `--overwrite` / `--json`（无 `--queries`——固定集不接受覆写）

## 参考资料

- `references/join-table.md` — 固定查询集与 join 语义对照表（供检视人核对）、口径、下推判定、conf 换装与恢复纪律、拒绝 / 告警判据、输出契约
- `conf/nexmark-bench.yaml` — 固定基准口径（performance-verify 同一份的逐字节拷贝，唯一事实来源在源头 skill）
