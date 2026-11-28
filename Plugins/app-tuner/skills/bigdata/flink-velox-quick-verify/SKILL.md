---
name: flink-velox-quick-verify
description: >
  Flink SQL 单侧快速语义校验：内置场景库（v1：datetime-expr 十个时间提取函数、
  agg-changelog GROUP BY 聚合折叠、filter-project 谓词投影、two-way-join 双流
  join）固定 CSV 输入经 sql-client 提交到当前在跑的那一侧（原生 Flink 或
  GlutenFlink，从 lib 判定并记录），从 TM .out 快照差集捕获 print changelog，
  折叠为终态结果集后与场景自带 Python 参考模型（ground-truth 子进程运行）做
  行级多重集比对；失配按首列配对做字段级归因点名出错表达式。空捕获、折叠错
  误、两侧 plan 门（GlutenFlink 侧无 gluten 节点 / native 侧有）一律 FAIL。
  plan 零提交零写盘，run 须 --yes。不动 conf 不切栈。
  触发词：快速语义校验、单侧验证、参考模型对比、ground-truth 对比、字段级归
  因、print 捕获对比、时间函数校验、DAYOFWEEK 校验。
  不负责：两侧等价裁决（同 SQL 双栈对比）→ flink-velox-e2e-verify；吞吐基准
  → flink-velox-performance-verify；切栈/重启集群 → 调用方或
  flink-velox-cluster-switch。
compatibility: Linux（openEuler 验证），Python 3 标准库；需在跑的 Flink 1.19 单机集群（REST 端点从 $FLINK_HOME 生效 conf 推导）、可读 $FLINK_HOME/log（print 捕获）、可执行 $FLINK_HOME/bin/sql-client.sh；会提交作业并在项目根 e2e/quick/ 证据树写捕获与报告（--overwrite 前不覆盖 --out 既有文件）；本 skill 不改任何 conf、不切栈、不重启集群
metadata:
  skill_type: tool_assisted
  category: verification
  domain: bigdata
  side_effect_free: "false"
  version: "1.1.0"
---

# Flink-velox 单侧快速语义校验

本 skill 在鲲鹏 Flink 向量化加速闭环中承担**单侧快速冒烟**：把内置场景的固定输入提交到**当前在跑的那一侧**（原生 Flink 或 GlutenFlink，从 lib 判定并记录），捕获 print sink 输出折叠为终态结果集，与场景自带的 **Python 参考模型**（ground-truth，子进程运行、零代码共享）做行级多重集精确比对；失配时做**字段级归因**逐列点名出错表达式——改完下推/算子/表达式，先一条命令确认"这一侧语义没坏"，再谈双跑等价与吞吐。

## 与相邻 skill 的分工

**本 skill** 回答"这一侧语义对不对"（单侧 + 参考模型 + 字段级归因）；两侧等价裁决（同 SQL 双栈零容差对比）用 flink-velox-e2e-verify；吞吐基准用 flink-velox-performance-verify。三者互补，不互相替代。

## 场景库

场景随 skill 交付，落在 `scenarios/<name>/`，v1 四个：

| 场景 | 检验面 | 语义锚定（详见各 README.md） |
|---|---|---|
| `datetime-expr` | YEAR/MONTH/DAYOFMONTH/DAYOFYEAR/DAYOFWEEK/QUARTER/HOUR/MINUTE/SECOND/WEEK 十个时间提取函数 | UTC 挂钟输入+session-timezone 往返不变；DAYOFWEEK Flink 口径 Sun=1..Sat=7（曾暴露 velox 返回 raw tm_wday 真 bug）；WEEK 按 ISO 8601；含跨 ISO 年/季界样例 |
| `agg-changelog` | GROUP BY count/sum + changelog 回撤 | 终态结果集折叠语义，丢回撤/终态错算过不去；只校验**终态一致性**（见下） |
| `filter-project` | 谓词 + 投影 + 表达式运算 | 输入钉死 `>` 与 `>=` 边界行 |
| `two-way-join` | 双流 regular join（同 key 多事件） | 终态=同 key 全组合；单侧 key 零产出钉死丢侧 bug |

每场景四件套：`case.sql`（CSV 源建表 + print sink + INSERT，**路径占位符 `{{INPUT_DIR}}`**，执行器替换为输入目录绝对路径落证据树再提交）；`input/`（固定小型确定性 CSV，无随机数据；字符串字段不含逗号、竖线、方括号——print 行字段边界约束）；`reference.py`（**唯一入口 `expected(input_dir) -> list[str]`**，返回与 Flink print 逐字节一致的 `+I[a, b, c]` 行；v1 全为终态结果集；`python3 reference.py expected <input_dir>` 打印 JSON 数组。参考实现本身受测：tier-A 把四个 expected() 与手工核对值钉死）；`README.md`（语义依据与历史坑）。plan 与 run 都校验完整性：缺 case.sql/input/reference.py 任一件的场景**逐个 FAIL**（detail 点名缺什么），不静默消失——不存在"零场景全过"。

