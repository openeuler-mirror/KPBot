# 安装表与判据：Flink 运行时与 nexmark

脚本 `scripts/runtime_install.py` 的探测判据、命令与退出语义。

## 1. flink

| 项 | 值 |
|---|---|
| 版本 | `--version`（默认 1.19.2），必须匹配 `1.19.x`，否则 plan / apply 退出 2 |
| 目录 | `<prefix>/flink-<版本>/`（默认 `/opt`）；软链 `<prefix>/flink` → 该目录 |
| 探测 | `bin/flink` 可执行 = 已装；软链 `realpath` == 版本目录 = 已指对；`conf/flink-conf.yaml` 存在且没有任何非注释非空行 = 需补最小配置 |
| 下载 | `curl -fsSL --retry 3 --max-time 3600 -o <tmp>/<tgz> <mirror>/flink-<版本>/<tgz>`，失败再 `<archive>/flink-<版本>/<tgz>`；镜像默认 `https://mirrors.huaweicloud.com/apache/flink`，归档恒为 `https://archive.apache.org/dist/flink` |
| 校验 | `curl -fsSL --retry 3 --max-time 120 -o <tmp>/<tgz>.sha512 <archive>/flink-<版本>/<tgz>.sha512`（**只从归档取**，镜像不带校验文件）→ `(cd <tmp> && sha512sum -c <tgz>.sha512)` |
| 解压 | `tar -xzf <tmp>/<tgz> -C <prefix>`，之后核对 `bin/flink` 可执行 |
| 软链 | `ln -sfn <dist> <prefix>/flink`；若 `<prefix>/flink` 已是真实目录 → 失败、不覆盖 |
| 最小配置 | 追加 11 个键：`jobmanager.rpc.address: localhost`、`jobmanager.rpc.port: 6123`、`jobmanager.bind-host: localhost`、`taskmanager.bind-host: 0.0.0.0`、`taskmanager.host: localhost`、`taskmanager.data.bind-host: 0.0.0.0`、`rest.bind-address: localhost`、`client.address: localhost`、`jobmanager.memory.process.size: 2048m`、`taskmanager.memory.process.size: 8192m`、`taskmanager.numberOfTaskSlots: 8`——只在文件全注释时 |
| 需 root | `<prefix>`（或其最近存在的祖先）对当前用户不可写 |

## 2. nexmark-jar

| 项 | 值 |
|---|---|
| 探测 | `<prefix>/flink/lib/*nexmark*.jar` 存在 → 跳过 |
| 前置 | `mvn` 在 PATH；JDK：`JAVA_HOME` → `/usr/lib/jvm/java-17-*` → `/usr/lib/jvm/java-1.8.0-openjdk-*`（须有 `bin/javac`）；缺一则 `blocked` |
| 源码 | `<repos-dir>/nexmark`（`--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`）；无 `.git` → `git clone --depth 1 https://github.com/nexmark/nexmark.git <src>`，失败重试 3 次（间隔 `ENV_PREPARE_RETRY_SLEEP`，默认 5 秒）并清半截目录 |
| 构建 | 在源码目录 `JAVA_HOME=<jdk> mvn -pl nexmark-flink -am package -DskipTests`（14400 秒超时） |
| 部署 | `nexmark-flink/target/nexmark-flink-*.jar` 中排除 `-sources` 的第一个（名序）拷进 Flink lib |
| 需 root | Flink lib 不可写 |

## 3. nexmark-tree

| 项 | 值 |
|---|---|
| 探测 | `<nexmark-home>/bin/run_query.sh` 可执行 → 跳过 |
| 前置 | nexmark-jar 步骤不 blocked；jar 已部署但没有源码 `nexmark-flink/src/main/resources` → `blocked`，fix 给 clone 命令 |
| 命令 | `mkdir -p <home>`；`cp -R <res>/{bin,conf,queries,queries-cep} <home>/`（四个目录缺任一 → 失败）；`mkdir -p <home>/{lib,log,data}`；`cp <flink-lib>/nexmark-flink-*.jar <home>/lib/` |
| 需 root | `<nexmark-home>` 不可写 |

## 4. apply 的拒绝条件（退出 2）

缺 `--yes`；版本不在 1.19 线；任一步 `blocked`（信息带该步 detail 与 fix）；有需 root 的 TODO 步骤而 `euid != 0`（提示 `sudo python3 runtime_install.py apply --yes`）。

## 5. verify

依次调 `../flink-velox-env/flink-velox-flink-dist-check/scripts/flink_dist_check.py --json` 与 `…/flink-velox-nexmark-check/scripts/nexmark_check.py --json`，环境 `FLINK_HOME=<prefix>/flink`、`NEXMARK_HOME=<nexmark-home>`（`--no-nexmark` 时 nexmark 记 skipped）；取各结果 `*_result.success` / `missing` / `next_step`；脚本不存在 → `skipped`；无 JSON → `fail`。全部 ok 退出 0，否则 1。

## 6. 来源

Flink 版本、目录布局（`/opt/flink-<版本>` + `/opt/flink` 软链）、镜像与校验来源（华为云取包、sha512 恒取 archive.apache.org）、11 个最小配置键、nexmark 的克隆 / 构建 / 部署 / 可运行树铺法均来自 GFV 安装器的 `install_flink` / `ensure_flink_conf` / `install_nexmark`。
