# 安装表与判据：Flink 运行时与 nexmark

脚本 `scripts/runtime_install.py` 的探测判据、命令与退出语义。

## 1. flink

| 项 | 值 |
|---|---|
| 版本 | `--version`（默认 1.19.2），必须匹配 `1.19.x`，否则 plan / apply 退出 2 |
| 目录 | `<prefix>/flink-<版本>/`（默认 `/opt`）；软链 `<prefix>/flink` → 该目录 |
| 探测 | `bin/flink` 可执行 = 已装；软链 `realpath` == 版本目录 = 已指对；Flink 实际读的那份 conf 存在且没有任何非注释非空行 = 需补最小配置 |
| 配置文件 | conf 目录里 `flink-conf.yaml` 存在就只认它，否则 `config.yaml`（Flink 1.19 两份并存时只读前者——1.19.2 的 `GlobalConfiguration.loadConfiguration` 字节码实证）；不合并两份 |
| 下载 | `curl -fsSL --retry 3 --max-time 3600 -o <tmp>/<tgz> <mirror>/flink-<版本>/<tgz>`，失败再 `<archive>/flink-<版本>/<tgz>`；镜像默认 `https://mirrors.huaweicloud.com/apache/flink`，归档恒为 `https://archive.apache.org/dist/flink` |
| 校验 | `curl -fsSL --retry 3 --max-time 120 -o <tmp>/<tgz>.sha512 <archive>/flink-<版本>/<tgz>.sha512`（**只从归档取**，镜像不带校验文件）→ `(cd <tmp> && sha512sum -c <tgz>.sha512)` |
| 解压 | `tar -xzf <tmp>/<tgz> -C <prefix>`，之后核对 `bin/flink` 可执行 |
| 软链 | 先建 `<prefix>/flink.runtime-install.<pid>.<随机>.tmp` 再 `os.replace` 到 `<prefix>/flink`——临时名是本次运行独占的，已有文件不会被 unlink；若 `<prefix>/flink` 已是真实目录 → 失败、不覆盖 |
| 最小配置 | 追加 11 个键：`jobmanager.rpc.address: localhost`、`jobmanager.rpc.port: 6123`、`jobmanager.bind-host: localhost`、`taskmanager.bind-host: 0.0.0.0`、`taskmanager.host: localhost`、`taskmanager.data.bind-host: 0.0.0.0`、`rest.bind-address: localhost`、`client.address: localhost`、`jobmanager.memory.process.size: 2048m`、`taskmanager.memory.process.size: 8192m`、`taskmanager.numberOfTaskSlots: 8`——只在文件全注释时 |
| 前置 | `curl`、`sha512sum`、`tar` 在 PATH，缺则 `blocked` |
| 需 root | `<prefix>`（或其最近存在的祖先）对当前用户不可写 |

## 2. nexmark-jar

