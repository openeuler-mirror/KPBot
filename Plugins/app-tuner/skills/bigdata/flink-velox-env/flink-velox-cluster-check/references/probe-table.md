# 探测表与判据：集群状态

脚本 `scripts/cluster_check.py` 的判读依据。条目名即 `items[].name`（`home` / `processes` / `other-clusters` / `loaded-build` / `lib-vs-cluster` / `rest` / `jobs`；`rest` 可多条）。

## 1. 探测

| 条目 | 怎么探 |
|---|---|
| `home` | `FLINK_HOME` → PATH 上 `flink` 真实路径上两级 → `/opt/flink` → `/usr/local/flink`，首个有可执行 `bin/flink` 者 |
| 守护进程归属 | `/proc/<pid>/cmdline`（NUL 分隔）：主类匹配 `org.apache.flink.runtime.entrypoint.*Entrypoint` / `…taskexecutor.TaskManagerRunner`，且 `-classpath` 里 `flink-dist*.jar` 所在目录的真实路径 == 本安装 `lib/` 的真实路径；其它安装的只计入 `other_flink_daemons` |
| 进程起始时间 | `/proc/<pid>/stat` 字段 22（自启动的 tick 数）/ `SC_CLK_TCK` + `/proc/stat` 的 `btime` |
| 加载的构建 | 每个本安装守护进程 classpath 上文件名匹配 `gluten|velox` 的 jar：有 → gfv，无 → native；全部相同 → `loaded_build`，否则 `mixed`。只覆盖本机进程；REST 列出、不在本机跑的 TaskManager 记入 `remote_taskmanagers`，不参与判定 |
| lib 一致性 | **逐进程**：某进程的 `gfv_jars` 与 `lib_gfv_jars`（lib 里匹配 `gluten|velox` 的 jar）不同（差记入该进程的 `missing_vs_lib` / `extra_vs_lib`），或 lib 里任一 `*.jar` 的 mtime 晚于最早的进程起始时间 → 不一致。各进程的并集不作为判据（并集相等会漏掉"TaskManager 比 JobManager 少一个 jar"） |
| REST 是否本集群 | REST 报的 Flink 版本 == 本安装 flink-dist 版本（任一侧未知则视为是）；不是 → 它列出的 TaskManager 不用于判断本集群 |
| REST 地址 | `$FLINK_REST_URL` → conf `rest.address` / `rest.port` → `http://localhost:8081`。conf 只读 Flink 实际读的那份：`flink-conf.yaml` 存在就是它，否则 `config.yaml`（两份并存时 Flink 1.19 不读 `config.yaml`），不合并 |
| REST 概览 | `GET <rest>/overview`（`ProxyHandler({})` 忽略环境代理，3 秒超时）：`flink-version` / `taskmanagers` / `jobs-running` |

## 2. 判定

| 情形 | status | fix |
|---|---|---|
| 找不到安装 | `home` fail | 安装 Flink 1.19.x 的提示 |
| 都没在跑 | `processes` fail | `start the cluster (starting or restarting needs the user consent)` |
| 只有 JM，REST（本集群）列出 ≥ 1 个 TaskManager | `processes` warn：TaskManager 注册自别处，加载情况这里查不了（`remote_taskmanagers` 记数） | — |
| 只有 JM，REST 列不出 TaskManager（或不应答 / 不是本集群） | `processes` fail | `start the TaskManagers (or restart the cluster cleanly; a restart needs the user consent)` |
| 只有 TM | `processes` fail | `stop the leftover TaskManagers and restart the cluster cleanly (a restart needs the user consent)` |
| JM + TM | `processes` ok | — |
| REST 列出的 TM 多于本机在跑的 | `rest` warn：多出来的注册自别处 | — |
| 其它安装的守护进程 | `other-clusters` info | — |
| mixed | `loaded-build` fail | `restart the cluster as a whole (a restart needs the user consent)` |
| native / gfv 一致 | `loaded-build` ok | — |
| lib 改过 | `lib-vs-cluster` fail（逐进程点名加载了什么 / 缺什么，与晚于启动的 jar） | `restart the cluster so every daemon loads what is in lib now (a restart needs the user consent)` |
| lib 未改 | `lib-vs-cluster` ok | — |
| 在跑但 REST 不应答 | `rest` warn | — |
| REST 版本 ≠ 本安装 flink-dist 版本 | `rest` warn | — |
| REST 列出的 TM 少于本安装在跑的 TM | `rest` warn | — |
| 不在跑却应答 | `rest` warn（别的安装 / 容器 / 远端） | — |
| 不在跑也不应答 | `rest` info | — |
| 有作业在跑 | `jobs` warn（重启会打断） | — |

- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"条目名: detail"` 列表

## 3. next_step

按"找不到安装 → 不在跑 → 进程残缺 → mixed → lib 改过"取第一个成立的；其中会以重启收尾的分支，在 REST 报有作业在跑时一律换成 `N job(s) are running — let them finish (or stop them with the user's consent), then rerun; a restart now would interrupt them`（同时给建议重启的条目的 `fix` 附上"先等作业结束"）：
1. 找不到安装 → 安装 Flink 1.19.x 的提示 + `, then rerun`
2. 不在跑 / 进程残缺 → 对应 `processes` 条目的 fix + `, then rerun`
3. mixed → `restart the cluster as a whole so every daemon loads the same build (a restart needs the user consent), then rerun`
4. lib 改过 → `restart the cluster so every daemon loads what is in lib now (a restart needs the user consent), then rerun`
5. 否则 `ready: the cluster runs the <loaded_build> build that is in lib`（有 warn 附 ` (warnings above)`）

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

守护进程归属、加载构建判定、lib 与进程启动时间对比、REST 解析顺序均来自 GFV 环境自检的 cluster 分组；REST 请求忽略环境代理是因为本机 :8081 可能有另一套 Flink（容器共享宿主网络）且企业代理会改写 localhost。
