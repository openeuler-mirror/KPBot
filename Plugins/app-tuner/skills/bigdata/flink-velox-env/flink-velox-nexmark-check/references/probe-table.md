# 探测表与判据：nexmark 准备状态

脚本 `scripts/nexmark_check.py` 的判读依据。条目名即 `items[].name`（`flink` / `jar` / `home`）。

## 1. 探测

| 条目 | 怎么探 |
|---|---|
| `flink` | `FLINK_HOME` → PATH 上 `flink` 真实路径上两级 → `/opt/flink` → `/usr/local/flink`，首个有可执行 `bin/flink` 者 |
| `jar` | `<flink>/lib/*nexmark*.jar`（按文件名序）；第一个记为 `jar`，全部记为 `jars` |
| `home` | `$NEXMARK_HOME` → `/usr/local/nexmark` → `/opt/nexmark`，首个有可执行 `bin/run_query.sh` 者；`home_via` 记 `$NEXMARK_HOME` 或 `default`。找到后按清单核对：`bin/run_query.sh`、`bin/config.sh`、`conf/log4j.properties`、`conf/nexmark.yaml`、`conf/config.yaml`、`queries/ddl_gen.sql`、`queries/ddl_kafka.sql`、`queries/ddl_views.sql`、`queries/insert_kafka.sql`、至少一个 `queries/q*.sql`（`run_query.sh` 会 `.` 进 `bin/config.sh`，classpath 是 `$NEXMARK_HOME/lib/*:$FLINK_HOME/lib/*`）；缺项记入 `home_missing` |

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| 找到 Flink 安装 | — | — |
| 找不到 Flink 安装 | `flink` fail | 安装 Flink 1.19.x 的提示 |
| 有 nexmark jar | `jar` ok：`<jar> in lib` | — |
| 多个 nexmark jar | `jar` 另给一条 warn：`several nexmark jars in lib: … — the first in name order is used; remove the stale ones` | — |
| 没有 nexmark jar | `jar` fail：`no nexmark jar in <lib>` | `build nexmark-flink from https://github.com/nexmark/nexmark (mvn -pl nexmark-flink -am package -DskipTests) and copy the jar into lib` |
| 有可运行树且清单齐全 | `home` ok：`runnable tree at <路径> (via <来源>)` | — |
| 有可运行树但清单缺项 | `home` fail：`the nexmark tree at <路径> (via <来源>) is incomplete: missing <缺项> — run_query.sh needs them` | `copy the rest of nexmark-flink/src/main/resources over <路径> (existing files are kept), then rerun` |
| 没有可运行树 | `home` fail | `copy nexmark-flink/src/main/resources of the nexmark checkout as the runnable tree and point NEXMARK_HOME at it` |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表

## 3. next_step

1. 找不到 Flink → `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it, then rerun`
2. 缺 jar / 缺树 / 树不完整（一次列全）→ `nexmark is not ready: build the nexmark-flink jar (mvn -pl nexmark-flink -am package -DskipTests) and copy it into lib and lay out the runnable nexmark tree (nexmark-flink/src/main/resources) and point NEXMARK_HOME at it, then rerun`；树不完整时缺树那一段换成 `complete the nexmark tree at <路径> (missing <缺项>)`
3. 都在 → `ready: nexmark jar and runnable tree are in place`（有 warn 附 `; warnings above`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

nexmark 的两件（jar 进 Flink lib、可运行树 = `nexmark-flink/src/main/resources`）与构建命令（`mvn -pl nexmark-flink -am package`）来自 GFV 安装器的 nexmark 构建步骤；可运行树的三个候选位置与安装器落盘位置一致。
