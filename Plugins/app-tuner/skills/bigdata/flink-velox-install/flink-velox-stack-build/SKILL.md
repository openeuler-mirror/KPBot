---
name: flink-velox-stack-build
description: >
  编译并部署 GlutenFlink 构建（Flink + GlutenFlink，velox 向量化栈）：在 velox4j 源码仓 mvn 构建（对着工作区 velox 源码编原生 .so），
  在 gluten-flink 源码仓构建 planner / loader / runtime 三模块，把 velox4j + 三个 gluten jar + 14 个运行依赖（guava / arrow /
  jackson / flatbuffers / commons-io，构建后从 ~/.m2 取）部署进 `$FLINK_HOME/lib`；只把本次会被替换的旧 jar 备份到 lib.bak/<时间戳>/ 再移出 lib；
  Flink 实际读的那份 conf 没有 env.java.home 时补成构建用的 JDK 17。三段式：`plan`（默认）只打印、`apply --yes` 才执行（失败即停、变更清单；
  lib 不可写时非 root 拒绝）；`verify` 调 lib-state-check --expect gfv。jar 名一律 glob 匹配，不硬编码版本。
  触发词：编译 velox4j、编译 gluten-flink、部署 GlutenFlink 构建、把 jar 放进 Flink lib、编译部署、stack build。
  不负责：准备构建环境 → flink-velox-build-env-prepare；装 Flink → flink-velox-runtime-install；
  重启集群让新构建生效 → flink-velox-cluster-switch；查 lib 状态 → flink-velox-lib-state-check。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需 mvn、JDK 17、三个源码仓、Flink 安装；经 sudo 运行时 Maven 构建以原用户执行（需 runuser 在 PATH）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# 编译并部署 GlutenFlink 构建

GlutenFlink 构建是四个 jar：velox4j（含对着 velox 源码编出来的原生 `.so`）与 gluten-flink 的 planner / loader / runtime，外加 14 个它们运行时要用的第三方 jar（guava、arrow 系列、jackson 系列、flatbuffers、commons-io）。构建要用 JDK 17（gluten 的 jar 是 Java 17 字节码），velox4j 的原生部分要对**工作区的** velox 源码编（本地改动要进 `.so`，且不去下载一份固定的 velox），gluten-flink 依赖本地 Maven 仓库里有 nexmark-flink（从 Flink lib 里的 nexmark jar 装）。部署是把这 18 个 jar 放进 `$FLINK_HOME/lib`——**先备份会被替换的再放**，并且 Flink 实际读的那份 conf 里没有 `env.java.home` 时补上构建 JDK，否则集群用旧 JDK 起不来。每次构建都会把**源码来自哪里**（checkout 的 origin、commit、是否有本地改动）写进计划与变更清单。本 skill 把这套流程做成**可先看计划再执行**的形式，是 `flink-velox-install` 安装部署家族的成员，是 env 检查家族里 lib-state-check 报 native / partial 时的修复动作。构建表与判据细节在 [build-table.md](references/build-table.md)。

## 术语

这套栈的官方叫法是 GlutenFlink；`gfv` / `GFV` 是代码与契约层沿用的缩写，指同一构建，按既有契约保留原名——CLI 取值 `gfv`（`verify` 调 lib-state-check 传的 `--expect gfv`，verify 报告里兄弟检查对 lib 的判定 `build=gfv` 同指这套构建）、环境变量 `GFV_REPOS_DIR`（`--repos-dir` 没给时的取值来源）、脚本里的变量名 `GFV_JAR_RE`（备份清单按它圈定 GlutenFlink jar）、项目名 GFVBot（构建表判据解析自其 compile.sh）。

## 使用场景

### 适用（应使用本 skill）

- 构建环境（三仓、mvn、JDK 17）与 Flink 运行时都装好后，第一次把 GlutenFlink 构建部署进 Flink
- velox / velox4j / gluten 源码有改动，重新编译部署（全部，或用 `--only` 只重建一个组件）
- `flink-velox-lib-state-check` 报 lib 是 native 或 partial，要部署完整 GlutenFlink 集

