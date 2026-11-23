# 探测表与判据：Java 环境

脚本 `scripts/jdk_check.py` 的判读依据。条目名即 `items[].name`。

## 1. 探测

| 条目 | 怎么找 | 版本怎么取 |
|---|---|---|
| `jdk17` | JAVA_HOME → PATH 上 `java` 真实路径的上两级 → `/usr/lib/jvm/java-17-*`；每个候选须有 `bin/javac` 且主号 17 | `<home>/release` 的 `JAVA_VERSION=`，否则 `java -version` 的 `version "x"` |
| `JAVA_HOME` | 环境变量原值；有 `bin/javac` 才算 JDK | 同上 |
| `flink` | `FLINK_HOME` → PATH 上 `flink` 真实路径的上两级 → `/opt/flink` → `/usr/local/flink`，首个有 `bin/flink` 者 | — |
| `startup` | conf `env.java.home`（`conf/config.yaml` 嵌套键与 `conf/flink-conf.yaml` 扁平键都读）→ `JAVA_HOME` → PATH 上 `java` | 同上；路径无 `bin/java` 或读不出 = major null |
| `running` | `/proc/<pid>/cmdline`（NUL 分隔）：主类匹配 `org.apache.flink.runtime.entrypoint.*Entrypoint` / `…taskexecutor.TaskManagerRunner`，且 `-classpath` 里 `flink-dist*.jar` 所在目录的真实路径 == 本安装 `lib/` 的真实路径；Java home = `argv[0]` 真实路径的上两级 | 同上 |
| `java` | `running` 与 `startup` 是同一 Java（真实路径相等）时合并，`found_via` 追加 "; the running daemons use it" | — |

- `jdk8`（`java-1.8.0-openjdk-*` / `java-8-openjdk-*`）只作信息记录，不参与判定
- 没有 Flink 安装时 `startup` 仍按 JAVA_HOME → PATH 算，`running` 为 null，另加一条 `flink` warn

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| JDK 17 找到 | `jdk17` ok | — |
| JDK 17 找不到 | `jdk17` fail | `install a JDK 17 (java-17-openjdk-devel on openEuler/CentOS)` |
| JAVA_HOME 未设 | `JAVA_HOME` warn（注明构建回退到找到的 JDK 17） | — |
| JAVA_HOME 不是 JDK（无 bin/javac） | `JAVA_HOME` fail | `point JAVA_HOME at a JDK 17` |
| JAVA_HOME 主号 ≠ 17 | `JAVA_HOME` fail（构建脚本优先取它） | `point JAVA_HOME at the JDK 17 at <path>` / 装 JDK 17 |
| 没有 Flink 安装 | `flink` warn | — |
| startup/running 路径为空 | fail | `set env.java.home in the Flink conf (or JAVA_HOME)` |
| startup/running 无 bin/java 或版本读不出 | fail | `… to a JDK` |
| goal=build 且主号 ≠ 17 | fail（`UnsupportedClassVersionError`） | `set env.java.home in the Flink conf to a JDK 17 and restart the cluster (a restart needs the user consent)` |
| 其余 | ok | — |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表

## 3. next_step

按"先补 JDK → 修 JAVA_HOME → 修守护进程 Java"的顺序取第一个成立的：
1. 无 JDK 17 → `install a JDK 17 (java-17-openjdk-devel on openEuler) and point JAVA_HOME at it, then rerun`
2. JAVA_HOME 不是 JDK 17 → `point JAVA_HOME at the JDK 17 at <path>, then rerun`
3. 守护进程/重启 Java 需 17 而不是 → `set env.java.home in the Flink conf to a JDK 17 and restart the cluster (a restart needs the user consent), then rerun`；其它 Java fail → `set env.java.home in the Flink conf (or JAVA_HOME) to a JDK, then rerun`
4. 否则 `ready: the Java for the Flink daemons is in order`（守护进程条目有 warn 时附 `(warnings above)`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

探测顺序与判据来自 GFV 环境自检的 jdk / java 分组：构建脚本（velox4j、gluten-flink 的构建）优先读 JAVA_HOME；Flink 自身选 Java 的顺序见其 `bin/bash-java-utils.sh`（conf `env.java.home` → JAVA_HOME → PATH）；GFV jar 为 Java 17 字节码。
