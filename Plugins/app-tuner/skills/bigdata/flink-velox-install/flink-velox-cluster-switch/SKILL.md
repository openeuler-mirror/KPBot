---
name: flink-velox-cluster-switch
description: >
  在 native 与 GlutenFlink 构建之间切换 Flink 的 lib 并重启集群：`--to native` 把 lib 里所有 gluten/velox jar 挪到 lib.gfv-backup/（不删、不覆盖），
  `--to gfv` 从备份挪回（lib 已是完整 GlutenFlink 集时只重启）；然后停集群（stop-cluster.sh → 对本安装残留的 JM/TM 发 TERM/KILL → 等 REST 不应答）、起集群
  （start-cluster.sh → 等 REST 应答且 TaskManager ≥ 1，超时给 taskexecutor 日志尾）。REST 报有作业在跑、或作业数无法确认（REST 不应答）时拒绝执行；
  必须 `--yes`。触发词：切 native、切 GlutenFlink、换 jar 后重启集群、重启 Flink、让新构建生效、lib.gfv-backup。
  不负责：编译部署 jar → flink-velox-stack-build；只查集群加载了哪套 → flink-velox-cluster-check / lib-state-check。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；读 /proc 与 Flink conf、REST 轮询；执行 Flink 的 stop-cluster.sh / start-cluster.sh
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# 切换 native / GlutenFlink 并重启集群

lib 里的 jar 决定了集群下次启动加载哪套构建，但**集群只在启动时读 lib**：换了 jar 不重启，集群还在跑旧的；要跑 native 侧做双跑对照，就得把 gluten/velox jar 挪出 lib 再重启。本 skill 把"挪 jar + 重启"做成一步，并且只在确认没有作业在跑时才动：`--to native` 把 lib 里所有 `gluten*` / `velox4j*` jar 挪到 `<lib>.gfv-backup/`（**不删**，`--to gfv` 时挪回），然后停集群、清残留、等端口释放、起集群、等 TaskManager 就绪。它是 `flink-velox-install` 安装部署家族的成员，是 env 检查家族里 cluster-check / lib-state-check 报"lib 改了没重启"或"要重启"时的执行者，也是 stack-build 部署后的收尾。切换表与判据细节在 [switch-table.md](references/switch-table.md)。

## 术语

这套栈的官方叫法是 GlutenFlink；`gfv` / `GFV` 是代码与契约层沿用的缩写，指同一构建，按既有契约保留原名——CLI 取值与 JSON 值 `gfv`（`--to gfv`、结果里的 `to: gfv`、verify 传给 lib-state-check 的 `--expect gfv`）、备份目录名 `<lib>.gfv-backup/`、脚本与用例里的变量名（`GFV_JAR_RE` / `GFV_COMPONENTS`，tier-A 用例的 `GFV_JARS`）。`--to native` 的 `native` 不是缩写：指不带 gluten/velox jar 的普通 Flink 构建。

## 使用场景

### 适用（应使用本 skill）

- `flink-velox-stack-build` 部署了新构建，要让集群加载它
- 要跑 native 侧（双跑对照、原生基线）：把 GlutenFlink jar 挪出 lib 并重启；跑完再切回
- `flink-velox-cluster-check` 报 `lib-vs-cluster` 不一致（lib 改了没重启）或 mixed（各守护进程加载的不一致，要整集群重启）
- conf 改了 `env.java.home` 等需要重启才生效的键

### 不适用（不要用本 skill）

