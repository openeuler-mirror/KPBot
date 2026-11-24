---
name: flink-velox-e2e-verify
description: >
  Flink SQL 双栈端到端等价性验证——同一条 SQL 在原生 Flink 与 GlutenFlink
  （velox 向量化栈）上以完全相同的固定输入双跑，把 print sink 的 changelog
  输出（+I/-U/+U/-D）按多重集折叠还原为最终结果集，两侧零容差精确对比，
  产出 MATCH/MISMATCH 等价性裁决。每侧四道门禁：lib 构建门（防错栈基线）、
  REST 跟踪作业到 FINISHED、运行计划栈校验（plan 内 gluten 算子节点与模式
  匹配，防"换了 jar 没重启"与"整条 SQL 回退原生算子"）、捕获非空。用例与
  证据沉淀在项目根 e2e/ 证据树，形成可回归的用例库。适用于鲲鹏/ARM 上
  Flink 向量化加速（下推表达式、算子、聚合）优化前后的行为一致性验证、
  加速栈版本升级后的等价性回归，以及任何"同一 Flink 作业两种执行配置
  （基线 vs 优化）"的双跑对比验证。
  场景入口类触发：Flink 下推验证、GlutenFlink 输出对比、双跑一致性、加速
  前后结果一致性、velox 算子行为验证。
  验证协议类触发：changelog 对比、回撤流折叠、print sink 捕获、SQL 等价性
  门禁、Flink 结果集精确对比。
  不负责标准吞吐/耗时基准（nexmark/TPC-DS 查询集与规模因子口径）与瓶颈
  定位（profiling 类 skill）；无界流上的持续验证以有界输入让作业到 FINISHED
  为准。
compatibility: Linux（openEuler 验证），bash + Python 3 标准库（无 YAML 依赖）；需在跑的 Flink 1.19 集群（REST 端点从 $FLINK_HOME 生效 conf 推导）、可读 $FLINK_HOME/lib 与 log/（print sink 捕获）、可执行的 $FLINK_HOME/bin/sql-client.sh；会提交作业并在项目根 e2e/ 证据树写捕获与报告（--overwrite 前不覆盖既有文件）；栈切换重启由调用方负责，本 skill 绝不自动重启集群
metadata:
  skill_type: tool_assisted
  category: verification
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# Flink-velox e2e 双跑验证

对单条 SQL 在原生 Flink 与 GlutenFlink 之间做双跑验证：同一固定输入、同一 SQL，两侧各跑一次，print sink 的 changelog 折叠还原为最终结果集后**零容差精确对比**。在鲲鹏 Flink 向量化加速闭环中，本 skill 承担**等价性门禁**环节：下推改写、算子替换、栈升级，必须先过"输出与基线精确一致"，任何后续优化讨论才有意义。

changelog 折叠语义（为何无需 key 即可还原最终结果集）见 [changelog-semantics.md](references/changelog-semantics.md)，此处只给协议。

## 使用场景

### 适用（应使用本 skill）

- 为 Flink 实现了 velox 下推（表达式 / 算子 / 聚合），需验证其行为与原生 Flink 一致
- GlutenFlink / velox4j / velox 任一栈升级后，回归验证既有下推是否仍等价
- 任何"同一 Flink SQL 作业、两种执行配置（基线 vs 优化）"的输出一致性验证

### 不适用（不要用本 skill）

- 吞吐/耗时基准（nexmark、TPC-DS 查询集与规模因子口径的提速测量）→ 专用基准 skill；本 skill 不计时、不报加速比
- 瓶颈定位 / profiling → profiling 类 skill
- 跨引擎对比（Flink vs Spark）——changelog 语义是 Flink 特有的
- 无界流上的持续验证——本协议以有界输入让作业到 FINISHED 为准

## 测试形态

每条用例同一形态，两栈看到的输入字节一致：

```sql
CREATE TABLE src (
  <按用例数据写列与类型>
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://<绝对数据目录>',
  'format' = 'csv'
);
CREATE TABLE sink (
  <与 SELECT 输出列一致的列与类型>
) WITH ('connector' = 'print');
INSERT INTO sink SELECT <受测表达式或查询> FROM src;
```

规则：

- 输入永远是用例目录下的有界 CSV——不用随机或运行期生成的数据，不用无界 source。
- sink 永远是 `print`。有界输入结束作业；作业只有到 FINISHED 才算成功。
- 一条用例 = 一个 SQL 文件。用例命名 `NNN_<intent>.sql`，落**项目根** `e2e/sql/` 下，数据落 `e2e/data/NNN_<intent>/`。
- 用例应产出**非空**结果集：空捕获在本协议里是失败而非"空结果也一致"（见四道门禁之四）。

## 证据树：`e2e/` 在项目根，不按任务收纳

