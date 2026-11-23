# 准备表与判据：构建环境

脚本 `scripts/build_env_prepare.py` 的探测判据、命令与退出语义。

## 1. packages

| 项 | 值 |
|---|---|
| 探测 | `rpm -q <包>` 退出 0 = 已装；`rpm` 不在 PATH → 步骤 `blocked`（非 RPM 系统，openEuler 包名不适用） |
| 包（28，openEuler 名） | 工具链 `git cmake gcc gcc-c++ maven`；构建工具 `ninja-build ccache autoconf automake libtool flex bison python3 patchelf`；C++ 库 `libevent-devel openssl-devel re2-devel zstd-devel lz4-devel double-conversion-devel libdwarf-devel elfutils-libelf-devel libcurl-devel libicu-devel libsodium-devel zlib-devel librdkafka-devel`；`java-17-openjdk-devel` |
| 命令 | `dnf install -y <缺的包…>`（一次装齐；`dnf` 不在 PATH 且有缺包 → apply 拒绝） |
| 失败 | `dnf` 非零 → 步骤 `failed`，后续步骤不跑 |

包名与 env 家族 toolchain-check / build-tools-check / cpp-deps-check 的 `fix` 提示同表（g++ → gcc-c++、mvn → maven、ninja → ninja-build）。

## 2. repos

| 仓 | 上游 | 基线分支 | 标志文件 |
|---|---|---|---|
| velox | https://github.com/bigo-sg/velox.git | gluten-20260829 | `CMakeLists.txt`、`scripts/` |
| velox4j | https://github.com/bigo-sg/velox4j.git | gluten-20260829 | `pom.xml` |
| gluten | https://github.com/apache/gluten.git | main | `gluten-flink/pom.xml` |

- 目录存在且标志文件齐 → 跳过；存在但缺标志文件 → `blocked`（不自动删，fix：人工移走或修复）；不存在 → `git clone --quiet -b <基线> <上游> <dest>`，失败重试 3 次（间隔 `ENV_PREPARE_RETRY_SLEEP`，默认 5 秒），每次失败清掉半截目录；3 次都失败 → `failed`，后续仓不克隆
- `--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`；`git` 不在 PATH → `blocked`（由 packages 步骤装）

## 3. source-deps

| 项 | 值 |
|---|---|
| velox checkout | `$VELOX_HOME`（有 `scripts/`）→ `<repos-dir>/velox`；都没有：repos 步骤会克隆 → `todo` + `deferred`；否则 `blocked` |
| 脚本 | Ubuntu/Debian → `scripts/setup-ubuntu.sh`；其余 → `scripts/setup-centos9.sh`（CentOS 7 没有，发行版层已拒绝） |
| 清单 | 从 `function install_velox_deps {` 到第一个 `}` 之间的 `run_and_time install_<lib>` 行，去掉 `_from_dnf` / `_from_apt` / `conda`，保持顺序 |
| 已装探测 | `/usr/local/lib/cmake/<name|Capitalized|UPPER>` 或 `lib64/cmake/…` 目录、`/usr/local/include/<name>/`、`include/<name>.h` 或 `include/lib<name>.h`、探针二进制（protobuf → `bin/protoc`） |
| 命令 | 每个缺的库一个子 shell：`source <脚本> > /dev/null; set +x; install_<lib>()`，环境 `INSTALL_PREFIX=/usr/local`（sysroot 前缀）、`DEPENDENCY_DIR=$GFVBOT_SOURCE_DEPS_DIR|/tmp/gfvbot-source-deps`、`BUILD_THREADS=min(32, --jobs|核数)`、`PROMPT_ALWAYS_RESPOND=n` |
| 失败 | 非零即停（后面的库依赖前面的），`built` 列出已构建的、`missing` 是本次本该构建的全部 |

## 4. apply 的拒绝条件（退出 2）

缺 `--yes`；任一步 `blocked`；有需 root 的 TODO 步骤而 `euid != 0`（提示 `sudo python3 … apply --yes`）；发行版不受支持（非 openEuler / CentOS 9 / RHEL 9）且有需 root 的 TODO；packages 步骤 TODO 而 `dnf` 不在 PATH。

## 5. verify

依次调 `../flink-velox-env/flink-velox-{toolchain,build-tools,cpp-deps,jdk,source-repos}-check/scripts/*.py --json`（source-repos 传 `--repos-dir`，并设 `GFV_REPOS_DIR`），取各结果里的 `*_result.success` / `missing` / `next_step`；脚本不存在 → `skipped`；无 JSON → `fail`。全部 ok 退出 0，否则 1。

## 6. 来源

克隆表与 `pkg_for` 包名映射来自 GFV 安装器（`GFVBOT_REPOS`、`setup-openEuler.sh`）；source-deps 的清单解析、子 shell 驱动、空依赖目录清理、`BUILD_THREADS=min(32, cores)` 来自其 `source-deps.sh`；探测判据与 env 家族的 cpp-deps-check 同一套。
