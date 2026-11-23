---
name: flink-velox-jdk-check
description: >
  Flink + GlutenFlink（velox 向量化栈）Java 环境只读检查，一遍回答三件事：机器上有没有 JDK 17（构建要用）、
  JAVA_HOME 是不是构建脚本该取的那个（它们优先读 JAVA_HOME）、Flink 守护进程跑的是哪个 Java 以及重启会选到哪个
  （conf env.java.home → JAVA_HOME → PATH，/proc 读运行中进程）。goal=build 要求 Java 17（GFV jar 是 17 字节码），
  goal=native 接受 8/11/17。逐项 ok/warn/fail + fix + 唯一 next_step。只扫不改：不装 JDK、不改 conf、不重启集群。
  触发词：JDK 17 装了没、JAVA_HOME 对不对、Flink 用的哪个 Java、守护进程 Java、UnsupportedClassVersionError、
  java 版本。不负责：编译工具链 → flink-velox-toolchain-check；Flink 发行版与版本线 → flink-velox-flink-dist-check；
  "能不能编译 GFV 栈"的总裁决 → flink-velox-env-check；重启集群 → 切换类 skill（规划中）且须用户同意。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库；读 /proc 与 Flink conf
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink-velox Java 环境检查

GFV 栈对 Java 有两层要求：编译 velox4j 与 gluten-flink 要 JDK 17，而且构建脚本**优先读 JAVA_HOME**——JAVA_HOME 指向别的 JDK 时哪怕机器上有 JDK 17 也会编错；运行时 Flink 1.19 本身能在 Java 8 / 11 / 17 上跑，但 GFV 的 jar 是 Java 17 字节码，守护进程用 Java 8 跑会在类加载时报 `UnsupportedClassVersionError`。从这种报错倒查"到底哪个环节选错了 Java"很慢。本 skill 只做一件事：**把 JDK 17、JAVA_HOME、守护进程实际 Java 与重启将选的 Java 逐项核对**。它是 `flink-velox-env` 检查家族里"Java"这一具体项：只读、不改动机器。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 搭 GFV 构建环境前后，确认 JDK 17 在、JAVA_HOME 指对了
- Flink 作业报 `UnsupportedClassVersionError`、类加载失败时，先查守护进程跑的 Java 版本
- 改了 Flink conf 的 `env.java.home` 或 JAVA_HOME 之后、重启集群之前，确认重启会选到哪个 Java
- 换机器、换容器后的 Java 环境确认

### 不适用（不要用本 skill）