### 不适用（不要用本 skill）

- 缺源码仓 / mvn / JDK 17 → `flink-velox-build-env-prepare`（见 !322）；缺 Flink → `flink-velox-runtime-install`（见 !323）——本 skill 会 BLOCKED 并指向它们
- 只想知道 lib 里是哪套构建 → `flink-velox-lib-state-check`（只读，见 !321）
- 部署后让集群加载新构建 → `flink-velox-cluster-switch`（见 !325）：**部署不等于生效**，集群要重启才会加载新 jar；运行依赖缺失、某步失败时不要重启
- 只换 lib 里的 jar 不重新编译 → `flink-velox-cluster-switch`

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 探测前置（**按 `--only` 只查本次构建读的源码仓**，另查 JDK 17、mvn、Flink），列出五个步骤的状态与**将执行的精确命令**（备份清单、mvn 命令、cp、conf），以及每个构建的源码 origin / commit / 是否有本地改动 | 否 |
| `apply --yes` | 执行：备份 → 构建 velox4j → 构建 gluten-flink → 部署运行依赖 → 补 conf；第一步失败即停，`changes` 逐条列出已做的事（每个挪走的 jar、每次构建的源码） | 是（编译、写 lib 与 conf、写 ~/.m2） |
| `verify` | 运行 env 检查家族的 lib-state-check（`--expect gfv`）；兄弟检查 skill 没装时报 `NOT VERIFIED`；显式 `--flink-home` 不是一套 Flink 安装时直接拒绝（退出 2），不回退复查机器上另一套 Flink | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）。agent 先把 `plan` 给用户看、得到明确同意后再加 `--yes`。`lib/` 对当前用户不可写时 `apply` 要求 root；经 `sudo` 运行时**只有备份与往 lib 部署以 root 执行**，Maven 构建交回原用户（`SUDO_USER`，经 `runuser`），`~/.m2` 也是原用户的；root 为 install-file 建的临时目录交给该用户，对 checkout 的 git 查询也以该用户执行。**装进 lib 的是原用户 checkout 构建出的 jar**——部署自己的构建正是本 skill 的用途，所以能 sudo 运行它的用户就能决定集群加载什么代码，授权时按此对待（`flink-velox-runtime-install` 不同：那里装的是固定上游 commit 的产物）。

## 五个步骤

| 步骤 | 内容 | 幂等 / 判据 | 需 root |
|---|---|---|---|
| `backup` | **只搬本次会被替换的 jar**：全量构建 = lib 里的 `gluten*` / `velox4j*` 与 14 个运行依赖；`--only velox4j` = 只 `velox4j*`；`--only gluten` = 只 `gluten*`。它们拷到 `<lib>.bak/<时间戳>/` 后**从 lib 移出**（lib 不会同时留两个版本；同一秒内重跑自动加 `-1`、`-2` 后缀）；不重建的组件与运行依赖原样留在 lib，计划里注明 | lib 里没有这些 jar 则 SKIP | lib 不可写时 |
| `velox4j` | 在 velox4j 源码目录 `JAVA_HOME=<JDK 17> VELOX4J_VELOX_SOURCE_DIR=<velox 源码> VELOX4J_BUILD_JOBS=min(8, --jobs 或核数) mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true`，取 `target/velox4j-*.jar`（排除 `-sources` / `-javadoc`）拷进 lib | 构建步骤**总是重做**（源码可能变了） | lib 不可写时 |
| `gluten` | lib 里有 nexmark jar 时先把它装进本地 Maven 仓库（从 jar 里取出 `META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml`，`mvn install:install-file`）；再在 `gluten/gluten-flink` 目录 `mvn clean install -DskipTests -pl planner,loader,runtime -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true`，取三模块 `target/gluten-flink-<模块>-*.jar` 拷进 lib | 总是重做；没有 nexmark jar 时跳过 install-file 并提示 | lib 不可写时 |
| `runtime-deps` | 14 个 `group:artifact:version` **在两条 mvn 之后**再到 `~/.m2/repository` 找（首次构建时正是这两条 mvn 把它们装进去的），找到的拷进 lib；**仍缺的判该步失败**——lib 是不完整的 GlutenFlink 集，`next:` 明确写不要重启集群 | — | lib 不可写时 |
| `pin-jdk` | **Flink 实际读的那份配置**（`flink-conf.yaml` 存在就是它，否则 `config.yaml`）没有 `env.java.home` → 追加 `env.java.home: <构建 JDK>`；已有且等于构建 JDK → SKIP；已有但不同 → **不碰**，SKIP 并 WARNING（用户配置的值归用户） | — | conf 目录不可写时 |

