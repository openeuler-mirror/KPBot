# 构建表与判据：GFV 构建编译部署

脚本 `scripts/stack_build.py` 的探测判据、命令与退出语义。

## 0. 前置（任一缺失 → 整份计划 BLOCKED）

| 项 | 判据 | 缺了指向 |
|---|---|---|
| velox / velox4j / gluten 源码仓 | `<repos-dir>/<仓>`（或 `VELOX_HOME` / `VELOX4J_HOME` / `GLUTEN_HOME`）含标志文件（velox：`CMakeLists.txt` + `scripts/`；velox4j：`pom.xml`；gluten：`gluten-flink/pom.xml`） | flink-velox-build-env-prepare |
| JDK 17 | `JAVA_HOME`（有 `bin/javac` 且 `release` 主号 17）→ PATH 上 `java` 的真实路径上两级 → `/usr/lib/jvm/java-17-*` | 同上 |
| mvn | PATH 上有 | 同上 |
| Flink | `FLINK_HOME` → PATH 上 `flink` 上两级 → `/opt/flink` → `/usr/local/flink`（有 `bin/flink`） | flink-velox-runtime-install |

`<repos-dir>`：`--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`。

## 1. backup

lib 里 `*.jar` 中文件名匹配 `^(gluten|velox4j)` 的，加上 14 个运行依赖（按 `artifactId-` 前缀匹配）→ 清单 `backup_list`；为空则 `skipped`。apply：`mkdir <lib>.bak/<YYYYmmdd-HHMMSS>`（已存在则加 `-1`、`-2`…），逐个 `copy2` 进备份目录后从 lib `unlink`。

## 2. velox4j

| 项 | 值 |
|---|---|
| 命令 | 在 velox4j 源码目录：`mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true` |
| 环境 | `JAVA_HOME=<JDK 17>`、`PATH=<JDK>/bin:$PATH`、`VELOX4J_VELOX_SOURCE_DIR=<velox 源码>`、`VELOX4J_BUILD_JOBS=min(8, --jobs 或核数)` |
| 产物 | `target/velox4j-*.jar`（名序，排除 `-sources` / `-javadoc`）→ `copy2` 进 lib |
| 失败 | mvn 非零或 target 无 jar → `failed`，后续步骤不跑 |

## 3. gluten

| 项 | 值 |
|---|---|
| nexmark 前置 | lib 里有 `*nexmark*.jar`（名序第一个）→ 用 `zipfile` 取出 `META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml` 到临时目录，`mvn install:install-file -Dfile=<jar> -DpomFile=<tmp>/pom.xml`（临时目录用完即删）；没有 nexmark jar → 跳过并在 detail / plan 注明 |
| 命令 | 在 `gluten/gluten-flink`：`mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true -pl planner,loader,runtime` |
| 产物 | `planner/target/gluten-flink-planner-*.jar`、`runtime/target/gluten-flink-runtime-*.jar`、`loader/target/gluten-flink-loader-*.jar`（各取名序第一个、排除 `-sources` / `-javadoc`）→ 逐个 `copy2` 进 lib |
| 失败 | install-file 或 mvn 非零、任一模块无 jar → `failed`（已部署的 velox4j 保留在 lib） |

## 4. runtime-deps

14 个 `group:artifact:version`（guava 33.4.0-jre、failureaccess 1.0.2、arrow-cdata / format / memory-core / memory-unsafe / vector 18.1.0、commons-io 2.18.0、flatbuffers-java 24.3.25、jackson-annotations / core / databind 2.18.0、jackson-datatype-jdk8 / jsr310 2.18.0）；路径 `~/.m2/repository/<group 点换斜杠>/<artifact>/<version>/<artifact>-<version>.jar`。在的 `copy2` 进 lib，不在的进 `missing` 并在 changes 里 `warned`——不判失败。一个都不在 → `skipped`。

## 5. pin-jdk

读 `conf/flink-conf.yaml` 的 `env.java.home:`：无 → 追加 `env.java.home: <构建 JDK>`（`action: append`）；有且 `realpath` 等于构建 JDK → `skipped`（`none`）；有但不同 → `skipped` 并在 detail 标 `WARNING`（`warn`），**不改**。

## 6. apply 的拒绝条件（退出 2）

缺 `--yes`；前置 BLOCKED；有需 root 的 TODO 步骤（lib 或 conf 对当前用户不可写，看目标或其最近存在的祖先）而 `euid != 0`（提示 `sudo python3 stack_build.py apply --yes`）。

## 7. verify

调 `../flink-velox-env/flink-velox-lib-state-check/scripts/lib_state_check.py --json --expect gfv`；脚本不存在 → `skipped`；无 JSON → `fail`；全部 ok 退出 0，否则 1。

## 8. 来源

五个步骤、mvn 参数、`VELOX4J_VELOX_SOURCE_DIR` / `VELOX4J_BUILD_JOBS=min(8, cores)`、nexmark install-file 的 pom 取法、14 个运行依赖清单、`pin_flink_jdk` 的只填不覆盖语义均来自 GFV 的 `compile.sh`；与其差异：jar 名改 glob（原脚本硬编码 `gluten-flink-*-1.8.0-SNAPSHOT.jar`，真机与主线已是 1.9.0）、部署前备份旧集、nexmark pom 用 zipfile 取而非 `jar -xf`、`/tmp` 零件改为临时目录用完即删。
