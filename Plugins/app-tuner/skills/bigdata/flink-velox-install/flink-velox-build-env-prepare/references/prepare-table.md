# 准备表与判据：构建环境

脚本 `scripts/build_env_prepare.py` 的探测判据、命令与退出语义。

## 1. packages

| 项 | 值 |
|---|---|
| 探测 | `rpm -q <包>` 退出 0 = 已装；`rpm` 不在 PATH → 步骤 `blocked`（非 RPM 系统，openEuler 包名不适用）；`dnf` 不在 PATH → 步骤 `blocked`（装包要靠它） |
| 包（28，openEuler 名） | 工具链 `git cmake gcc gcc-c++ maven`；构建工具 `ninja-build ccache autoconf automake libtool flex bison python3 patchelf`；C++ 库 `libevent-devel openssl-devel re2-devel zstd-devel lz4-devel double-conversion-devel libdwarf-devel elfutils-libelf-devel libcurl-devel libicu-devel libsodium-devel zlib-devel librdkafka-devel`；`java-17-openjdk-devel` |
| 命令 | `dnf install -y <缺的包…>`（一次装齐） |
| 失败 | `dnf` 非零、命令无法执行或超时 → 步骤 `failed`，后续步骤不跑 |

包名与 env 家族 toolchain-check / build-tools-check / cpp-deps-check 的 `fix` 提示同表（g++ → gcc-c++、mvn → maven、ninja → ninja-build）。

## 2. repos

| 仓 | 上游 | 基线分支 | 标志文件 |
|---|---|---|---|
| velox | https://github.com/bigo-sg/velox.git | gluten-20260829 | `CMakeLists.txt`、`scripts/` |
| velox4j | https://github.com/bigo-sg/velox4j.git | gluten-20260829 | `pom.xml` |
| gluten | https://github.com/apache/gluten.git | main | `gluten-flink/pom.xml` |

- 目录存在且标志文件齐 → 跳过；存在但缺标志文件 → `blocked`（不自动删，fix：人工移走或修复）；不存在 → 待克隆
- 克隆：**每次尝试** `tempfile.mkdtemp(prefix=".<仓>-clone-", dir=<repos-dir>)` 建一个本次独占的临时目录，`git clone --quiet -b <基线> <上游> <临时目录>/<仓>`，成功后 `os.rename` 到目标位置；失败重试 3 次（间隔 `ENV_PREPARE_RETRY_SLEEP`，默认 5 秒），每次只清理本次的临时目录。挪动时目标位置已被别的进程建出来 → `rename` 失败 → 该步 `failed`，那个目录不动。3 次都失败 → `failed`，后续仓不克隆。两个 apply 同时克隆同一目标时谁也不会删谁的 checkout
- `--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`（经 sudo 时是原用户的 home）
- `git` 不在 PATH：packages 步骤的缺失清单里有 git → 不阻塞（计划里注明"装包后才有"）；否则 `blocked`
- 降权：euid 为 0 且有 `SUDO_USER` 时克隆走 `runuser -u <user> -- git clone …`（保留环境变量，HOME 换成该用户的）；`runuser` 不在 PATH → apply 拒绝
- 降权时目录的属主：root 建的目录是 0700、归 root，降权用户进不去——所以本次新建的 `<repos-dir>` 与每次尝试的临时目录建出来就 `chown` 给该用户（uid / gid 取自 passwd；`changes` 里记一条 `created directory`），克隆完的 checkout 归该用户；对 checkout 的 git 查询（origin / HEAD / 是否有本地改动）也以该用户执行，root 不在它的 checkout 里跑 git（仓库配置能让 git 执行程序，例如 `core.fsmonitor`）

## 3. source-deps

| 项 | 值 |
|---|---|
| velox checkout | `$VELOX_HOME`（有 `scripts/`）→ `<repos-dir>/velox`；都没有：repos 步骤会克隆 → `todo` + `deferred`；否则 `blocked` |
| 脚本 | Ubuntu/Debian → `scripts/setup-ubuntu.sh`；其余 → `scripts/setup-centos9.sh`（CentOS 7 没有，发行版层已拒绝）；`bash` 不在 PATH → `blocked` |
| 留档 | velox checkout 的 `git remote get-url origin` / `rev-parse HEAD` / `status --porcelain`（`.git` 不存在或无 git 时记"不是 git checkout"）与脚本文件的 sha256，进步骤（`source` / `setup_sha256`）与 detail；apply 时再写一条 `changes`（`source recorded`）。不拦任何来源：源码可以是自己的 fork、可以带本地改动，但"以什么脚本的 root 权限装库"必须看得见。apply 带 `--plan <plan 用 --out 留档的结果文件>` 时这份留档还是护栏：对将执行的脚本重新校验 sha256 与 origin / HEAD（apply 开始时与 source-deps 执行前各一次），与 plan 记录不一致、或记录指纹的 checkout 没了（只能重新克隆）→ 退出 2 拒绝——`--yes` 同意的是 plan 时的指纹；plan 时 velox 还没克隆（deferred、无指纹可记）则不比对，apply 自己克隆后照旧留档 |
| 清单 | 从 `function install_velox_deps {` 到第一个 `}` 之间的 `run_and_time install_<lib>` 行，去掉 `_from_dnf` / `_from_apt` / `conda`，保持顺序 |
| 已装探测 | `/usr/local/lib/cmake/<name|Capitalized|UPPER>` 或 `lib64/cmake/…` 目录、`/usr/local/include/<name>/`、`include/<name>.h` 或 `include/lib<name>.h`、探针二进制（protobuf → `bin/protoc`） |
| 命令 | 每个缺的库一个子 shell：`source <脚本> > /dev/null; set +x; install_<lib>()`，环境 `INSTALL_PREFIX=/usr/local`（sysroot 前缀）、`DEPENDENCY_DIR=$GFVBOT_SOURCE_DEPS_DIR|/tmp/gfvbot-source-deps`、`BUILD_THREADS=min(32, --jobs|核数)`、`PROMPT_ALWAYS_RESPOND=n` |
| 已有的依赖目录 | `<DEPENDENCY_DIR>/<lib>` 存在但没有构建入口文件（CMakeLists.txt / configure / Makefile / bootstrap.sh / setup.py）→ 该库不开始构建，步骤 `failed` 并指出目录（**不删**：目录不是本 skill 建的）；由用户删或修后重跑 |
| 失败 | 非零即停（后面的库依赖前面的），`built` 列出已构建的、`missing` 是本次本该构建的全部 |