`--only velox4j|gluten` 只构建一个组件（跳过运行依赖与 conf，另一组件与运行依赖不动），**前置也按 `--only` 只查本次构建真正读的源码仓**：`--only velox4j` 需 velox + velox4j（native `.so` 对着 velox 源码编）；`--only gluten` 需 gluten——gluten 构建不读 velox / velox4j 源码仓，但 gluten-flink 的 planner / runtime 以 provided 依赖从 `~/.m2` 解析 velox4j（SNAPSHOT，远端仓库不提供），所以 `--only gluten` 另有一个前置：**velox4j 已构建过**（版本读 gluten-flink/pom.xml 的 `velox4j.version` 属性），缺了 BLOCKED 并指向先跑 `--only velox4j` 或全量构建；全量构建需三仓、不查这条（velox4j 步骤先跑，本趟就把它装进 `~/.m2`）。JDK 17、mvn、Flink、runuser 等其余前置不受 `--only` 过滤影响。`--jobs N` 设 velox4j 原生构建并行度（默认 `min(8, 核数)`，更多 C++ 单元会吃光内存）。`--flink-home DIR` 指定部署目标：给了就只认它，不是一套 Flink 安装直接 BLOCKED（verify 同样拒绝：退出 2，不会回退复查机器上另一套 Flink）。JDK 17 的每个候选都核对 `release` 里的真实主版本——目录名叫 `java-17-*` 但内容是别的 JDK 不会被选用。

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/stack_build.py plan [--repos-dir DIR] [--flink-home DIR] [--only velox4j|gluten] [--jobs N] [--json] [--out FILE [--overwrite]]
```

前置缺失时整份计划是一条 `BLOCKED`（指出缺什么、该跑哪个 skill）。否则五个步骤各一行：状态、做什么、将执行的命令、构建的源码出处，末尾 `PLAN: N step(s) to apply` 与 `next:`（提醒部署后要重启集群；`next:` 给出的 apply 命令是完整命令——`python3`、本脚本的绝对路径、本次显式传入的选项——在任意目录照抄即可）。要机器消费加 `--json`，要留档加 `--out <项目内路径>/build-plan.json`。

### 步骤 2：征得同意后执行

```bash
python3 <skill_dir>/scripts/stack_build.py apply --yes [--repos-dir DIR] [--flink-home DIR] [--only …] [--jobs N] [--json] [--out FILE]
```

退出码：**0** 全部完成；**1** 某步失败即停——`changes:` 逐条列出已做的每件事（备份了哪些 jar、哪个组件构建到哪），`next:` 说明修哪一步；命令无法执行或超时也算该步失败，记录不清空；被替换的上一套在 `lib.bak/<时间戳>/`，可手工拷回；**2** 拒绝——缺 `--yes`、需 root 而非 root（提示里给出带 `sudo` 的同一条命令）、前置 BLOCKED、`--out` 不合法（此时什么都没做）。

### 步骤 3：重启集群后复查

部署只改 lib，集群还在跑旧的：交给 `flink-velox-cluster-switch`（须用户同意），然后 `python3 <skill_dir>/scripts/stack_build.py verify` 调 lib-state-check（`--expect gfv`）确认 lib 持有完整 GlutenFlink 集。退出码 **0** 通过；**1** 检查失败，或兄弟检查 skill 没装（报 `NOT VERIFIED` 并列出缺哪个——不是通过）；**2** 拒绝——显式 `--flink-home` 不是一套 Flink 安装（报 `REFUSED`，绝不把机器上另一套 Flink 的完整当成它的验证通过）。兄弟检查按 skill 名定位，不依赖目录层级：`ENV_PREPARE_ENV_FAMILY_DIR` 指定了目录就只认它（没装就是 `missing`）；否则依次找源码树的 `skills/bigdata/flink-velox-env/<检查 skill>/`、安装后平铺的 `<config>/skills/<检查 skill>/`。

## 环境约定

- **plan 不改任何东西**；apply 的写操作只有：备份目录、lib 里的 jar、conf 的 `env.java.home`、`~/.m2`（install-file）；构建产物在各源码仓的 `target/`
- jar 名一律 glob（`velox4j-*.jar`、`gluten-flink-<模块>-*.jar`），版本随源码走；**不删除 lib 里不认识的 jar**，只处理本次重建的 GlutenFlink 组件与（全量构建时的）14 个运行依赖
- 源码可以是任何 checkout（含自己的 fork 与本地改动）——本 skill 不拦，但把 origin / commit / 是否有本地改动记在计划与 `changes` 里，让"以什么源码构建"可审
- 运行依赖的 `group:artifact:version` 清单来自 velox4j / gluten-flink 实际解析的版本，升级栈时要同步（[build-table.md](references/build-table.md)）
- `ENV_PREPARE_SYSROOT`、`ENV_PREPARE_HOME`（替换 ~，影响 ~/.m2）、`ENV_PREPARE_EUID`、`ENV_PREPARE_PREFIX_WRITABLE=0`、`ENV_PREPARE_ENV_FAMILY_DIR` 仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限，且**在任何动作开始前校验**：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；已存在的目标不是普通文件（管道、目录等）时即使带 `--overwrite` 也拒绝。被拒绝时退出码 2、stderr 说明原因，apply 不会开始

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`command`、`repos`、`jdk`、`flink_home`、`steps[].status`、`changes`、`root_steps`、`next_step`、`error_message` 必须保留（下例 plan，节选）：

```json
{
  "flink_velox_stack_build_result": {
    "success": true,
    "checked_at": "2026-11-24T16:00:00+08:00",
    "command": "plan",
    "repos": {"velox": "/work/repos/velox", "velox4j": "/work/repos/velox4j", "gluten": "/work/repos/gluten"},
    "jdk": "/usr/lib/jvm/java-17-openjdk", "flink_home": "/opt/flink", "lib": "/opt/flink/lib",
    "steps": [
      {"name": "backup", "status": "todo", "needs_root": true, "backup_dir": "/opt/flink/lib.bak/20261124-160000",
       "backup_list": ["gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "velox4j-0.0.9-SNAPSHOT.jar"],
       "detail": "2 jar(s) in lib would be overwritten by this build: gluten-flink-runtime-1.8.0-SNAPSHOT.jar, velox4j-0.0.9-SNAPSHOT.jar",
       "plan": ["mkdir -p /opt/flink/lib.bak/20261124-160000", "cp /opt/flink/lib/gluten-flink-runtime-1.8.0-SNAPSHOT.jar velox4j-0.0.9-SNAPSHOT.jar /opt/flink/lib.bak/20261124-160000/   # then removed from lib"], "actions": []},
      {"name": "velox4j", "status": "todo", "needs_root": true,
       "detail": "mvn clean install -DskipTests in /work/repos/velox4j (against /work/repos/velox, JDK /usr/lib/jvm/java-17-openjdk); velox4j-*.jar -> lib; source https://github.com/me/velox4j.git @ 6b3646c3baec with local modifications / velox https://github.com/me/velox.git @ 0123456789ab",
       "source": {"origin": "https://github.com/me/velox4j.git", "head": "6b3646c3baec…", "dirty": true},
       "plan": ["# JAVA_HOME=… VELOX4J_VELOX_SOURCE_DIR=/work/repos/velox VELOX4J_BUILD_JOBS=8", "mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in /work/repos/velox4j)", "cp /work/repos/velox4j/target/velox4j-*.jar /opt/flink/lib/"], "actions": []},
      {"name": "gluten", "status": "todo", "needs_root": true, "detail": "… ; nexmark nexmark-flink-0.3-SNAPSHOT.jar installed into ~/.m2 first; source …", "plan": ["…"], "actions": []},
      {"name": "runtime-deps", "status": "todo", "needs_root": true,
       "detail": "14 of 14 runtime dependencies in ~/.m2 now, re-checked after the builds -> lib",
       "plan": ["cp ~/.m2/repository/com/google/guava/guava/33.4.0-jre/guava-33.4.0-jre.jar /opt/flink/lib/", "…"], "found": ["…"], "missing": [], "actions": []},
      {"name": "pin-jdk", "status": "todo", "needs_root": true, "detail": "/opt/flink/conf/config.yaml has no env.java.home: append env.java.home: /usr/lib/jvm/java-17-openjdk (gluten jars carry Java 17 bytecode)", "plan": ["echo \"env.java.home: /usr/lib/jvm/java-17-openjdk\" >> /opt/flink/conf/config.yaml"], "conf": "/opt/flink/conf/config.yaml", "action": "append", "actions": []}
    ],
    "changes": [],
    "root_steps": ["backup", "velox4j", "gluten", "runtime-deps", "pin-jdk"],
    "next_step": "as root: review the plan above, then run python3 <skill_dir>/scripts/stack_build.py apply --yes (the cluster must be restarted afterwards: flink-velox-cluster-switch)",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`（前置缺失时只有一条 `prereqs`）；`backup` 有 `backup_dir` / `backup_list`；`velox4j` / `gluten` 有 `source`（`origin` / `head` / `dirty`，不是 git checkout 时三项为 `null`），`velox4j` 另有 `velox_source`；`runtime-deps` 有 `found` / `missing`（plan 里是构建前的快照，apply 里是构建后的结果）；`pin-jdk` 有 `conf` / `action`（append / warn / none）
- `changes`（apply）：`[{step, action, detail}]`，`action` 取 `backed up`（**每个 jar 一条**）、`built+deployed`（detail 带源码出处）、`installed nexmark-flink into ~/.m2`、`deployed`、`configured`、`skipped`、`failed`；某步失败时它之前的条目都保留
- `next_step`：plan 给"看计划后 apply"（需 root 时注明 as root；给出的命令都是完整的——`python3` + 本脚本绝对路径——并带着本次的显式选项），apply 失败给修哪一步并提示被替换的上一套在 lib.bak（运行依赖缺失时写明**不要重启集群**），成功给"重启集群后 verify"；`error_message`：`refused: ...` / `<step> failed` / `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（lib-state 一条）：`status` 取 `ok` / `fail` / `missing`（兄弟检查 skill 没装，带 `skill` 名）；有 `missing` 或 `fail` 时 `success=false`、退出 1，`error_message` 写"N check(s) not run (sibling skills not installed): …"或"N check(s) failing"

## 边界与相邻 skill

- env 检查家族的 lib-state-check（见 !321）——本 skill 的 `verify` 调它（`--expect gfv`）；它的 fail 就是本 skill 的工单
- `flink-velox-build-env-prepare`（见 !322）：三仓、mvn、JDK 17、源码级库——本 skill 的前置；`flink-velox-runtime-install`（见 !323）：Flink 与 nexmark jar——gluten 构建依赖 nexmark 进 ~/.m2
- `flink-velox-cluster-switch`（见 !325）：部署后重启集群让新构建生效，须用户同意
- 本 skill 不起停集群、不提交作业、不改 lib 里 GlutenFlink 集与运行依赖以外的任何 jar
