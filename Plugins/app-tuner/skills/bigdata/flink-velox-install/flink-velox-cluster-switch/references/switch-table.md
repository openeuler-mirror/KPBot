# 切换表与判据：native / GFV 切换并重启

脚本 `scripts/cluster_switch.py` 的探测判据、命令与退出语义。

## 0. 定位

| 项 | 判据 |
|---|---|
| Flink | `--flink-home` → `$FLINK_HOME` → PATH 上 `flink` 的真实路径上两级 → `/opt/flink` → `/usr/local/flink`（有 `bin/flink`） |
| REST | `$FLINK_REST_URL` → conf `rest.address` / `rest.port` → `http://localhost:8081`；`GET /overview`（忽略代理，3 秒超时）不应答视为"集群没在跑" |
| 本安装的守护进程 | `/proc/<pid>/cmdline` 主类为 JobManager / TaskManager 入口，且 classpath 里 `flink-dist*.jar` 所在目录真实路径 == 本安装 `lib/` 真实路径 |
| 在跑作业 | `/overview` 的 `jobs-running` |

## 1. jars

| 目标 | lib 里的 GFV jar（文件名匹配 `^(gluten|velox4j)`） | 备份目录 `<lib>.gfv-backup/` | 状态 |
|---|---|---|---|
| native | 有 | — | `todo`：`mkdir -p` 备份目录，逐个 `mv` 出去 |
| native | 无 | — | `skipped`（已是 native） |
| gfv | 无 | 无 | `blocked`：没有 GFV 集可切，fix 指向 stack-build |
| gfv | 任意 | 有 | `todo`：逐个 `mv` 回 lib，`rmdir` 备份目录（空时） |
| gfv | 有 | 无 | `skipped`（已是 GFV） |

运行依赖（guava / arrow / jackson …）不参与切换：native 下留着无害。需 root：lib 不可写。

## 2. stop

有本安装守护进程或 REST 应答 → `todo`：`bin/stop-cluster.sh`（120 秒超时，退出码记入 changes）→ 扫 /proc 的残留（TERM → 2 秒 → KILL；无权发信号记 `TERM failed` / `KILL failed` 继续）→ 轮询 REST 直到不应答（`--timeout`，默认 180 秒；超时判 `failed`："a daemon of another installation may own the port"）。集群本来没在跑 → `skipped`。

## 3. start

`bin/start-cluster.sh`（300 秒超时）→ 轮询 REST 直到 `taskmanagers >= 1`（`--timeout`）；超时或脚本非零 → `failed`，detail 附 `log/flink-*-taskexecutor-*.log` 最新一份的末 20 行（有则附）；成功 detail 记 REST 报的版本 / TM 数 / 在跑作业数。

## 4. apply 的拒绝条件（退出 2）

缺 `--yes`；缺 `--to`（argparse 用法错）；`jobs-running > 0`；任一步 `blocked`；有需 root 的 TODO 步骤而 `euid != 0`。

## 5. verify

调 `../flink-velox-env/flink-velox-cluster-check/scripts/cluster_check.py --json` 与 `…/flink-velox-lib-state-check/scripts/lib_state_check.py --json --expect <to>`，环境 `FLINK_HOME`、`ENV_CHECK_SYSROOT` / `ENV_CHECK_PROC_ROOT` 透传；脚本不存在 → `skipped`；无 JSON → `fail`。全部 ok 退出 0，否则 1。

## 6. 来源

"停 → 按 configDir 清残留 → 等端口 → 起 → 等 JM+TM 就绪（jps 轮询、超时给 taskexecutor 日志尾）"的流程与残留清理的理由（stop-cluster.sh 只认 pid 文件，丢失时旧 JM 仍占 6123，下一轮 start 报 BindException 而作业被交到旧集群）来自 GFV 的 `restart_flink_local.sh`；jar 切换 GFV 走 config.sh 的 `raw_gluten_jars`（extra-jars.txt），本 skill 改为在 lib 与备份目录之间挪 jar——与 lib-state-check 的判据（lib 里有无 gluten/velox jar）直接对应。
