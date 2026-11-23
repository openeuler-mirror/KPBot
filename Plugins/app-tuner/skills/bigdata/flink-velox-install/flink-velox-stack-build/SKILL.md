---
name: flink-velox-stack-build
description: >
  编译并部署 GFV 构建（Flink + GlutenFlink，velox 向量化栈）：在 velox4j 源码仓 mvn 构建（对着工作区 velox 源码编原生 .so），
  在 gluten-flink 源码仓构建 planner / loader / runtime 三模块，把 velox4j + 三个 gluten jar + 14 个运行依赖（guava / arrow /
  jackson / flatbuffers / commons-io，从 ~/.m2 取）部署进 `$FLINK_HOME/lib`；旧 GFV jar 先备份到 lib.bak/<时间戳>/ 再移出 lib；
  conf 没有 env.java.home 时补成构建用的 JDK 17。三段式：`plan`（默认）只打印、`apply --yes` 才执行（失败即停、变更清单；
  lib 不可写时非 root 拒绝）；`verify` 调 lib-state-check --expect gfv。jar 名一律 glob 匹配，不硬编码版本。
  触发词：编译 velox4j、编译 gluten-flink、部署 GFV 构建、把 jar 放进 Flink lib、编译部署、stack build。
  不负责：准备构建环境 → flink-velox-build-env-prepare；装 Flink → flink-velox-runtime-install；
  重启集群让新构建生效 → flink-velox-cluster-switch（规划中）；查 lib 状态 → flink-velox-lib-state-check。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需 mvn、JDK 17、三个源码仓、Flink 安装
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# 编译并部署 GFV 构建