## 执行步骤

### 步骤 0：plan（默认）

```bash
python3 <skill_dir>/scripts/quick_verify.py plan --all
```

探测 Flink 位置（见环境约定）、lib 判定当前侧、REST 探活、sql-client 可执行校验、场景完整性；打印将跑场景、逐场景命令与证据路径。**零提交零写盘**（`--out` 是唯一显式要求的写盘）。

### 步骤 1：run --yes

`--yes` 是同意门（用户看过 plan 才允许执行）；单场景用 `--scenario <名>`（可重复）。

| # | 步骤 | 内容 |
|---|---|---|
| 1 | 门 | lib 构建门判定当前侧 + REST 探活 + 场景完整性 + 证据路径护栏 |
| 2 | 提交 | 替换占位符落 `<scenario>.submit.sql`，sql-client.sh 提交，REST 跟踪到 FINISHED（first-job 超时、`--timeout` 预算；客户端挂死不返回时按失败规则走残留作业协议；提交失败区分**客户端未启动**（退出 126/127）与**启动但退出非零**） |
| 3 | 捕获 | TM .out **快照差集**只取本次新增行；changelog 行正则**行内任意位置**，容忍 log4j 与 `3> ` 子任务前缀；按文件身份（inode/mtime/size/边界行）识别轮转/截短 → 从头重读，detail 注明 capture may be incomplete |
| 4 | 参考 | 子进程跑 `reference.py expected(input_dir)`（ground-truth 与执行器零代码共享） |
| 5 | 比对 | 折叠（`+I/+U` 加入、`-U/-D` 移除所携带行）后**终态行级多重集相等**；两侧都取作业 plan 走对称门 |

失败规则（一律 FAIL，不产生假阳性）：

- **空捕获 = FAIL**——0 changelog 行不是"空结果也一致"；捕获行永不终止、回撤从未存在的行 = FAIL（捕获截断/折叠错误）；
- **plan 双门对称**：GlutenFlink 侧 plan 无 gluten 节点 = FAIL（**引擎字段匹配法**：operator / operator_strategy / inputs 的 ship_strategy / exchange；`description` 内嵌用户 SQL 永不参与——表名叫 gluten_orders 也不该误判）；**native 侧 plan 出现 gluten 节点 = FAIL**——lib 判为 native 但集群仍跑 GlutenFlink planner（挪 jar 不等于切栈，需在 native 构建重启），不报 native 绿；plan 取不到 = FAIL（不可验证就不放行）；
- 缺件场景 = FAIL；证据（含 FINISHED 记录 .job.json）写不进 = FAIL；参考模型自身崩溃/输出不合契约 = FAIL（detail 明示是参考实现的失败）。

多场景串行跑、互不中断：一个场景失败后续场景照跑（每场景独立）。例外：**sql-client 挂死超时**、作业跟踪超时或 REST 跟踪中断的场景会先照 performance-verify 的模式**尽力取消本场景自己的残留作业**（REST cancel 后确认终态；客户端挂死时取消的是超时窗口内提交、仍在 RUNNING 的作业，全已终态则视为已清）——取消确认后下一场景照常提交；取消无法确认（作业 ID 一并记录在报告里）时剩余场景**标 skipped** 不再提交——两份 print 混入同一捕获会误判失配。

## 证据树

`e2e/quick/` 在项目根（跨任务沉淀、每轮原地刷新）：`<scenario>.submit.sql`（实际提交的 SQL）、`.submit.log`（sql-client 输出）、`.job.json`（作业状态/FINISHED 记录）、`.out`（捕获的 changelog 行）。每个证据路径与 `--out` 同款守卫（见硬规矩），写不进该场景就 FAIL。

## 硬规矩

- **不动 conf 不切栈**：不改任何配置、不重启集群、不取消无关作业；唯一例外是上面"尽力取消本场景自己超时的残留作业"。栈在哪一侧由调用方决定，本 skill 从 lib 判定并如实报告；切栈找 flink-velox-cluster-switch。
- **同意门**：提交作业类动作须 `run --yes`；plan 只探测打印，零提交零写盘。
- **`--out` 护栏**（动作前校验）：判定与落盘用同一条 `os.path.realpath` 解析路径（内核语义：先解 symlink 再解释 `..`，词法折叠看不出 `link/../x` 越界），带 symlink 组件不写穿、项目外拒、已存在需 `--overwrite`、非普通文件 `--overwrite` 也拒、**与所选场景任一证据路径冲突直接拒**（场景先写捕获末尾写报告，冲突会让报告失败或 --overwrite 毁掉捕获）。
- **中止态全键**：工具异常退出时输出同一套键（观测位空值，`error_message: tool aborted: ...`）。
- **命令带解释器与绝对路径**：报告给出的可执行命令照抄能跑。
- 场景数据固定、有界、无随机；集群上同一时刻只跑一个受测作业（print 捕获按"REST 快照差集"识别，并发作业会被误认）。