- 只想知道集群现在加载了哪套、lib 与集群是否一致 → `flink-velox-cluster-check`（只读）
- 编译并把新 jar 放进 lib → `flink-velox-stack-build`；装 Flink → `flink-velox-runtime-install`
- 有作业在跑，或者 REST 不应答、作业数无法确认：本 skill 会拒绝——等作业结束、修好 REST 地址 / 端口，或由用户决定停掉它们后再来
- 要重启的集群不在本机：本 skill 只重启本机安装

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 探测 lib / 备份目录 / 集群 / 在跑作业，列出三步状态与**将执行的命令**（挪哪些 jar、停起脚本、等待条件）；有作业在跑、作业数无法确认、REST 不在本机时在 `next:` 里劝阻 | 否 |
| `apply --yes` | 执行：挪 jar → 停集群（含残留清理）→ 起集群；第一步失败即停，报告已做 / 未做（每个挪过的 jar、每条已发的信号都在 `changes` 里） | 是（挪 jar、起停进程） |
| `verify` | 运行 env 检查家族的 cluster-check 与 lib-state-check（`--expect` 与目标一致）；兄弟检查 skill 没装时报 `NOT VERIFIED` | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）——重启会打断集群，agent 必须先把 `plan` 给用户看、得到明确同意。REST 报 `jobs-running > 0` 时无论有没有 `--yes` 都拒绝；本安装有守护进程在跑而 REST 不应答（或应答里没有 `jobs-running`）时同样拒绝——"作业数未知"不等于"没有作业"。`lib/` 对当前用户不可写时 `apply` 要求 root。

## 三个步骤

