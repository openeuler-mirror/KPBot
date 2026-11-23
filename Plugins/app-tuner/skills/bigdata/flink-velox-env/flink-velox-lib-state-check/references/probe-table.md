# 探测表与判据：lib 部署状态

脚本 `scripts/lib_state_check.py` 的判读依据。条目名即 `items[].name`（`home` / `lib` / `build` / `duplicates`）。

## 1. 判定构建

lib 里全部 `*.jar`（按名序）：

| 判定 | 条件 |
|---|---|
| native | 没有任何文件名匹配 `gluten|velox`（大小写不敏感）的 jar |
| gfv | `gluten-flink-planner-`、`gluten-flink-runtime-`、`gluten-flink-loader-` 各至少一个；`velox4j-` 至少一个；下表 14 个运行依赖各至少一个（按 `artifactId-` 前缀匹配，版本不计） |
| partial | 有 gluten/velox jar 但不满足 gfv；`missing_parts` = 缺的 gluten artifact + 缺的运行依赖，缺 velox4j 时排最前 |

运行依赖（14）：guava、failureaccess、arrow-c-data、arrow-format、arrow-memory-core、arrow-memory-unsafe、arrow-vector、commons-io、flatbuffers-java、jackson-annotations、jackson-core、jackson-databind、jackson-datatype-jdk8、jackson-datatype-jsr310。

`duplicates`：同一 artifact 多个版本——三个 gluten artifact 与 velox4j 各自多于一个 jar 时记入，列出全部 jar 名。

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| native 且 expect ∈ {any, native} | `build` ok | — |
| native 且 expect = gfv | `build` fail | `build velox4j and gluten-flink and deploy the complete set into lib, then restart the cluster (a restart needs the user consent)` |
| gfv 且 expect ∈ {any, gfv} | `build` ok：`complete GFV set: planner runtime loader + velox4j + 14 runtime deps` | — |
| gfv 且 expect = native | `build` fail | `move every gluten/velox jar out of lib and restart the cluster (a restart needs the user consent)` |
| partial（任何 expect） | `build` fail：点名 missing_parts 与 gluten/velox jar | `deploy the complete GFV build into lib, or remove every gluten/velox jar, then restart the cluster (a restart needs the user consent)` |
| 多版本 | `duplicates` warn | — |
| 找不到安装 | `home` fail | 安装 Flink 1.19.x 的提示 |
| 安装没有 lib/ | `lib` fail | 同上 |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表

## 3. next_step

按"找不到安装 → partial → 不符期望 → 多版本"取第一个成立的：
1. 找不到安装 / 没有 lib → `install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it, then rerun`
2. partial → `deploy the complete GFV build into lib (missing <parts>) or remove every gluten/velox jar, then restart the cluster (a restart needs the user consent), then rerun`
3. expect=native 而非 native → `move every gluten/velox jar out of lib and restart the cluster (a restart needs the user consent), then rerun`；expect=gfv 而非 gfv → `build velox4j and gluten-flink and deploy the complete set into lib, then restart the cluster (a restart needs the user consent), then rerun`
4. 多版本 → `remove the stale duplicate jars (<artifact: <jars>; …) and restart the cluster (a restart needs the user consent), then rerun`
5. 否则 `ready: lib holds the <build> build`（expect 不是 any 时附 ` as expected`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

"有 gluten/velox jar 即 GFV 构建"与 `flink-velox-e2e-verify` 的 lib 门同一判据；GFV 集合的组成（planner / runtime / loader + velox4j + 14 个运行依赖）来自 GFV 构建脚本拷进 `$FLINK_HOME/lib` 的清单（artifactId 不随版本变）。