SQL 表达验证范围、数据是固定输入、输出是运行捕获、diff 是裁决依据——都是验证交付的证据，跨任务沉淀，故放项目根；`tmp/` 只放运行日志：

```
e2e/
  sql/      NNN_<intent>.sql                     验证范围
  data/     NNN_<intent>/*.csv                   固定输入
  out/      NNN_<intent>.native.out              两侧捕获（最新一轮，原地刷新）
            NNN_<intent>.gfv.out
            NNN_<intent>.*.submit.log            提交日志，随 out 自动生成
            NNN_<intent>.*.job.json              作业状态/时长/栈校验记录
  verify/   NNN_<intent>.diff.txt                最新对比报告（原地刷新）
            RESULTS.md                           全量结果汇总（每轮原地刷新）
```

- 同名用例再现时先比对内容：一致即复用（这就是回归用例库），不一致换 intent 名。
- 单跑覆盖 `NNN.<side>.out`，不累积重复文件。

## 每侧四道门禁

`run-sql.sh` 的退出码 0 意味着一侧**完全确认**，四道门禁全过：

| # | 门禁 | 拦截的失败模式 | 豁免 |
|---|---|---|---|
| 1 | lib 构建门：`$FLINK_HOME/lib` 内 gluten/velox jar 与模式一致（native 无、gfv 有） | 对着错栈基线提交 | `RUN_SQL_SKIP_BUILD_CHECK=1` |
| 2 | FINISHED：REST 跟踪提交的作业到终态，非 FINISHED 即失败 | sql client 提交后立即返回，作业失败被当成成功 | 无 |
| 3 | 栈校验：作业 plan 内 gluten 节点与模式一致（gfv ≥1、native 0） | "删/加了 jar 但没重启集群"（planner 类仍驻留）；gfv 侧整条 SQL 回退原生算子 | `RUN_SQL_SKIP_PLAN_CHECK=1` |
| 4 | 捕获非空：零行捕获即失败 | 失败作业/漏捕获被当成"空结果也一致"通过 | 无 |

退出码：0 该侧完成；1 作业失败 / 栈失配 / 空捕获 / 提交失败；2 REST 不可达；3 超时未 FINISHED。**非 0 即该侧未完成：修复后重跑该侧，绝不带着失败的一侧切栈。**

## 执行步骤

### 步骤 0：出运行计划（候选命令）

执行前先产出运行计划 JSON（候选动作，供调用方/上游编排审查门控后再执行）：

```json
{
  "flink_velox_run_plan": {
    "case": "003_window_tumble",
    "steps": [
      {"side": "native", "expect_lib": "no gluten/velox jars", "expect_plan": "0 gluten nodes",
       "command": "bash <skill_dir>/scripts/run-sql.sh native e2e/sql/003_window_tumble.sql e2e/out/003_window_tumble.native.out"},
      {"side": "gfv", "expect_lib": "gluten/velox jars present", "expect_plan": ">=1 gluten node",
       "command": "bash <skill_dir>/scripts/run-sql.sh gfv e2e/sql/003_window_tumble.sql e2e/out/003_window_tumble.gfv.out"}
    ],
    "switch_point": "两侧之间的切栈重启只发生在 native 侧退出 0 之后，且须先征得用户同意",
    "requires": ["native 侧提交前集群为原生构建", "gfv 侧提交前集群已加载 GlutenFlink 构建并重启"]
  }
}
```

### 步骤 1：备用例

按「测试形态」备固定 CSV 与 SQL，落 `e2e/{sql,data}/`。

### 步骤 2：native 侧

```bash
bash <skill_dir>/scripts/run-sql.sh native e2e/sql/NNN_<intent>.sql e2e/out/NNN_<intent>.native.out
```

脚本依次执行：lib 门 → REST 快照 → 提交 → REST 跟踪到 FINISHED → plan 栈校验 → 捕获（快照差集：只取提交后新增行，剥离子任务前缀 `3> `）→ 非空检查。旁车落 `*.submit.log` 与 `*.job.json`（作业状态、时长、gluten 节点数、异常栈）。

### 步骤 3：切栈（确认后再重启）

**只有上一侧 `run-sql.sh` 退出 0（作业 FINISHED + 栈校验通过 + 捕获落盘非空）之后，才允许重启集群切栈**——成功输出会明确打印 `side complete ... the cluster may be switched now`。理由：

- 重启会清空 `$FLINK_HOME/log/*`，未落盘的捕获证据会随 TM 日志一起丢失；
- 未完成的侧可能还有作业在跑，带作业重启会留下僵尸 TaskManager。

本 skill **绝不自动重启集群、绝不自动取消作业**：切栈重启前须单独征得用户同意；超时/失败后作业可能仍在集群上运行，取消与否由用户决定。

### 步骤 4：gfv 侧

集群以 GlutenFlink 构建重启后：