| 项 | 值 |
|---|---|
| 探测 | `<prefix>/flink-<版本>/lib/*nexmark*.jar`（**正在安装的版本目录**，不是 `<prefix>/flink` 软链今天指向的那个）存在 → 跳过：**不静默**——detail 逐个列出现有 jar 的 sha256（重新计算）、本次请求的 `--nexmark-ref`，以及"要换 ref 先移除旧 jar 或装别的版本 / prefix"；跳过合法（幂等），`--nexmark-ref` 变更不会被忽略而是明示为"保留现有 jar、不重建" |
| 固定版本 | `--nexmark-ref`（默认 commit `6b3646c3baec701f1fa74baf938d235f742e5d3c`：`github.com/nexmark/nexmark` 默认分支在 2025-12-26 的提交，"[Flink] Fix unexpected job cancellation (#68)"；本机 GlutenFlink 环境用的就是它。上游没有 tag / 发布件，nexmark-flink 只能从源码构建）。给 40 位 commit 时取到该 commit 并核对 `HEAD`；给 tag / 分支时 `git clone --depth 1 --branch <ref>` 并把 `HEAD` 记入 `changes`——**tag / 分支只属于用户自己可写前缀的安装**：由 root 安装（privileged，euid 0）时 `--nexmark-ref` 必须是 commit 形态，tag / 分支是可变引用、没法钉死核对，`plan` / `apply` 直接拒绝（退出 2，`error_message` / `next_step` 说明要 commit 形态）；不用 `git ls-remote` 解析（机器上实证会超时） |
| 源码位置 | `<repos-dir>/nexmark`（`--repos-dir` → `$GFV_REPOS_DIR` → `<原用户 home>/repos`） |
| 取源码 | 每次尝试在 `<repos-dir>/.nexmark-clone-*/nexmark` 这个独占临时目录里 `git init` → `remote add` → `fetch --depth 1 origin <commit>` → `checkout --detach FETCH_HEAD`，核对 `HEAD` 后 `rename` 到位；失败重试 3 次（间隔 `ENV_PREPARE_RETRY_SLEEP`，默认 5 秒），**只清理本次的临时目录**。经 sudo 时临时目录与本次新建的 `<repos-dir>` 建出来就 `chown` 给原用户（uid / gid 取自 passwd）：root 建的目录是 0700、归 root，降权的 git 进不去 |
| 已有目标 | 是完整 checkout：核对 `HEAD`；不是 checkout（非空、无 `.git`）→ `blocked`，不克隆覆盖、不删除 |
| 已有 checkout 的门槛 | `HEAD` ≠ 请求的 commit → `blocked`（fix：显式 `--nexmark-ref <该 commit>` 或挪走）。由 root 安装（euid 0，无论有没有 `SUDO_USER`）时还要求：origin 是官方地址、`git status --porcelain --untracked-files=all` 为空 |
| 核对过的副本 | 由 root 安装时构建不碰 checkout 本身：拷一份到 `<prefix>/.nexmark-build-*/src`（`mkdtemp`，root 拥有 0700，不带 `.git`），逐文件与 commit 的 tree 比对——`git ls-tree -r -z <钉死的 commit>`（**基准取自钉死 commit 的 SHA 本身，不取可动的 HEAD**；给未知 SHA 或读不到时判"无法列出"→ `failed`）给出每个路径的 mode 与 blob 哈希，副本里的文件按同样的算法算哈希（sha1 / sha256 随 tree 的对象格式）；不在 commit 里、内容不同、执行位不同、缺失都算，对不上判该步 `failed` 并点名文件（最多列 5 个）。所有只读 git 查询（`rev-parse` / `get-url` / `status` / `ls-tree`）都带 `GIT_NO_REPLACE_OBJECTS=1` 执行——`refs/replace` 换不掉 SHA 名下的 tree。**守卫复验**：副本拷完、基准读完之后 `untrusted_reasons` 再核一遍（HEAD 仍等于钉死 commit、origin 仍官方、status 仍干净），不一致判该步 `failed`。已有 checkout 与本次刚克隆的走同一套；jar 已部署、只补可运行树时同样先拷先核对。副本用完即删 |
| 降权 | euid 0 且有 `SUDO_USER`：git（克隆与只读查询）走 `runuser -u <user> -- …`（保留环境变量，HOME 换成该用户的），root 不在原用户的 checkout 里跑 git；`runuser` 不在 PATH → `blocked`。mvn 不降权：由 root 在核对过的副本里跑，`HOME` 换成 root 自己的，`~/.m2`（settings 与本地仓库）读不到原用户的 |
| 前置 | `mvn` 在 PATH；JDK：`JAVA_HOME` → `/usr/lib/jvm/java-17-*` → `/usr/lib/jvm/java-1.8.0-openjdk-*`（须有 `bin/javac`）；要取源码时 `git` 在 PATH |
| 构建 | 非 root：在源码目录；由 root 安装：在核对过的副本里。`JAVA_HOME=<jdk> mvn -pl nexmark-flink -am package -DskipTests`（14400 秒超时）；`changes` 里 `verified copy` 一条说明拷了多少文件、对的是哪个 commit |
| 部署 | `nexmark-flink/target/nexmark-flink-*.jar` 中排除 `-sources` 的第一个（名序）拷进 `<prefix>/flink-<版本>/lib`；`changes` 里记 jar 的 sha256 |
| 需 root | 该 lib（或其最近存在的祖先）不可写 |

## 3. nexmark-tree