GFV 构建是四个 jar：velox4j（含对着 velox 源码编出来的原生 `.so`）与 gluten-flink 的 planner / loader / runtime，外加 14 个它们运行时要用的第三方 jar（guava、arrow 系列、jackson 系列、flatbuffers、commons-io）。构建要用 JDK 17（gluten 的 jar 是 Java 17 字节码），velox4j 的原生部分要对**工作区的** velox 源码编（本地改动要进 `.so`，且不去下载一份固定的 velox），gluten-flink 依赖本地 Maven 仓库里有 nexmark-flink（从 Flink lib 里的 nexmark jar 装）。部署是把这 18 个 jar 放进 `$FLINK_HOME/lib`——**先备份旧的再放**，并且 conf 里没有 `env.java.home` 时补上构建 JDK，否则集群用旧 JDK 起不来。本 skill 把这套流程做成**可先看计划再执行**的形式，是 `flink-velox-install` 安装部署家族的第三个 skill，是 env 检查家族里 lib-state-check 报 native / partial 时的修复动作。构建表与判据细节在 [build-table.md](references/build-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 构建环境（三仓、mvn、JDK 17）与 Flink 运行时都装好后，第一次把 GFV 构建部署进 Flink
- velox / velox4j / gluten 源码有改动，重新编译部署
- `flink-velox-lib-state-check` 报 lib 是 native 或 partial，要部署完整 GFV 集

### 不适用（不要用本 skill）

- 缺源码仓 / mvn / JDK 17 → `flink-velox-build-env-prepare`；缺 Flink → `flink-velox-runtime-install`（本 skill 会 BLOCKED 并指向它们）
- 只想知道 lib 里是哪套构建 → `flink-velox-lib-state-check`（只读）
- 部署后让集群加载新构建 → `flink-velox-cluster-switch`（规划中）：**部署不等于生效**，集群要重启才会加载新 jar
- 只换 lib 里的 jar 不重新编译 → 直接用切换类 skill（规划中）

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 探测前置（源码仓、JDK 17、mvn、Flink），列出五个步骤的状态与**将执行的精确命令**（备份清单、mvn 命令、cp、conf） | 否 |
| `apply --yes` | 执行：备份 → 构建 velox4j → 构建 gluten-flink → 部署运行依赖 → 补 conf；第一步失败即停，报告已做 / 未做 | 是（编译、写 lib 与 conf、写 ~/.m2） |
| `verify` | 运行 `flink-velox-env` 家族的 lib-state-check（`--expect gfv`） | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）。agent 先把 `plan` 给用户看、得到明确同意后再加 `--yes`。`lib/` 对当前用户不可写时 `apply` 要求 root。

## 五个步骤

| 步骤 | 内容 | 幂等 / 判据 | 需 root |
|---|---|---|---|
| `backup` | lib 里已有的 `gluten*` / `velox4j*` jar 与 14 个运行依赖 → 拷到 `<lib>.bak/<时间戳>/` 后**从 lib 移出**（lib 不会同时留两个版本；同一秒内重跑自动加 `-1`、`-2` 后缀） | lib 里没有这些 jar 则 SKIP | lib 不可写时 |
| `velox4j` | 在 velox4j 源码目录 `JAVA_HOME=<JDK 17> VELOX4J_VELOX_SOURCE_DIR=<velox 源码> VELOX4J_BUILD_JOBS=min(8, --jobs 或核数) mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true`，取 `target/velox4j-*.jar`（排除 `-sources` / `-javadoc`）拷进 lib | 构建步骤**总是重做**（源码可能变了） | lib 不可写时 |
| `gluten` | lib 里有 nexmark jar 时先把它装进本地 Maven 仓库（从 jar 里取出 `META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml`，`mvn install:install-file`）；再在 `gluten/gluten-flink` 目录 `mvn clean install -DskipTests … -pl planner,loader,runtime`，取三模块 `target/gluten-flink-<模块>-*.jar` 拷进 lib | 总是重做；没有 nexmark jar 时跳过 install-file 并提示 | lib 不可写时 |
| `runtime-deps` | 14 个 `group:artifact:version` 在 `~/.m2/repository` 里找，找到的拷进 lib；**缺的只警告不判失败**（构建刚把它们装进 ~/.m2，正常不会缺） | 一个都没有则 SKIP | lib 不可写时 |
| `pin-jdk` | `conf/flink-conf.yaml` 没有 `env.java.home` → 追加 `env.java.home: <构建 JDK>`；已有且等于构建 JDK → SKIP；已有但不同 → **不碰**，SKIP 并 WARNING（用户配置的值归用户） | — | conf 目录不可写时 |

`--only velox4j|gluten` 只构建一个组件（跳过运行依赖与 conf）。`--jobs N` 设 velox4j 原生构建并行度（默认 `min(8, 核数)`，更多 C++ 单元会吃光内存）。

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/stack_build.py plan [--repos-dir DIR] [--only velox4j|gluten] [--jobs N] [--json] [--out FILE [--overwrite]]
```

前置缺失时整份计划是一条 `BLOCKED`（指出缺什么、该跑哪个 skill）。否则五个步骤各一行：状态、做什么、将执行的命令，末尾 `PLAN: N step(s) to apply` 与 `next:`（提醒部署后要重启集群）。要机器消费加 `--json`，要留档加 `--out <项目内路径>/build-plan.json`。

### 步骤 2：征得同意后执行

```bash
python3 <skill_dir>/scripts/stack_build.py apply --yes [--repos-dir DIR] [--only …] [--jobs N] [--json] [--out FILE]
```

退出码：**0** 全部完成；**1** 某步失败即停——`changes:` 列出已做的每件事（备份目录在哪、哪个组件构建到哪），`next:` 说明修哪一步；lib 里的上一套在 `lib.bak/<时间戳>/`，可手工拷回；**2** 拒绝——缺 `--yes`、需 root 而非 root、前置 BLOCKED。

### 步骤 3：重启集群后复查

部署只改 lib，集群还在跑旧的：交给 `flink-velox-cluster-switch`（规划中，须用户同意），然后 `python3 <skill_dir>/scripts/stack_build.py verify` 调 lib-state-check（`--expect gfv`）确认 lib 持有完整 GFV 集。

## 环境约定

- **plan 不改任何东西**；apply 的写操作只有：备份目录、lib 里的 jar、conf 的 `env.java.home`、`~/.m2`（install-file）；构建产物在各源码仓的 `target/`
- jar 名一律 glob（`velox4j-*.jar`、`gluten-flink-<模块>-*.jar`），版本随源码走；**不删除 lib 里不认识的 jar**，只处理 GFV 集与 14 个运行依赖
- 运行依赖的 `group:artifact:version` 清单来自 velox4j / gluten-flink 实际解析的版本，升级栈时要同步
- `ENV_PREPARE_SYSROOT`、`ENV_PREPARE_HOME`（替换 ~，影响 ~/.m2）、`ENV_PREPARE_EUID`、`ENV_PREPARE_PREFIX_WRITABLE=0`、`ENV_PREPARE_ENV_FAMILY_DIR` 仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，结论照常打印

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
      {"name": "backup", "status": "skipped", "needs_root": true, "detail": "lib holds no GFV jar or runtime dependency yet", "plan": ["# nothing to back up"], "actions": []},
      {"name": "velox4j", "status": "todo", "needs_root": true, "detail": "mvn clean install -DskipTests in /work/repos/velox4j (against /work/repos/velox, JDK /usr/lib/jvm/java-17-openjdk); velox4j-*.jar -> lib",
       "plan": ["# JAVA_HOME=… VELOX4J_VELOX_SOURCE_DIR=/work/repos/velox VELOX4J_BUILD_JOBS=8", "mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in /work/repos/velox4j)", "cp /work/repos/velox4j/target/velox4j-*.jar /opt/flink/lib/"], "actions": []},
      {"name": "gluten", "status": "todo", "needs_root": true, "detail": "… ; nexmark nexmark-flink-0.3-SNAPSHOT.jar installed into ~/.m2 first", "plan": ["…"], "actions": []},
      {"name": "runtime-deps", "status": "todo", "needs_root": true, "detail": "14 of 14 runtime dependencies from ~/.m2 -> lib", "plan": ["cp ~/.m2/repository/com/google/guava/guava/33.4.0-jre/guava-33.4.0-jre.jar /opt/flink/lib/", "…"], "found": ["…"], "missing": [], "actions": []},
      {"name": "pin-jdk", "status": "todo", "needs_root": true, "detail": "conf has no env.java.home: append env.java.home: /usr/lib/jvm/java-17-openjdk (gluten jars carry Java 17 bytecode)", "plan": ["echo \"env.java.home: /usr/lib/jvm/java-17-openjdk\" >> /opt/flink/conf/flink-conf.yaml"], "conf": "/opt/flink/conf/flink-conf.yaml", "action": "append", "actions": []}
    ],
    "changes": [],
    "root_steps": ["backup", "velox4j", "gluten", "runtime-deps", "pin-jdk"],
    "next_step": "as root: review the plan above, then run stack_build.py apply --yes (the cluster must be restarted afterwards: flink-velox-cluster-switch)",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`（前置缺失时只有一条 `prereqs`）；`backup` 有 `backup_dir` / `backup_list`；`runtime-deps` 有 `found` / `missing`；`pin-jdk` 有 `conf` / `action`（append / warn / none）
- `changes`（apply）：`[{step, action: backed up|built+deployed|installed nexmark-flink into ~/.m2|deployed|warned|configured|skipped|failed, detail}]`
- `next_step`：plan 给"看计划后 apply"（需 root 时注明 as root），apply 失败给修哪一步并提示上一套在 lib.bak，成功给"重启集群后 verify"；`error_message`：`refused: ...` / `<step> failed` / `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（lib-state 一条）

## 边界与相邻 skill

- `flink-velox-env` 家族：lib-state-check——本 skill 的 `verify` 调它（`--expect gfv`）；它的 fail 就是本 skill 的工单
- `flink-velox-build-env-prepare`：三仓、mvn、JDK 17、源码级库——本 skill 的前置；`flink-velox-runtime-install`：Flink 与 nexmark jar——gluten 构建依赖 nexmark 进 ~/.m2
- `flink-velox-cluster-switch`（规划中）：部署后重启集群让新构建生效，须用户同意
- 本 skill 不起停集群、不提交作业、不改 lib 里 GFV 集与运行依赖以外的任何 jar
