# 探测表与判据：Flink 发行版

脚本 `scripts/flink_dist_check.py` 的判读依据。条目名即 `items[].name`。

## 1. 探测

| 条目 | 怎么探 |
|---|---|
| `home` | `FLINK_HOME` → PATH 上 `flink` 的真实路径上两级 → `/opt/flink` → `/usr/local/flink`，首个有可执行 `bin/flink` 者 |
| `files` | `bin/flink`、`bin/start-cluster.sh`、`bin/stop-cluster.sh`、`bin/sql-client.sh` 可执行；`lib/flink-dist*.jar` 至少一个 |
| `dist-jars` | `lib/flink-dist*.jar` 按文件名序全部列出 |
| `version` | 文件名序第一个 dist jar：文件名里首个 `-\d+\.\d+\.\d+`；没有则读 jar 内 `META-INF/MANIFEST.MF` 的 `Implementation-Version`（真实 zip） |
| `conf` | `conf/config.yaml`（嵌套键，按缩进展平为 `rest.address` 这类点分键）或 `conf/flink-conf.yaml`（扁平点分键）任一存在 |
| REST 地址 | `$FLINK_REST_URL`（无 scheme 补 `http://`，去尾 `/`）→ conf `rest.address` / `rest.port` → `http://localhost:8081` |

- 版本线只比 major.minor：`(1, 19)`；1.19.2 与 1.19.3 都算在版本线上（用户裁定：patch 不钉死）

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| 安装找到 | `home` ok：`Flink <版本或 ?> at <路径> (via <来源>; version from <来源>)` | — |
| 安装找不到 | `home` fail | `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it` |
| stock 脚本或 dist jar 缺失 | `files` fail（点名缺的文件） | `unpack the distribution again, or point FLINK_HOME at a complete one` |
| dist jar 多于一个 | `dist-jars` fail（按名序列出；版本取第一个） | `keep only the flink-dist jar of the installed version in lib, then restart the cluster (a restart needs the user consent)` |
| 版本读不出 | `version` fail | 同"安装找不到"的提示 |
| 不在 1.19 线 | `version` fail：`Flink <版本> is not on the 1.19 line — …` | 同上 |
| conf 文件都不在 | `conf` warn（不阻断） | — |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表；`on_1_19_line` 在版本 fail 时为 null

## 3. next_step

按"装 → 补文件 → 去多余 jar（需重启）→ 换版本线"取第一个成立的：
1. 无安装 → `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it, then rerun`
2. 文件缺失 → `unpack the Flink distribution again (or point FLINK_HOME at a complete one), then rerun`
3. 多个 dist jar → `keep only the flink-dist jar of the installed version in lib, then restart the cluster (a restart needs the user consent), then rerun`
4. 版本读不出 → `reinstall the Flink 1.19.x runtime (its flink-dist jar carries no readable version), then rerun`；不在 1.19 线 → `install the Flink 1.19.x runtime instead (this installation is <版本>), then rerun`
5. 否则 `ready: Flink <版本> at <路径>`（有 warn 时附 `(warnings above)`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

- 多个 `flink-dist*.jar` 判 fail 的依据：Flink 1.19.2 发行版 `bin/config.sh` 的 `constructFlinkClassPath` 对 `find lib -name '*.jar'` 的结果匹配 `flink-dist[^/]*\.jar`，多于一个即打印 `Multiple flink-dist*.jar found` 并 `exit 1`（真机实测）
- 安装定位顺序与 REST 解析顺序与 GFV 环境自检的 flink 分组一致；REST 只解析不访问
