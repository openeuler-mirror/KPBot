---
name: flink-velox-cluster-check
description: >
  Flink + GlutenFlink（velox 向量化栈）本安装的 Flink 集群只读检查：从 /proc 找归属本安装的守护进程（按 classpath 里
  flink-dist jar 的真实路径），判它们实际加载的是 native 还是 GFV 构建（classpath 上的 gluten/velox jar）、是否一致（mixed）、
  lib 是否在集群启动后改过（加载的 jar 与磁盘上的不等、或有 jar 晚于最早进程启动时间）、REST /overview 是否应答及版本 /
  TaskManager 数 / 在跑作业（忽略环境代理）。没在跑、JM 无 TM、TM 无 JM、mixed、lib 改过均判 fail；REST 异常与在跑作业给 warn。
  只扫不改：不起停集群、不提交作业。触发词：集群加载的是哪套 jar、lib 与集群一致吗、集群在不在跑、重启了没、REST 应答。
  不负责：lib 里是哪套构建 → flink-velox-lib-state-check；守护进程用的 Java → flink-velox-jdk-check；起停集群 → 切换类 skill
  （规划中）且须用户同意。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库；读 /proc 与 Flink conf，一次 REST GET
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink 集群状态检查

换了 lib 里的 jar 却没重启、或者只重启了一半——集群还在跑它启动时加载的那一套，而 lib 已经是另一套；从"作业没走 gluten 算子"倒查到这一点很慢。本 skill 只做一件事：**核对本安装的守护进程在不在跑、它们实际加载了哪套构建、lib 改了之后集群是否还在跑旧的、REST 说了什么**。它是 `flink-velox-env` 检查家族里"集群状态"这一具体项：只读、不改动机器、不起停集群、不提交作业。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 换 jar / 改 conf 之后、重启之前或之后，确认集群加载的与 lib 一致
- 作业报"没走 gluten 算子"、`UnsupportedClassVersionError` 时，先看集群实际加载的构建
- 要知道本安装的集群在不在跑、几个 TaskManager、REST 是否应答、有没有作业在跑（重启会打断）
- 一台机器上有多个 Flink 安装时，区分哪些守护进程属于本安装

### 不适用（不要用本 skill）

