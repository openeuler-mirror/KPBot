# 构建表与判据：GlutenFlink 构建编译部署

脚本 `scripts/stack_build.py` 的探测判据、命令与退出语义。

## 0. 前置（任一缺失 → 整份计划 BLOCKED）

| 项 | 判据 | 缺了指向 |
|---|---|---|
| 源码仓（**按 `--only` 只查本次构建读的**） | `<repos-dir>/<仓>`（或 `VELOX_HOME` / `VELOX4J_HOME` / `GLUTEN_HOME`）含标志文件（velox：`CMakeLists.txt` + `scripts/`；velox4j：`pom.xml`；gluten：`gluten-flink/pom.xml`）。全量 = velox / velox4j / gluten 三仓全要；`--only velox4j` = velox + velox4j（native `.so` 对着 velox 源码编）；`--only gluten` = gluten——gluten 构建不读 velox / velox4j 源码仓，缺它们不再拦；JDK / mvn / Flink / runuser 等其余前置不受 `--only` 过滤 | flink-velox-build-env-prepare |
| velox4j 构建产物（**仅 `--only gluten` 查**） | gluten-flink 的 planner / runtime 以 provided 依赖 `io.github.zhztheplayer:velox4j`，版本 = gluten-flink/pom.xml 的 `velox4j.version` 属性，从本地 Maven 仓库解析（SNAPSHOT，远端仓库不提供，只有 velox4j checkout 构建过一次才会在 `~/.m2`）；`<原用户 home>/.m2/repository/io/github/zhztheplayer/velox4j/<version>/velox4j-<version>.jar` 在 → 过；属性读不出 → 同样 BLOCKED（宁可拦下也不放行一个注定解析失败的构建）。全量构建不查：velox4j 步骤先跑，本趟就把它装进 `~/.m2` | 先跑本 skill 的 `--only velox4j`（或全量构建） |
| JDK 17 | `JAVA_HOME` → PATH 上 `java` 的真实路径上两级 → `/usr/lib/jvm/java-17-*`；**每个候选都核对**：有 `bin/javac` 且 `release` 的 `JAVA_VERSION` 主号是 17（目录名不算数），不符或读不出就继续找 | 同上 |
| mvn | PATH 上有 | 同上 |
| Flink | `--flink-home` 给了就是唯一候选（否则 `FLINK_HOME` → PATH 上 `flink` 上两级 → `/opt/flink` → `/usr/local/flink`），须有 `bin/flink` | flink-velox-runtime-install |
| runuser | 仅经 sudo 运行时需要（把 Maven 构建交回原用户） | 系统包 util-linux |

`<repos-dir>`：`--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`（经 sudo 时是原用户的 home）。

每个可用的 checkout 记下出处：`git -C <仓> remote get-url origin`、`rev-parse HEAD`、`status --porcelain` 是否非空（`.git` 不存在或 git 不在 PATH 时记"不是 git checkout"）；不拦任何来源，只留档。

## 1. backup

清单 = **本次会被替换的 jar**：全量构建 = lib 里文件名匹配 `^(gluten|velox4j)` 的 + 14 个运行依赖（按 `artifactId-` 前缀匹配）；`--only velox4j` = 只 `velox4j*`；`--only gluten` = 只 `gluten*`。为空则 `skipped`。apply：`mkdir <lib>.bak/<YYYYmmdd-HHMMSS>`（已存在则加 `-1`、`-2`…），逐个 `copy2` 进备份目录后从 lib `unlink`，每个 jar 一条 `changes`。不重建的 GlutenFlink 组件与运行依赖不搬、不动，计划里注明。

## 2. velox4j

| 项 | 值 |
|---|---|
| 命令 | 在 velox4j 源码目录：`mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true` |
| 环境 | `JAVA_HOME=<JDK 17>`、`PATH=<JDK>/bin:$PATH`、`VELOX4J_VELOX_SOURCE_DIR=<velox 源码>`、`VELOX4J_BUILD_JOBS=min(8, --jobs 或核数)` |
| 产物 | `target/velox4j-*.jar`（名序，排除 `-sources` / `-javadoc`）→ `copy2` 进 lib；`changes` 记 `<jar> -> <lib> (source: <origin> @ <commit>[" with local modifications"])` |
| 失败 | mvn 非零、命令无法执行或超时、target 无 jar → `failed`，后续步骤不跑 |