## 术语

- **GlutenFlink**：加载了 gluten/velox4j jar 的 Flink 构建（velox 向量化栈）。「原生 Flink / native」指 lib 里没有这些 jar 的纯 Apache Flink。两者合称"侧"（side）。用户可见文本一律写 GlutenFlink 或原生 Flink，不写未声明的缩写——GlutenFlink 的缩写只以契约值 `gfv` 的形式出现在代码里（GFVBot 除外）。
- **当前侧**：执行时集群实际在跑的构建，从 `$FLINK_HOME/lib` 判定（gluten/velox jar 在即 GlutenFlink）；plan 门在两侧对称兜底 lib 与集群不一致的情况。
- **参考模型 / ground-truth**：场景内 reference.py，独立于 Flink 实现的期望值来源。
- **changelog 行**：print sink 打出的 `+I[...]`/`-U[...]`/`+U[...]`/`-D[...]` 行；回撤携带完整旧值，折叠后即终态结果集。
- **字段级归因**：失配时把两侧行按首列尽力配对（同 key 中取列值最接近者），逐列计 mismatch 与样例；配不上的行单独计数（unpaired_missing / unpaired_extra）。首列不唯一时归因尽力而为，行级裁决不受影响。

## 输出

`flink_velox_quick_verify_result` 契约（`--json` 打印；对话汇报同结构）：

```json
{"flink_velox_quick_verify_result": {
  "success": false, "checked_at": "...", "command": "run", "side": "native",
  "flink_home": "/usr/local/flink", "rest": "http://localhost:8159",
  "evidence_dir": "e2e/quick", "timeout": 600,
  "scenarios": [{"name": "filter-project", "status": "fail", "side": "native",
    "rows_expected": 4, "rows_captured": 4, "column_diffs": [{"column": "qty",
    "mismatches": 1, "samples": [{"expected": "6", "actual": "5"}]}],
    "unpaired_missing": 0, "unpaired_extra": 0, "gluten_nodes": 0,
    "capture": "e2e/quick/filter-project.out", "detail": "result set mismatch: ..."}],
  "next_step": "fix the failed scenario(s) — ...",
  "error_message": "1 scenario(s) failed: filter-project"}}
```

- `status`：`pass` | `fail` | `skipped`（plan 下为 skipped，detail 说明原因；残留作业未确认终止时剩余场景 skipped 并带原因与作业 ID）。
- `side`：当前侧（native / GlutenFlink）；`gluten_nodes` 在两侧 plan 校验后都填数（native 正常为 0）。
- 比对标准：折叠后的**终态**多重集精确相等——不多行、不少行、任何行字段值无差异、重复行计数参与比较、行序不参与；一行不一致即场景失败。**中间 changelog 更新不在校验范围**：print sink 协议下中间步骤无法独立校验（如首次小计错、回撤同错值、终态正确仍会判 PASS）；需要校验中间态须用 retract 感知的验证器。日志轮转/截短时 detail 注明 capture may be incomplete（不自动 FAIL 但如实报告）。
- 人读报告：场景逐行 PASS/FAIL/SKIPPED + 失败场景字段归因摘要 + SIDE/PASS 汇总。
- 退出码：plan 0 / 2 拒绝或中止（含缺件场景）；run 0 全过、1 有场景失败（其余照跑）、2 拒绝（前置不满足 / 缺 --yes / --out 护栏）或工具中止。

## 环境约定

- Flink 位置：`--flink-home` → `$FLINK_HOME` → 工作区档案 `.gfvbot/env.json` 的 `stack.flink.path`（自当前目录向上逐级查找），都缺 fail-fast。
- REST 端点：`FLINK_REST_URL` → `$FLINK_HOME/conf`（config.yaml 嵌套块或 flink-conf.yaml 扁平键，后者并存时胜出）→ `http://localhost:8081`。REST 调用**无视环境代理**。conf 可解析时**不要**设 `FLINK_REST_URL`——不一致会让跟踪器"看不到新作业"。
- `--timeout`（秒，默认 600）限每场景等 FINISHED；first-job 出现另设 `min(timeout, 90)` 上限，INSERT 失败不吃满预算。
- 集群须单机：print 捕获只读本机 `$FLINK_HOME/log`，远端 TaskManager 分区的行不可见——但捕获不全会向 FAIL 方向失安全（行数对不上即失配），不会假阳性。
- 临时产物一律落项目目录内，绝不写 `/tmp`。

## 脚本工具

- `scripts/quick_verify.py` — 主执行器：`plan`（默认）/ `run --yes`；`--scenario`（可重复）| `--all`、`--flink-home`、`--timeout`、`--json`、`--out`、`--overwrite`。