## 4. 异常

步骤里的命令无法执行、超时，都算**该步失败**（退出 1，detail 记异常类型与信息），不是工具崩溃；之前步骤已完成的条目都留在 `changes` 里。`tool aborted:`（退出 2）只用于步骤之外的工具错误。

## 5. apply 的拒绝条件（退出 2）

缺 `--yes`；任一步 `blocked`（信息 = "the <步骤> step is blocked: <detail> — <fix>"）；有需 root 的 TODO 步骤而 `euid != 0`（提示里的 sudo 命令是完整的——`sudo python3 <脚本绝对路径> …`，照抄即可——并带着本次的 `--repos-dir` / `--jobs`）；发行版不受支持（非 openEuler / CentOS 9 / RHEL 9）且有需 root 的 TODO；经 sudo 而 `runuser` 不在 PATH；`--out` 不合法（校验先于任何动作）；`--plan` 记录读不了（文件在但不是 plan 写的 JSON）或指纹校验不过——setup 脚本的 sha256 / velox origin / HEAD 与 plan 记录不一致（信息 = "the setup script changed between plan and apply (<差异>) — rerun plan and review, then apply again"），或记录指纹的 checkout 没了而 apply 只能重新克隆（同款信息）。校验时机：apply 开始时（与其它拒绝一起、机器未动）与 source-deps 执行前（此前步骤已完成，`changes` 保留；信息同款，退出 2）。plan 时 velox 未克隆（deferred）→ 无指纹可校验，apply 照旧执行。

## 6. verify

依次调 env 家族五个检查（`toolchain_check.py`、`build_tools_check.py`、`cpp_deps_check.py`、`jdk_check.py`、`source_repos_check.py`，均 `--json`；source-repos 传 `--repos-dir`，并设 `GFV_REPOS_DIR`），取各结果里的 `*_result.success` / `missing` / `next_step`。兄弟检查的定位（相对本脚本目录）：指定了 `ENV_PREPARE_ENV_FAMILY_DIR` 就只认它下面的 `<检查 skill>/scripts/<脚本>`（没有就是 `missing`，别处不再找）；否则按 skill 名依次找源码树 `../../../flink-velox-env/<检查 skill>/scripts/<脚本>` → 安装后平铺 `../../<检查 skill>/scripts/<脚本>`（install.sh 把每个 skill 拷到 `<config>/skills/<skill>/`）→ 平铺且保留子域 `../../flink-velox-env/<检查 skill>/scripts/<脚本>`。找不到 → `missing`；跑了但没有 JSON → `fail`。全部 ok 退出 0；有 `fail` 或 `missing` 退出 1（`missing` 时人读报告是 `NOT VERIFIED`，不是通过）。五个检查中 jdk / source-repos 两项的 skill 随 !320 合入（另三项已随 !268 在仓）——!320 合入前的树上这两项必然 `missing`：detail 写 "…delivered by !320 (pending merge)"、item 带 `pending_pr` 字段，`next_step` 对 `missing` 给"装 skill 或等 !320 合入"、对 `fail` 给"修失败项"，两类并存时依次都给（兄弟检查缺失不算验证通过，这是既定纪律，`missing` 不是通过）。

## 7. --out

任何动作开始前先校验（项目目录内、非符号链接、已存在须 `--overwrite`、已存在的须是普通文件——管道文件会让写入无限等待读者），不合法 → 退出 2 且不做任何事；写入前再校验一次。

## 8. 来源

克隆表与 `pkg_for` 包名映射来自 GFVBot 安装器（`GFVBOT_REPOS`、`setup-openEuler.sh`）；source-deps 的清单解析、子 shell 驱动、`BUILD_THREADS=min(32, cores)` 来自其 `source-deps.sh`；探测判据与 env 家族的 cpp-deps-check 同一套。与它的差别：克隆先落独占临时目录再挪到位、已有的目录与依赖目录一律不删、经 sudo 时克隆以原用户执行、setup 脚本的来源与 sha256 留档。