## 3. gluten

| 项 | 值 |
|---|---|
| nexmark 前置 | lib 里有 `*nexmark*.jar`（名序第一个）→ 用 `zipfile` 取出 `META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml` 到临时目录，`mvn install:install-file -Dfile=<jar> -DpomFile=<tmp>/pom.xml`（临时目录用完即删）；没有 nexmark jar → 跳过并在 detail / plan 注明 |
| 命令 | 在 `gluten/gluten-flink`：`mvn clean install -DskipTests -pl planner,loader,runtime -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true` |
| 产物 | `planner/target/gluten-flink-planner-*.jar`、`runtime/target/gluten-flink-runtime-*.jar`、`loader/target/gluten-flink-loader-*.jar`（各取名序第一个、排除 `-sources` / `-javadoc`）→ 逐个 `copy2` 进 lib |
| 失败 | install-file 或 mvn 非零、任一模块无 jar → `failed`（已部署的 velox4j 保留在 lib） |

## 4. 降权（经 sudo）

euid 为 0 且环境里有 `SUDO_USER`（非 root）时，上两步的 mvn 与 nexmark 的 install-file 都走 `runuser -u <SUDO_USER> -- mvn …`：`runuser -u` 保留环境变量（`JAVA_HOME`、`VELOX4J_*` 照常传到）、把 HOME 换成该用户的，所以 `~/.m2` 是原用户的（util-linux 2.39 实测）。备份与往 lib 部署仍是 root。没有 `SUDO_USER`（真正的 root 登录 / 容器）时构建就是 root 跑——这时源码出处留档是唯一的把关，计划里看清楚。

root 建的目录归 root 且是 0700，降权用户进不去：install-file 用的临时目录建出来就 `chown` 给该用户（uid / gid 取自 passwd），降权的 mvn 才读得到里面的 pom；对 checkout 的 git 查询（出处留档）也以该用户执行，root 不在它的 checkout 里跑 git（仓库配置能让 git 执行程序，例如 `core.fsmonitor`）。

**信任边界**：经 sudo 运行时装进 lib 的是原用户 checkout 构建出的 jar——部署自己的构建正是本 skill 的用途，所以能 sudo 运行它的用户就能决定集群加载什么代码，sudoers 授权时按此对待。这与 `flink-velox-runtime-install` 不同：那里装的是固定上游 commit 的产物，构建前逐文件核对。

## 5. runtime-deps

14 个 `group:artifact:version`（guava 33.4.0-jre、failureaccess 1.0.2、arrow-cdata / format / memory-core / memory-unsafe / vector 18.1.0、commons-io 2.18.0、flatbuffers-java 24.3.25、jackson-annotations / core / databind 2.18.0、jackson-datatype-jdk8 / jsr310 2.18.0）；路径 `<原用户 home>/.m2/repository/<group 点换斜杠>/<artifact>/<version>/<artifact>-<version>.jar`。plan 里给的是**当时的快照**（注明构建后复查）；apply 里在两条 mvn **之后**重新找：在的 `copy2` 进 lib，仍不在的 → 该步 `failed`（退出 1），`changes` 记 `failed: not in ~/.m2 after the builds: …`，`next:` 写明 **不要重启集群**（缺运行依赖的 GlutenFlink 集加载不了）。

## 6. pin-jdk

配置文件 = Flink 实际读的那一份：conf 目录里 `flink-conf.yaml` 存在就只认它，否则 `config.yaml`（Flink 1.19 两份并存时只读前者——1.19.2 的 `GlobalConfiguration.loadConfiguration` 字节码实证；所以绝不在只有 `config.yaml` 的安装上新建 `flink-conf.yaml`，否则原来的端口 / 内存设置全部失效）。读它的 `env.java.home:`：无 → 追加 `env.java.home: <构建 JDK>`（`action: append`）；有且 `realpath` 等于构建 JDK → `skipped`（`none`）；有但不同 → `skipped` 并在 detail 标 `WARNING`（`warn`），**不改**。