| 步骤 | 内容 | 判据 | 需 root |
|---|---|---|---|
| `jars` | `--to native`：lib 里文件名匹配 `gluten` / `velox4j` 的 jar 逐个 `mv` 到 `<lib>.gfv-backup/`（运行依赖 guava / arrow 等留着，无害）；`--to gfv`：备份目录里的 jar 逐个 `mv` 回 lib，目录空了就 `rmdir` | 已是目标状态（native 下 lib 无 GlutenFlink jar；gfv 下 lib 已有**完整 GlutenFlink 集**——planner / loader / runtime / velox4j 各有 jar，加 stack-build 一并部署的 14 个运行依赖都齐）→ SKIP；备份目录与 lib 里有同名 jar → BLOCKED（不覆盖，提示手工比较新旧）；`--to gfv` 而挪回后集仍不齐（组件或运行依赖缺，运行依赖缺失正是 stack-build 明确"不要重启"的状态）→ BLOCKED（先跑 stack-build 补齐，或改切 native），点名缺什么 | lib 不可写时 |
| `stop` | `bin/stop-cluster.sh` → 再扫 /proc 找**本安装**（classpath 里 `flink-dist*.jar` 位于本 lib）残留的 JobManager / TaskManager：先 TERM、2 秒后仍在对 KILL（stop-cluster.sh 只认 pid 文件，pid 丢了就会留下还占着端口的旧 JM）→ 轮询 REST `/overview` 直到不应答 | 集群本来没在跑（无守护进程且 REST 不应答）→ SKIP | 否 |
| `start` | `bin/start-cluster.sh` → 轮询 REST `/overview` 直到 `taskmanagers >= 1`（默认 180 秒，`--timeout` 可改）；超时则报最新的 `flink-*-taskexecutor-*.log` 末 20 行 | — | 否 |

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/cluster_switch.py plan --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE [--overwrite]]
```

`--to` 必填。`--flink-home` 给了就只认它：不是一套 Flink 安装（缺 `bin/flink`、`bin/start-cluster.sh`、`bin/stop-cluster.sh`）直接拒绝，不会回落到环境变量或 PATH 上的另一套。人读报告三步各一行（挪哪些 jar、停起脚本与等待条件），末尾 `PLAN: N step(s) to apply` 与 `next:`。有作业在跑时 `next:` 明确写"会打断 N 个作业"；作业数无法确认、REST 不在本机时同样劝阻并说明 apply 会拒绝。要机器消费加 `--json`，要留档加 `--out <项目内路径>/switch-plan.json`。`next:` 给出的 apply / verify 命令是完整命令——`python3`、本脚本的绝对路径、本次用到的 `--flink-home` / `--timeout`——在任意目录照抄即可。

### 步骤 2：征得同意后执行

```bash
python3 <skill_dir>/scripts/cluster_switch.py apply --yes --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE]
```

退出码：**0** 已切换且集群带着 ≥ 1 个 TaskManager 回来；**1** 某步失败即停——`changes:` 逐条列出已挪的 jar（源路径 → 目标路径）、已发的信号、脚本的退出码；命令超时或无法执行也算该步失败，记录不清空；集群没起来时附日志尾，**此时集群可能处于停止状态**，按 `next:` 修复后重跑（jar 已挪的不会重复挪）；**2** 拒绝——缺 `--yes`、缺 `--to`、有作业在跑、作业数无法确认、REST 不在本机、`--flink-home` 不是 Flink 安装、`jars` 步 BLOCKED（同名冲突 / 组件或运行依赖不齐）、需 root 而非 root（提示里给出带 `sudo` 的同一条命令）、`--out` 不合法（此时什么都没做）。

### 步骤 3：复查

```bash
python3 <skill_dir>/scripts/cluster_switch.py verify --to native|gfv
```

调 cluster-check（集群在跑、加载一致）与 lib-state-check（`--expect` 与目标一致）。退出码 **0** 两项都通过；**1** 有检查失败，或兄弟检查 skill 没装（一项都没查，报 `NOT VERIFIED` 并列出缺哪个——不是通过）。兄弟检查按 skill 名定位，不依赖目录层级：`ENV_PREPARE_ENV_FAMILY_DIR` 指定了目录就只认它（没装就是 `missing`）；否则依次找源码树的 `skills/bigdata/flink-velox-env/<检查 skill>/`、安装后平铺的 `<config>/skills/<检查 skill>/`。

## 环境约定

- **plan 不改任何东西**；apply 的写操作只有：lib ↔ 备份目录之间的 jar 挪动、起停进程、备份目录的创建 / 删除；**不删除、不覆盖任何 jar**（备份目录就是回退路径：手工 `mv` 回去即可）
- 残留清理只针对 classpath 里 `flink-dist*.jar` 位于**本安装** `lib/` 真实路径下的进程——其它安装（另一套 Flink、容器共享宿主网络的那套）不会被碰；无权发信号时记录在 `changes` 里继续
- REST 地址取自 Flink 实际读的那份配置：两份并存时 Flink 1.19 只读 `flink-conf.yaml`、不读 `config.yaml`，本 skill 也只读那一份（`conf_file` 字段说明读的是哪份）；REST 不在本机（`rest.address` 不是回环、未指定或本机地址）时拒绝——不重启别人的集群
- REST 轮询忽略环境代理（localhost 不该被代理改写）；`jobs-running > 0` 与"作业数未知"都是硬拒绝，不提供"强制"开关
- `ENV_PREPARE_SYSROOT`、`ENV_PREPARE_PROC_ROOT`、`ENV_PREPARE_EUID`、`ENV_PREPARE_PREFIX_WRITABLE=0`、`ENV_PREPARE_ENV_FAMILY_DIR` 仅供 tier-A 用例构造假机器，正常使用不要设置；`ENV_PREPARE_PROC_ROOT` 指向假 /proc 时脚本不对任何进程发信号，只记录意图
- `--out` 留档受限，且**在任何动作开始前校验**：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；已存在的目标不是普通文件（管道、目录等）时即使带 `--overwrite` 也拒绝。被拒绝时退出码 2、stderr 说明原因，apply 不会开始

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`command`、`to`、`flink_home`、`conf_file`、`rest_url`、`steps[].status`、`changes`、`root_steps`、`next_step`、`error_message` 必须保留（下例 plan，节选）：

```json
{
  "flink_velox_cluster_switch_result": {
    "success": true,
    "checked_at": "2026-11-24T17:00:00+08:00",
    "command": "plan",
    "to": "native",
    "flink_home": "/opt/flink", "conf_file": "/opt/flink/conf/config.yaml", "rest_url": "http://localhost:8081",
    "steps": [
      {"name": "jars", "status": "todo", "needs_root": true, "in_lib": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar", "…"], "in_backup": [],
       "backup": "/opt/flink/lib.gfv-backup", "moving": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar", "…"], "direction": "out",
       "detail": "move 4 gluten/velox jar(s) out of lib: …", "plan": ["mkdir -p /opt/flink/lib.gfv-backup", "mv /opt/flink/lib/… /opt/flink/lib.gfv-backup/…"], "actions": []},
      {"name": "stop", "status": "todo", "needs_root": false, "jobs_running": 0, "rest_was_up": true,
       "detail": "stop-cluster.sh, TERM/KILL leftover daemons of this installation, wait until REST http://localhost:8081 stops answering",
       "plan": ["/opt/flink/bin/stop-cluster.sh", "# kill leftover JobManager/TaskManager of this installation (TERM, then KILL after 2 s)", "# poll GET http://localhost:8081/overview until it stops answering (timeout 180s)"], "actions": []},
      {"name": "start", "status": "todo", "needs_root": false, "detail": "start-cluster.sh, wait until REST http://localhost:8081 answers with at least 1 TaskManager (timeout 180s)",
       "plan": ["/opt/flink/bin/start-cluster.sh", "# poll GET http://localhost:8081/overview until taskmanagers >= 1 (timeout 180s)"], "actions": []}
    ],
    "changes": [],
    "root_steps": ["jars"],
    "next_step": "review the plan above, then run python3 <skill_dir>/scripts/cluster_switch.py apply --yes --to native (restarting the cluster needs the user's consent)",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`；`jars` 有 `in_lib` / `in_backup` / `backup` / `moving`（skipped / blocked 时没有）/ `direction`（out / in），blocked 时另有 `fix`，完备性 BLOCKED（--to gfv 集不齐）另有 `missing` / `missing_deps` 点名缺的组件与运行依赖；`stop` 有 `jobs_running`（REST 应答里的整数；REST 不应答或没有该键时为 `null`）与 `rest_was_up`
- `changes`（apply）：`[{step, action, detail}]`，`action` 取 `moved out` / `moved in`（**每个 jar 一条**，detail 是 `源路径 -> 目标路径`）、`stop-cluster.sh`、`sent TERM` / `sent KILL`、`TERM failed` / `KILL failed`、`start-cluster.sh`、`failed`、`skipped`；某步失败时它之前的条目都保留
- `next_step`：plan 给"看计划后 apply"或"先处理在跑作业 / REST 再来"；给出的命令都是完整的（`python3` + 本脚本绝对路径）并带着本次的 `--flink-home` / `--timeout`；apply 失败给修哪一步（集群可能处于停止状态），成功给 verify；`error_message`：`refused: ...` / `<step> failed` / `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（cluster / lib-state 各一条）：`status` 取 `ok` / `fail` / `missing`（兄弟检查 skill 没装，带 `skill` 名）；有 `missing` 或 `fail` 时 `success=false`、退出 1，`error_message` 写"N check(s) not run (sibling skills not installed): …"或"N check(s) failing"

## 边界与相邻 skill

- env 检查家族的 cluster-check / lib-state-check——本 skill 的 `verify` 调它们；它们报"要重启"时工单就是本 skill
- `flink-velox-stack-build`：部署 jar（不重启）→ 本 skill 让新构建生效；`--to gfv` 凑不齐完整集（4 组件 + 14 运行依赖，与 stack-build 的判定一致）时 BLOCKED 并指向它——stack-build 在 runtime-deps 失败时明确"do NOT restart"，本 skill 不把那次重启补上
- `flink-velox-e2e-verify`：双跑对照要跑 native 侧时，用本 skill `--to native` 切换（须用户同意），跑完再 `--to gfv` 切回
- 本 skill 不编译、不装包、不提交或取消作业：有作业在跑或作业数未知就拒绝
