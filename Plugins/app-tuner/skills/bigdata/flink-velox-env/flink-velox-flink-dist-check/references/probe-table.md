# 探测表与判据：Flink 发行版

脚本 `scripts/flink_dist_check.py` 的判读依据。条目名即 `items[].name`。

## 1. 探测

| 条目 | 怎么探 |
|---|---|
| `home` | `FLINK_HOME` 设了即唯一检查目标，按原样判定（不回退）；未设置时 PATH 上 `flink` 的真实路径上两级 → `/opt/flink` → `/usr/local/flink`，首个有可执行 `bin/flink` 者 |
| `files` | `bin/flink`、`bin/start-cluster.sh`、`bin/stop-cluster.sh`、`bin/sql-client.sh` 可执行；`lib/` 下（含子目录）`flink-dist*.jar` 至少一个 |
| `dist-jars` | `lib/` 下**递归**（`os.walk`）找到的全部 `flink-dist*.jar`，路径相对 `lib/`（顶层的在前，子目录里的按路径序）；点前缀子目录（如 `lib/.backup/`）同样进入——与 `find` 同范围，递归 glob 进不去 |
| `version` | `lib/` 顶层的第一个 dist jar（顶层没有才取子目录里路径序第一个）：文件名里首个 `-\d+\.\d+\.\d+`；没有则读 jar 内 `META-INF/MANIFEST.MF` 的 `Implementation-Version`（真实 zip） |
| `conf` | 只读 Flink 实际读的那一份：`flink-conf.yaml` 存在就是它（扁平点分键），否则 `config.yaml`（嵌套键，按缩进展平为 `rest.address` 这类点分键）；两份都不在为 warn；读的是哪份记在 `flink.conf_file` |
| REST 地址 | `$FLINK_REST_URL`（无 scheme 补 `http://`，去尾 `/`）→ conf `rest.address` / `rest.port` → `http://localhost:8081` |

- 版本线只比 major.minor：`(1, 19)`；1.19.2 与 1.19.3 都算在版本线上（用户裁定：patch 不钉死）

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| 安装找到 | `home` ok：`Flink <版本或 ?> at <路径> (via <来源>; version from <来源>)` | — |
| 安装找不到 | `home` fail | `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it` |
| 显式 `FLINK_HOME` 无效（目录不存在或没有可执行 `bin/flink`） | `home` fail：`FLINK_HOME is set to <路径>, which <does not exist / has no executable bin/flink> — it is the installation to check, so a flink on PATH and the default locations were not looked at`；`path`/`found_via` 落在该确切路径 | 同"安装找不到"的提示 |
| stock 脚本或 dist jar 缺失 | `files` fail（点名缺的文件） | `unpack the distribution again, or point FLINK_HOME at a complete one` |
| dist jar 多于一个 | `dist-jars` fail（按相对 `lib/` 的路径列出；版本取顶层第一个） | `move every flink-dist jar but the installed version out of lib/ (e.g. to <lib>.bak/), then restart the cluster (a restart needs the user consent)` |
| 版本读不出 | `version` fail | 同"安装找不到"的提示 |
| 不在 1.19 线 | `version` fail：`Flink <版本> is not on the 1.19 line — …` | 同上 |
| conf 文件都不在 | `conf` warn（不阻断） | — |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表；`on_1_19_line` 在版本 fail 时为 null

## 3. next_step

按"装 → 补文件 → 去多余 jar（需重启）→ 换版本线"取第一个成立的：
1. 无安装或显式 `FLINK_HOME` 无效 → `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it, then rerun`
2. 文件缺失 → `unpack the Flink distribution again (or point FLINK_HOME at a complete one), then rerun`
3. 多个 dist jar → `keep only the flink-dist jar of the installed version in lib, then restart the cluster (a restart needs the user consent), then rerun`
4. 版本读不出 → `reinstall the Flink 1.19.x runtime (its flink-dist jar carries no readable version), then rerun`；不在 1.19 线 → `install the Flink 1.19.x runtime instead (this installation is <版本>), then rerun`
5. 否则 `ready: Flink <版本> at <路径>`（有 warn 时附 `(warnings above)`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

- 多个 `flink-dist*.jar` 判 fail 的依据：Flink 1.19.2 发行版 `bin/config.sh` 的 `constructFlinkClassPath` 对 `find lib -name '*.jar'` 的结果匹配 `flink-dist[^/]*\.jar`，多于一个即打印 `Multiple flink-dist*.jar found` 并 `exit 1`；`bin/bash-java-utils.sh` 同样用 `find "$LIB_DIR" -name 'flink-dist*.jar'` 递归查找、多于一个报同样的错（均为 1.19.2 真机实测）——所以留在子目录（如 `lib/backup/`）里的旧 dist jar 一样会让集群起不来
- `lib/` 扫描用 `os.walk` 而非递归 glob：`find` 会进点前缀目录，而 Python 的 `**` 递归 glob 不会（3.11 实测：`lib/.backup/flink-dist-1.18.0.jar` + `lib/flink-dist-1.19.2.jar` 并存时 glob 只数到 1 个判 ok）——扫描范围必须与启动脚本的 `find` 一致
- `FLINK_HOME` 设了即唯一检查目标：家族对显式定位符的统一口径（`flink-velox-jdk-check` 对显式 `JAVA_HOME`、`flink-velox-source-repos-check` 对显式 `--repos-dir`、`flink-velox-stack-build` 的 verify 对显式 `--flink-home` 同此）；空 / 未设置才走 PATH 与默认路径——`flink-velox-stack-build` 的 verify 对显式无效的 `--flink-home` 宁可 refused，也不把它作为空 `FLINK_HOME` 交给本家族检查（否则检查回退，可能把另一台安装判成 VERIFIED）
- 安装定位顺序与 REST 解析顺序与 GFVBot 环境自检的 flink 分组一致；REST 只解析不访问