## 7. 异常

步骤里的命令无法执行、超时、拷贝出错，都算**该步失败**（退出 1，detail 记异常类型与信息），不是工具崩溃；之前步骤与当前步骤已完成的条目都留在 `changes` 里。`tool aborted:`（退出 2）只用于步骤之外的工具错误。

## 8. apply 的拒绝条件（退出 2）

缺 `--yes`；前置 BLOCKED；有需 root 的 TODO 步骤（lib 或 conf 对当前用户不可写，看目标或其最近存在的祖先）而 `euid != 0`（提示里的 sudo 命令与 `next_step` 里的命令一样是完整的——`sudo python3 <脚本绝对路径> …`，照抄即可——并带着本次的 `--repos-dir` / `--only` / `--jobs` / `--flink-home`）；`--out` 不合法（校验先于任何动作）。

## 9. verify

调 lib-state-check（`lib_state_check.py --json --expect gfv`，环境 `FLINK_HOME` 取本次解析到的安装）；`repos` 等观测字段与 plan / apply 用同一份选项（`--repos-dir` 不再被忽略）。前置与 plan / apply 同检：显式 `--flink-home` 不是一套 Flink 安装（没有 `bin/flink`，或路径不存在）时 `verify` 按 apply 口径拒绝——`success=false`、退出 2、人读报告 `REFUSED`，不再把空 `FLINK_HOME` 传给兄弟检查（兄弟会回退到 PATH / `/opt/flink` / `/usr/local/flink`，可能对机器上**另一套** Flink 报 VERIFIED）；非显式（发现逻辑选定）时行为不变。兄弟检查的定位（相对本脚本目录）：指定了 `ENV_PREPARE_ENV_FAMILY_DIR` 就只认它下面的 `<检查 skill>/scripts/<脚本>`（没有就是 `missing`，别处不再找）；否则按 skill 名依次找源码树 `../../../flink-velox-env/<检查 skill>/scripts/<脚本>` → 安装后平铺 `../../<检查 skill>/scripts/<脚本>`（install.sh 把每个 skill 拷到 `<config>/skills/<skill>/`）→ 平铺且保留子域 `../../flink-velox-env/<检查 skill>/scripts/<脚本>`。找不到 → `missing`；跑了但没有 JSON → `fail`。全部 ok 退出 0；有 `fail` 或 `missing` 退出 1（`missing` 时人读报告是 `NOT VERIFIED`，不是通过）；拒绝（显式目标不成立）退出 2。

## 10. --out

任何动作开始前先校验（项目目录内、非符号链接、已存在须 `--overwrite`、已存在的须是普通文件——管道文件会让写入无限等待读者），不合法 → 退出 2 且不做任何事；写入前再校验一次。

## 11. 来源

五个步骤、mvn 参数、`VELOX4J_VELOX_SOURCE_DIR` / `VELOX4J_BUILD_JOBS=min(8, cores)`、nexmark install-file 的 pom 取法、14 个运行依赖清单、`pin_flink_jdk` 的只填不覆盖语义均来自 GFVBot 的 `compile.sh`；与其差异：jar 名改 glob（原脚本硬编码 `gluten-flink-*-1.8.0-SNAPSHOT.jar`，真机与主线已是 1.9.0）、只备份会被替换的 jar、运行依赖在构建之后解析且缺失判失败、`env.java.home` 写进 Flink 实际读的那份配置、经 sudo 时构建以原用户执行、nexmark pom 用 zipfile 取而非 `jar -xf`、`/tmp` 零件改为临时目录用完即删。`--only gluten` 查的 velox4j 坐标（group `io.github.zhztheplayer`、版本属性名 `velox4j.version`）与 14 个运行依赖一样取自 gluten-flink / velox4j 实际的 pom，升级栈时要一并核对同步。
