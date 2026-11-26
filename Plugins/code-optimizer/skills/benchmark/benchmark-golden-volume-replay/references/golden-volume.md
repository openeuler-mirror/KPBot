# 黄金卷 + clone 复用全量（标定案例）

## 1. 目标

多轮基准的每轮底库必须一致。每轮重 seed 会：① 引入数据池漂移；② 付出 seed + HNSW 索引构建的
恒定成本（10–20 分钟）。黄金卷把 build 轮的固化底库复用给后续所有复用轮。

## 2. 流程图

```
build 轮  : 前缀切黄金前缀 → 生成 compose → seed → finalize(flush+compaction) → 停容器 → seal-golden
                                                                                         ↓
                                                            seal state 持久化 (runtime/state/golden-seal.json)
                                                                                         ↓
复用轮 N  : probe 校验就绪 → clone 黄金卷 → 跳过 seed → ingestion/qa → 删除克隆卷
```

## 3. 关键机制

- **golden prefix 切换**：`MILVUS_GOLDEN_VOLUME_PREFIX` 决定 build 轮卷前缀；组间独立前缀隔离（统计设计用）。
- **finalize**：flush + compaction，把 HNSW 索引固化进卷，clone 后无需重建。
- **seal state**：`seal-golden` 成功后写 `golden-seal.json`（finalize 只 flush+compaction、不写 seal 标记）；复用轮据此判断"已有黄金卷、跳过 build"，cleanup 据此跳过清理。
- **seal-golden 子命令**：免手动组装参数；分片形态自动合并 shard summaries；幂等。

## 3.5 CLI 来源与最小命令（首次使用者从这里接手）

**所属 CLI 与固定版本来源**：`seal-golden` / `probe` / `clone` 都来自 mem0/milvus 联调项目的
benchmark 子仓（`modules/benchmark/`，随子仓 `manifest.yaml` 钉版本）。入口编排脚本是
`mem0_locomo.sh`（`seal-golden` 是它的一个子命令），黄金卷 Python 工具是
`milvus_golden_volume.py`（位于基准子仓 `modules/benchmark` 下；`probe`/`seal`/`clone`/`cleanup` 四个 action）。本 skill 不随包
分发该子仓，拿不到时把"已解包的 benchmark bundle"列为外部必需输入，不要臆造路径。

**最小命令与成功判据**（在 runner 模式下、bundle 已解包到 `modules/benchmark`）：

| 动作 | 最小命令 | 成功判据 |
|---|---|---|
| probe（复用轮入口） | `PYTHONPATH="$ROOT" "$VENV/bin/python" "$ROOT/scripts/milvus_golden_volume.py" probe $(golden_volume_args)` | stdout 为 `ready`；否则为 per-field diff / 无 marker 提示，走 build 分支 |
| seal-golden（build 轮后，栈已下线、runner 容器仍在） | `docker exec mem0-locomo-runner /workspace/mem0_locomo.sh seal-golden` | 日志出现 `Milvus golden volumes sealed (state: .../golden-seal.json)`，可重跑（幂等） |
| clone（复用轮） | `PYTHONPATH="$ROOT" "$VENV/bin/python" "$ROOT/scripts/milvus_golden_volume.py" clone $(golden_volume_args)` | stdout 为 `{"status":"cloned","volumes":[{"golden_volume":...,"runtime_volume":...,"clone_seconds":...}]}` |

- `golden_volume_args` 从持久化 state 自动组装（`--golden-prefix`/`--runtime-prefix`/`--users`/
  `--user-prefix`/`--count`/`--dimensions`/`--seed-version`/`--milvus-image`/`--helper-image`/`--assignments`），
  不接受手工输入 → 杜绝参数漂移。
- **`$ROOT` / `$VENV` / `golden_volume_args` 符号来源与执行位置**：三者都是 `mem0_locomo.sh` 内定义的 shell
  符号——`ROOT` = 脚本所在目录（bundle 根）、`VENV` = runner 容器内 Python venv（`MEM0_LOCOMO_VENV` 覆盖默认
  `${ROOT}/.venv`）、`golden_volume_args()` = 脚本内 shell 函数。probe / clone / seal-golden 均在 **runner
  容器内**执行：宿主用 `docker exec mem0-locomo-runner /workspace/mem0_locomo.sh <子命令>` 转交，脚本内部
  已 source 好这些符号并自行组装参数，首次使用者无需手工加载或手传任何参数。
- **seal 时机**：必须在 build 轮结束、compose 栈停止后执行（`seal-golden` 要求 runner 模式、
  `MILVUS_GOLDEN_VOLUME_PREFIX` 已设、headless seed 模式）。前置不满足时脚本 `die` 并给原因。

## 4. 手动 seal 三类失效（为什么自动化）

| 失效 | 表现 | 自动化解决 |
|---|---|---|
| 忘 seal | 复用轮拿不到固化卷 | seal state 文件 + probe 强制 |
| 参数漂移 | seal 时参数与 build 不一致 | seal-golden 从 state 读参数，不收人工输入 |
| stale assignments | 分片时 assignment 过期 | 分片形态自动合并 shard summaries |

probe 报错升级为 per-field diff + 无 marker 专属提示（而非"seal 失败"笼统话术）。

## 5. 与统计设计的衔接

方案 C（统计设计见 `benchmark-statistical-design`，独立 PR 待合入）的每组 4 轮：
- 第 1 轮 = build + seal 存档轮，**不计分**。
- 第 2–4 轮 = 3 个黄金卷 clone 复用轮，**作为数据点**。
- 组间独立黄金前缀；9 个数据点数据池完全一致（起点/终点行数 + seal 数据摘要实证，见 §5.5）。

## 5.5 数据摘要：行数一致 ≠ 内容一致

各组**独立 build** 时，即便主键、向量或租户归属不同，起点/终点行数仍可完全相同。起止行数相同
只能证明"数据量一样"，不能证明"数据内容一样"。要判定数据池一致，必须在 **seal 时保存按稳定主键
排序的数据摘要**，并在每组及其 clone 之间比对：

- **摘要内容**：按稳定主键（如 trace `user_id` + 记录 id，或 Milvus 主键）排序后计算 ——
  行数、按主键聚合的每租户记录数分布、以及行内容/向量校验和（如逐行主键 + 内容的稳定哈希）。
- **比对口径**：组内（build 轮 vs 其 clone 复用轮）逐个比对；组间（g1/g2/g3）比对各自摘要，
  **摘要一致才判定数据池一致**。行数一致 + 摘要不一致 = 数据池漂移，不得据此下收益结论。
- **落盘位置**：摘要随 seal state（`runtime/state/golden-seal.json`）或 `seed/index summary` 一起持久化，
  复用轮 probe 时一并核对。

## 6. 坑点

- run 前 cleanup 检测到黄金 seal 状态已设则自动跳过（保护黄金卷）；未设则正常清理；
  run 结束 cleanup **必须**删克隆卷，否则下一轮无法重克隆。
- **独占分片形态（MEM0>=RUNNER>1）叠加黄金卷的归属不一致（已知边界）**：
  `milvus_golden_volume.py` 的 user→milvus 期望按**全列表轮转**（`u % MEM0_INSTANCES`）计算，
  与独占分片形态的**片内轮转**归属不一致 —— 若 probe 成功就跳过 seed，后续回放可能在错误实例上查用户。
  **clone 前必须校验分片归属**：待"全列表轮转"与"片内轮转"两端归属校验一致后再启用该组合；
  暂不兼容时使用**共享形态（RUNNER>MEM0）或单 runner**（这两者全一致），并在 contracts 层约束。