- 指望它装 JDK、改 conf 或重启集群——`fix` 与 `next_step` 只是工单；重启须用户同意，交给切换类 skill（规划中）
- git / cmake / gcc / g++ / mvn → `flink-velox-toolchain-check`；ninja / ccache 等 → `flink-velox-build-tools-check`
- Flink 发行版是否齐全、是否在 1.19 版本线 → `flink-velox-flink-dist-check`
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`

## 两种目标（goal）

| goal | Java 要求 | 默认 |
|---|---|---|
| `build` | 守护进程实际 Java 与重启将选的 Java 都必须是 17（GFV jar 是 17 字节码） | 是 |
| `native` | 8 / 11 / 17 均可（Flink 1.19 自身支持），能读到版本即可 | 用户问"能不能跑 native 侧"时 |

JDK 17 与 JAVA_HOME 两项在任何目标下都检查——它们描述的是构建侧的事实。

## 检查项

| 条目 | 怎么判 |
|---|---|
| `jdk17` | 依次在 JAVA_HOME → PATH 上的 `java`（取其真实路径的上两级）→ `/usr/lib/jvm/java-17-*` 找**有 `bin/javac` 且版本主号为 17** 的目录；找不到为 fail |
| `JAVA_HOME` | 未设为 warn（构建回退到找到的 JDK 17）；设了但不是 JDK（无 `bin/javac`）或主号不是 17 为 fail——构建脚本优先取它 |
| `flink` | 定位 Flink 安装（`FLINK_HOME` → PATH 上的 `flink` → `/opt/flink` → `/usr/local/flink`）；找不到为 warn：读不到 conf 的 `env.java.home`、也无法把运行中的守护进程归属到本安装 |
| `startup`（重启会选的 Java） | conf `env.java.home` → `JAVA_HOME` → PATH 上的 `java`，按 Flink 自身 `bash-java-utils.sh` 的顺序；路径无 `bin/java` 或版本读不出为 fail；`build` 下主号不是 17 为 fail |
| `running`（守护进程实际 Java） | 扫 `/proc/*/cmdline`，主类是 JobManager / TaskManager 入口且 classpath 里的 `flink-dist*.jar` 位于**本安装**的 `lib/`（其它安装的守护进程不计）的进程，取其 `argv[0]` 真实路径的上两级为 Java home；版本判定同 `startup` |
| `java`（合并） | 运行中的守护进程与重启将选的是同一 Java 时合并为一条，`found_via` 注明"the running daemons use it" |

## 执行步骤

### 步骤 1：定目标、扫描

```bash
python3 <skill_dir>/scripts/jdk_check.py [--goal build|native] [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `jdk         OK       JDK 17.0.18 at … (via JAVA_HOME); JAVA_HOME ok; java: Java 17.0.18 (…)`；缺了逐项 `FAIL <条目>: …` 并附 `fix:`；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/jdk-check.json`（留档规则见「环境约定」）。

退出码：**0** 没有 fail（warn 允许）；**1** 有 fail；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——它按"装 JDK 17 → 修 JAVA_HOME → 修守护进程/重启会选的 Java"的顺序给出唯一一个下一步。
2. `running` 与 `startup` 都在时分别读：`running` fail 而 `startup` ok = 集群还在旧 Java 上，改了 conf 没重启；反之 = 重启会换到一个错的 Java。
3. `JAVA_HOME` 的 warn 不是问题（构建回退到机器上的 JDK 17），但要转述；`flink` 的 warn 说明没找到安装，conf 与守护进程两项没查。
4. 本 skill 不改任何东西：`fix` 里涉及重启的，交给切换类 skill 并**征得用户同意**；装 JDK、改 conf 由安装类 skill（规划中）执行，做完重扫直到 READY。

## 环境约定

- **只读**：只查文件与可执行位、读 `release` 文件或运行 `java -version`、读 Flink conf 与 `/proc/<pid>/cmdline`；不写任何文件（`--out` 指定的除外）
- `JAVA_HOME` 与 `env.java.home` 路径按原样比较其真实路径；守护进程归属只认 classpath 中 `flink-dist*.jar` 所在目录与本安装 `lib/` 的真实路径相等
- `ENV_CHECK_SYSROOT`（给所有绝对系统路径加前缀）与 `ENV_CHECK_PROC_ROOT`（替换 /proc）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`goal`、`jdk17`、`cluster_java`、`running_java`、`missing`、`next_step`、`error_message` 必须保留（下例：集群还在 Java 8 上而 conf 未设，节选）：

```json
{
  "flink_velox_jdk_check_result": {
    "success": false,
    "checked_at": "2026-11-24T09:30:00+08:00",
    "goal": "build",
    "status": "fail",
    "jdk17": {"path": "/usr/lib/jvm/java-17-openjdk", "version": "17.0.18", "found_via": "JAVA_HOME"},
    "jdk8": "/usr/lib/jvm/java-1.8.0-openjdk",
    "java_home": "/usr/lib/jvm/java-17-openjdk",
    "flink_home": {"path": "/usr/local/flink", "found_via": "FLINK_HOME"},
    "cluster_java": {"path": "/usr/lib/jvm/java-17-openjdk", "version": "17.0.18", "major": 17, "found_via": "JAVA_HOME"},
    "running_java": {"path": "/usr/lib/jvm/java-1.8.0-openjdk", "version": "1.8.0_412", "major": 8, "found_via": "running process (pid 1234, taskmanager)"},
    "items": [
      {"name": "jdk17", "status": "ok", "detail": "17.0.18 at /usr/lib/jvm/java-17-openjdk (via JAVA_HOME)"},
      {"name": "running", "status": "fail", "detail": "Java 1.8.0_412 (…, via running process (pid 1234, taskmanager)): the gluten jars carry Java 17 bytecode and fail to load on it (UnsupportedClassVersionError)", "fix": "set env.java.home in the Flink conf to a JDK 17 and restart the cluster (a restart needs the user consent)"},
      {"name": "startup", "status": "ok", "detail": "Java 17.0.18 (…, via JAVA_HOME)"}
    ],
    "missing": ["running: Java 1.8.0_412 …"],
    "next_step": "set env.java.home in the Flink conf to a JDK 17 and restart the cluster (a restart needs the user consent), then rerun",
    "error_message": "1 item(s) missing: running"
  }
}
```

- 原始观测：`jdk17`（`path` / `version` / `found_via`）、`jdk8`（有则给出路径，仅信息）、`java_home`、`flink_home`、`cluster_java`（重启会选的：`path` / `version` / `major` / `found_via`）、`running_java`（守护进程实际的，无则为 null）；`items` 是逐项判定（`name` / `status` / `detail`，fail 与部分 warn 另带 `fix`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `running: Java 1.8.0_412 …`
- 失败形态：`success: false`，`error_message` 形如 `1 item(s) missing: running`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-toolchain-check`、`flink-velox-build-tools-check`、`flink-velox-cpp-deps-check`：构建环境侧的兄弟检查项，触发词不重叠
- `flink-velox-flink-dist-check`：Flink 安装、文件齐全与 1.19 版本线——本 skill 只借安装目录读 conf 与归属守护进程，不判版本
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 装 JDK、改 conf、重启集群：同族安装 / 切换类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