清单（`run_query.sh` 的 classpath 是 `$NEXMARK_HOME/lib/*:$FLINK_HOME/lib/*`，脚本会 `.` 进 `bin/config.sh`）：`bin/run_query.sh`（可执行）、`bin/config.sh`（可执行）、`conf/log4j.properties`、`conf/nexmark.yaml`、`conf/config.yaml`、`queries/ddl_gen.sql`、`queries/ddl_kafka.sql`、`queries/ddl_views.sql`、`queries/insert_kafka.sql`、至少一个 `queries/q*.sql`。

| 项 | 值 |
|---|---|
| 探测 | 清单齐全 → 跳过；否则列出缺项（`missing`） |
| 前置 | nexmark-jar 步骤不 blocked；jar 已部署（不会再去取源码）而 `<src>/nexmark-flink/src/main/resources` 不在 → `blocked`，fix 给取源码的命令 |
| 命令 | `mkdir -p <home>`；把 `<res>/{bin,conf,queries,queries-cep}` 里 `<home>` 没有的文件逐个拷过去（**已有的不覆盖**；四个目录缺任一 → 失败）；`mkdir -p <home>/{lib,log,data}`；jar 还不在 `<home>/lib/` 时拷一份 |
| 需 root | `<nexmark-home>` 不可写 |

## 4. 异常

步骤里的命令无法执行、超时、拷贝出错，都算**该步失败**（退出 1，detail 记异常类型与信息），不是工具崩溃；之前步骤与当前步骤已完成的条目都留在 `changes` 里。`tool aborted:`（退出 2）只用于步骤之外的工具错误。

## 5. apply 的拒绝条件（退出 2）

缺 `--yes`；版本不在 1.19 线；由 root 安装而 `--nexmark-ref` 不是 commit 形态（tag / 分支，`--no-nexmark` 时该 ref 不参与、不拒）；任一步 `blocked`（信息带该步 detail 与 fix）；有需 root 的 TODO 步骤而 `euid != 0`（提示里的 sudo 命令是完整的——`sudo python3 <脚本绝对路径> …`，照抄即可——并带着本次的显式选项）；`--out` 不合法（校验先于任何动作）。

## 6. verify

调 flink-dist-check（`flink_dist_check.py --json`）与 nexmark-check（`nexmark_check.py --json`），环境 `FLINK_HOME=<prefix>/flink`、`NEXMARK_HOME=<nexmark-home>`、`ENV_CHECK_SYSROOT`。`--no-nexmark` 时 nexmark 记 `disabled`。兄弟检查的定位（相对本脚本目录）：指定了 `ENV_PREPARE_ENV_FAMILY_DIR` 就只认它下面的 `<检查 skill>/scripts/<脚本>`（没有就是 `missing`，别处不再找）；否则按 skill 名依次找源码树 `../../../flink-velox-env/<检查 skill>/scripts/<脚本>` → 安装后平铺 `../../<检查 skill>/scripts/<脚本>`（install.sh 把每个 skill 拷到 `<config>/skills/<skill>/`）→ 平铺且保留子域 `../../flink-velox-env/<检查 skill>/scripts/<脚本>`。找不到 → `missing`；跑了但没有 JSON → `fail`。全部 ok（`disabled` 除外）退出 0；有 `fail` 或 `missing` 退出 1（`missing` 时人读报告是 `NOT VERIFIED`，不是通过）。

## 7. --out

任何动作开始前先校验（项目目录内、非符号链接、已存在须 `--overwrite`、已存在的须是普通文件——管道文件会让写入无限等待读者），不合法 → 退出 2 且不做任何事；写入前再校验一次。

## 8. 来源

Flink 版本、目录布局（`/opt/flink-<版本>` + `/opt/flink` 软链）、镜像与校验来源（华为云取包、sha512 恒取 archive.apache.org）、11 个最小配置键、nexmark 的构建 / 部署 / 可运行树铺法来自 GFVBot 安装器的 `install_flink` / `ensure_flink_conf` / `install_nexmark`。与它的差别：nexmark 取到固定 commit 而不是默认分支最新；jar 记 sha256；可运行树按清单补齐；已有目录不删。