```bash
bash <skill_dir>/scripts/run-sql.sh gfv e2e/sql/NNN_<intent>.sql e2e/out/NNN_<intent>.gfv.out
```

gfv 侧的栈校验若报"无 gluten 节点"，看 `*.job.json` 里的 `tm_velox4j_lines` 分诊：TM 日志有 velox4j 行 = 集群加载了 GlutenFlink 栈但这条 SQL 整条回退原生算子（换用例）；一行都没有 = 集群根本不是 GlutenFlink 构建（切栈没做成）。

### 步骤 5：折叠对比

```bash
python3 <skill_dir>/scripts/changelog_diff.py \
  e2e/out/NNN_<intent>.native.out e2e/out/NNN_<intent>.gfv.out \
  > e2e/verify/NNN_<intent>.diff.txt
```

多重集折叠：`+I`/`+U` 加入载荷行、`-U`/`-D` 移除载荷行（回撤携带完整旧值），行序不参与比较；折叠错误（回撤不存在的行）与任一侧空捕获均判 MISMATCH。

### 步骤 6：汇总与输出

全部用例跑完写 `e2e/verify/RESULTS.md`（原地刷新）：每用例一行，含两侧 FINISHED 状态与 gluten 节点数、两侧 changelog 行数与标志统计、裁决（MATCH/MISMATCH）、diff 路径；末行汇总通过数。

## 环境约定

- Flink 安装位置取值顺序：环境变量 `FLINK_HOME` → 工作区档案 `.gfvbot/env.json` 的 `stack.flink.path`（GlutenFlink 工作区，自**当前工作目录**向上逐级查找）。两者都缺则 fail-fast。
- 覆盖护栏：`run-sql.sh <out-file>` 与 `flink_job.py wait --out` 对已存在文件默认拒绝（退出 2），重跑覆盖须显式带 `--overwrite`；所有写落路径必须在项目目录内且不得是 symlink，旁车 `.submit.log`/`.job.json` 随新一轮原地刷新。
- REST 端点取值顺序：`FLINK_REST_URL` → `$FLINK_HOME/conf`（`config.yaml` 的 `rest:` 块或 `flink-conf.yaml` 的 `rest.address`/`rest.port`）→ `http://localhost:8081`。sql client 按 conf 提交，故 conf 可解析时**不要**设 `FLINK_REST_URL`——两者不一致会让跟踪器"看不到新作业"而失败。
- `FLINK_JOB_TIMEOUT`（秒，默认 600）限制等 FINISHED 的时长；`FLINK_JOB_POLL`（秒，默认 1）轮询间隔。
- **集群运行哪个构建由调用方负责**：构建与集群重启不在本 skill 范围内；`run-sql.sh` 的门禁只做失配拦截，不做自动切栈。
- 双跑期间集群上同一时刻只跑一个受测作业（作业以"REST 快照差集"识别，并发作业会被误认）。
- 临时产物一律落项目目录内，绝不写 `/tmp`。

## 输出

对话与上游编排可消费的结构化结论：

```json
{
  "flink_velox_e2e_result": {
    "success": true,
    "case": "003_window_tumble",
    "native_finished": true,
    "gfv_finished": true,
    "native_gluten_nodes": 0,
    "gfv_gluten_nodes": 2,
    "native_rows": 1000,
    "gfv_rows": 1000,
    "native_flags": {"+I": 1200, "-U": 150, "+U": 150, "-D": 50},
    "gfv_flags": {"+I": 1200, "-U": 150, "+U": 150, "-D": 50},
    "final_rows": 1000,
    "verdict": "MATCH",
    "diff_path": "e2e/verify/003_window_tumble.diff.txt",
    "error_message": ""
  }
}
```

- `verdict`：`MATCH`（精确一致）| `MISMATCH`（结果集差异或折叠错误）| `NOT_FINISHED`（任一侧 run-sql.sh 非零退出：作业失败/超时/栈失配/空捕获，此时不做对比）。任一非 MATCH 即 `success: false` 并填 `error_message`。
- `*_gluten_nodes` 取自该侧 `*.job.json`，是"这一侧真跑了对应栈"的证据。
- 对比标准：精确一致，零容差——不多行、不少行、任何行的字段值无差异；一行不一致即用例失败，不存在部分通过。两栈间 DECIMAL/TIMESTAMP 的格式差异是真实不一致，不是噪声。

## 边界与相邻 skill

- 吞吐/耗时基准（nexmark / TPC-DS 查询集、规模因子、并发梯度、加速比）→ 专用基准 skill；本 skill 只回答"结果一不一致"，不回答"快了多少"
- 瓶颈定位 / profiling → profiling 类 skill
- Flink/Spark 配置参数调优（TaskManager 内存、并行度）→ 框架调优类 skill
- GlutenFlink 栈构建（velox4j / gluten-flink 编译部署）→ 构建侧工作流，本 skill 只消费其产物