- 指望它起停集群、取消作业——`fix` 与 `next_step` 只是工单，重启须用户同意，交给切换类 skill（规划中）
- lib 里是哪套构建、GFV 集齐不齐 → `flink-velox-lib-state-check`
- 守护进程用哪个 Java、重启会选到哪个 → `flink-velox-jdk-check`
- Flink 安装完整性与版本线 → `flink-velox-flink-dist-check`
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`

## 检查项

| 条目 | 怎么判 |
|---|---|
| `home` | Flink 安装按 FLINK_HOME → PATH 上的 flink → /opt/flink → /usr/local/flink 定位；找不到为 fail（守护进程无从归属） |
| `processes` | 扫 `/proc/*/cmdline`：主类是 JobManager / TaskManager 入口且 classpath 里 `flink-dist*.jar` 位于**本安装** `lib/`（真实路径相等）的进程；JM + TM 都有为 ok；都没在跑为 fail；只有 JM（作业起不来）或只有 TM（残留进程）为 fail；其它安装的守护进程只计数（`other-clusters` info） |
| `loaded-build` | 每个本安装守护进程 classpath 上的 gluten/velox jar：全有为 gfv、全无为 native、不一致为 mixed（fail：整集群一起重启） |
| `lib-vs-cluster` | 加载的 GFV jar 集合 ≠ lib 里的 GFV jar 集合，或 lib 里有 jar 的 mtime 晚于最早的进程启动时间（`jars_replaced_after_start`）→ fail：集群还在跑旧的；否则 ok |
| `rest` | `GET <rest_url>/overview`（忽略环境代理，3 秒超时）：应答则记版本 / TaskManager 数 / 在跑作业数；在跑而不应答 warn；版本 ≠ 本安装 flink-dist 版本 warn；REST 列出的 TM 少于本安装在跑的 TM warn；不在跑却应答 warn（是别的安装 / 容器 / 远端地址） |
| `jobs` | REST 报有作业在跑 → warn：重启会打断 |
| `other-clusters` | 本机其它安装的 Flink 守护进程数 → info（WebUI / REST 端口可能是它们的） |

REST 地址：`FLINK_REST_URL` → conf `rest.address` / `rest.port` → `http://localhost:8081`，与家族其它 skill 同序。

## 执行步骤

### 步骤 1：扫描

```bash
python3 <skill_dir>/scripts/cluster_check.py [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `cluster     OK       processes: JobManager 123 and 2 TaskManager(s) of this installation; loaded-build: every daemon carries …; lib-vs-cluster: lib/ is unchanged since the cluster started (…); rest: REST http://… answers: Flink 1.19.2, 2 TaskManager(s), 0 job(s) running`；fail / warn 逐项列出并附 `fix:`，info 单列；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/cluster-check.json`（留档规则见「环境约定」）。

退出码：**0** 集群在跑、构建一致、lib 未改（warn 允许）；**1** 结构性问题（不在跑 / 缺 JM 或 TM / mixed / lib 改过）或找不到安装；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——按"起集群 → 补齐 / 清理进程 → 整集群重启消 mixed → 重启加载现有 lib"的顺序给出唯一下一步；每条都注明须用户同意。
2. `READY` 的含义是"集群加载的 == lib 里的"：它不说明那是 native 还是 GFV——读 `loaded_build`。
3. `jobs` 的 warn 必须转述：有作业在跑时不得重启。`rest` 的 warn 多半是端口 / 地址指向了别的安装或还在启动中。
4. 本 skill 不起停集群、不取消作业：把 `next:` 交给切换类 skill（规划中）并**征得用户同意**，做完重扫直到 READY。

## 环境约定

- **只读**：读 `/proc/<pid>/cmdline` 与 `stat`（进程起始时间 = 字段 22 的 tick 数 + `/proc/stat` 的 btime）、列 `lib/*.jar` 的 mtime、读 conf、一次 `GET /overview`；不执行任何 Flink 脚本；不写任何文件（`--out` 指定的除外）
- 守护进程归属只认 classpath 中 `flink-dist*.jar` 所在目录与本安装 `lib/` 的**真实路径**相等（符号链接解析后）；无权读取他人进程时该证据自然缺席，相当于"没在跑"
- `ENV_CHECK_SYSROOT`（给所有绝对系统路径加前缀）与 `ENV_CHECK_PROC_ROOT`（替换 /proc）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`cluster.running`、`cluster.loaded_build`、`cluster.consistent_with_lib`、`cluster.rest_reachable`、`missing`、`next_step`、`error_message` 必须保留（下例：换了 jar 没重启，节选）：

```json
{
  "flink_velox_cluster_check_result": {
    "success": false,
    "checked_at": "2026-11-24T11:00:00+08:00",
    "status": "fail",
    "cluster": {
      "flink_home": "/usr/local/flink", "found_via": "FLINK_HOME", "running": true,
      "jobmanagers": [{"pid": 756550, "gfv_jars": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar", "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar"], "started": "2026-11-23 18:00:12"}],
      "taskmanagers": [{"pid": 756612, "gfv_jars": ["…"], "started": "2026-11-23 18:00:40"}],
      "other_flink_daemons": 0, "loaded_build": "gfv",
      "loaded_gfv_jars": ["…"], "lib_gfv_jars": ["…"], "lib_version": "1.19.2",
      "earliest_start": "2026-11-23 18:00:12", "jars_replaced_after_start": ["gluten-flink-runtime-1.9.0-SNAPSHOT.jar"],
      "consistent_with_lib": false, "rest_url": "http://localhost:8159", "rest_reachable": true,
      "rest_version": "1.19.2", "rest_taskmanagers": 1, "rest_jobs_running": 0
    },
    "items": [
      {"name": "lib-vs-cluster", "status": "fail", "detail": "lib/ changed since the cluster started (replaced after the start: gluten-flink-runtime-1.9.0-SNAPSHOT.jar) — the cluster still runs what it loaded", "fix": "restart the cluster so it loads what is in lib now (a restart needs the user consent)"}
    ],
    "missing": ["lib-vs-cluster: lib/ changed since the cluster started (…)"],
    "next_step": "restart the cluster so it loads what is in lib now (a restart needs the user consent), then rerun",
    "error_message": "1 item(s) missing: lib-vs-cluster"
  }
}
```

- 原始观测 `cluster`：`flink_home` / `found_via`、`running`、`jobmanagers[]` / `taskmanagers[]`（`pid` / `gfv_jars` / `started`）、`other_flink_daemons`、`loaded_build`（native / gfv / mixed / null）、`loaded_gfv_jars`、`lib_gfv_jars`、`lib_version`、`earliest_start`、`jars_replaced_after_start`、`consistent_with_lib`（true / false / null）、`rest_url` / `rest_reachable` / `rest_version` / `rest_taskmanagers` / `rest_jobs_running`；`items` 是逐项判定（`name` / `status` / `detail`，fail 另带 `fix`；同名可多条，如两条 `rest`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `lib-vs-cluster: lib/ changed since …`
- 失败形态：`success: false`，`error_message` 形如 `1 item(s) missing: lib-vs-cluster`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-lib-state-check`：lib 里是哪套构建、GFV 集齐不齐——本 skill 用同一判据看 classpath 上加载的那套
- `flink-velox-jdk-check`：守护进程用哪个 Java、重启会选到哪个（它也读 /proc 做同样的归属）
- `flink-velox-flink-dist-check`：Flink 安装完整性与版本线（REST 报的版本与它对照）
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 起停集群、取消作业：同族切换类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何动作
