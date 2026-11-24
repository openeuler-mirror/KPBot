# 切换表与判据：native / GlutenFlink 切换并重启

脚本 `scripts/cluster_switch.py` 的探测判据、命令与退出语义。

## 0. 定位

| 项 | 判据 |
|---|---|
| Flink | `--flink-home` 给了就是唯一候选：须同时有可执行的 `bin/flink`、`bin/start-cluster.sh`、`bin/stop-cluster.sh`，否则拒绝（退出 2），不回落搜索；没给时依次找 `$FLINK_HOME` → PATH 上 `flink` 的真实路径上两级 → `/opt/flink` → `/usr/local/flink`（同样三个脚本齐全才算） |
| 配置文件 | conf 目录里 `flink-conf.yaml` 存在就只读它，否则只读 `config.yaml`（Flink 1.19 两份并存时只读 `flink-conf.yaml`、不读 `config.yaml`——1.19.2 的 `GlobalConfiguration.loadConfiguration` 字节码实证）；不合并两份；结果里 `conf_file` 记录读的是哪份 |
| REST | `$FLINK_REST_URL` → 配置里的 `rest.address` / `rest.port` → `http://localhost:8081`；`GET /overview`（忽略代理，3 秒超时）不应答视为"集群没在跑"；地址须在本机（回环、未指定、或本机能绑定的地址），否则拒绝 |
| 本安装的守护进程 | `/proc/<pid>/cmdline` 主类为 JobManager / TaskManager 入口，且 classpath 里 `flink-dist*.jar` 所在目录真实路径 == 本安装 `lib/` 真实路径 |
| 在跑作业 | `/overview` 的 `jobs-running`：只有明确的整数才算已知；本安装有守护进程而没有这个整数 → 作业数未知 |

## 1. jars

完整 GlutenFlink 集 = `gluten-flink-planner` / `gluten-flink-loader` / `gluten-flink-runtime` / `velox4j` 各至少一个 jar（按文件名前缀匹配，版本不写死），**加上 stack-build 的 runtime-deps 步骤一并部署的 14 个运行依赖**（guava、failureaccess、arrow 系列、commons-io、flatbuffers-java、jackson 系列——同一份清单、同一前缀规则，与 stack-build 的判定一致）：缺运行依赖的集在类加载中途失败，正是 stack-build 明确"do NOT restart"的状态，`--to gfv` 一样不放行。挪第一个之前先检查全部目标：lib 与备份目录里有同名 jar 一律 BLOCKED，不覆盖。

| 目标 | lib 里的 GlutenFlink jar | 备份目录 `<lib>.gfv-backup/` | 状态 |
|---|---|---|---|
| native | 无 | — | `skipped`（已是 native） |
| native | 有 | 有与 lib 同名的 | `blocked`：挪过去会覆盖备份里的旧 jar，fix 提示手工比较新旧 |
| native | 有 | 无同名 | `todo`：`mkdir -p` 备份目录，逐个 `mv` 出去 |
| gfv | 完整（组件 + 运行依赖都齐） | 任意 | `skipped`（已是 GlutenFlink；备份里剩下的旧 jar 不动，detail 注明） |
| gfv | 不完整 | 有与 lib 同名的 | `blocked`：挪回去会覆盖 lib 里的同名 jar |
| gfv | 无 | 无 | `blocked`：没有 GlutenFlink 集可切，fix 指向 stack-build（或改切 native） |
| gfv | 不完整 | 无 | `blocked`：集不齐且备份为空，列出缺的组件与运行依赖（`missing` / `missing_deps`） |
| gfv | 不完整 | 有，但补齐后仍有缺 | `blocked`：列出仍缺的组件与运行依赖 |
| gfv | 不完整 | 能补齐（缺的组件 / 依赖都可以从备份回挪） | `todo`：逐个 `mv` 回 lib，`rmdir` 备份目录（空时） |

运行依赖（guava / arrow / jackson …）不参与 `--to native` 方向的挪动：native 下留着无害；但 `--to gfv` 的完备性门槛要求它们齐（lib 已有或从备份补回都行），否则不重启。需 root：lib 不可写。

## 2. stop

有本安装守护进程或 REST 应答 → `todo`：`bin/stop-cluster.sh`（120 秒超时，退出码记入 changes）→ 扫 /proc 的残留（TERM → 2 秒 → KILL；无权发信号记 `TERM failed` / `KILL failed` 继续）→ 停之前 REST 应答过才轮询它直到不应答（`--timeout`，默认 180 秒；超时判 `failed`："a daemon of another installation may own the port"）。集群本来没在跑 → `skipped`。`ENV_PREPARE_PROC_ROOT` 指向假 /proc 时不发信号，changes 里记 `sent TERM (sandboxed proc root: not sent)`。

## 3. start

`bin/start-cluster.sh`（300 秒超时）→ 轮询 REST 直到 `taskmanagers >= 1`（`--timeout`）；超时或脚本非零 → `failed`，detail 附 `log/flink-*-taskexecutor-*.log` 最新一份的末 20 行（有则附）；成功 detail 记 REST 报的版本 / TM 数 / 在跑作业数。

## 4. 异常

步骤里的命令无法执行、超时、挪 jar 出错，都算**该步失败**（退出 1，detail 记异常类型与信息），不是工具崩溃；之前步骤与当前步骤已完成的条目都留在 `changes` 里。`tool aborted:`（退出 2）只用于步骤之外的工具错误。

## 5. apply 的拒绝条件（退出 2）

缺 `--yes`；缺 `--to`（argparse 用法错）；`jobs-running > 0`；作业数未知（本安装有守护进程而 REST 没给出整数）；REST 不在本机；`--flink-home` 不是 Flink 安装；任一步 `blocked`；有需 root 的 TODO 步骤而 `euid != 0`；`--out` 不合法（校验先于任何动作）。提示里的 sudo 命令与 `next_step` 里的命令一样是完整的（`sudo python3 <脚本绝对路径> …`，照抄即可）并带着本次的 `--flink-home` / `--timeout`。

## 6. verify

调 cluster-check（`cluster_check.py --json`）与 lib-state-check（`lib_state_check.py --json --expect <to>`），环境 `FLINK_HOME`、`ENV_CHECK_SYSROOT` / `ENV_CHECK_PROC_ROOT` 透传。兄弟检查的定位（相对本脚本目录）：指定了 `ENV_PREPARE_ENV_FAMILY_DIR` 就只认它下面的 `<检查 skill>/scripts/<脚本>`（没有就是 `missing`，别处不再找）；否则按 skill 名依次找源码树 `../../../flink-velox-env/<检查 skill>/scripts/<脚本>` → 安装后平铺 `../../<检查 skill>/scripts/<脚本>`（install.sh 把每个 skill 拷到 `<config>/skills/<skill>/`）→ 平铺且保留子域 `../../flink-velox-env/<检查 skill>/scripts/<脚本>`。找不到 → `missing`；跑了但没有 JSON → `fail`。全部 ok 退出 0；有 `fail` 或 `missing` 退出 1（`missing` 时人读报告是 `NOT VERIFIED`，不是通过）。

## 7. --out

任何动作开始前先校验（项目目录内、非符号链接、已存在须 `--overwrite`、已存在的须是普通文件——管道文件会让写入无限等待读者），不合法 → 退出 2 且不做任何事；写入前再校验一次。

## 8. 来源

"停 → 按 configDir 清残留 → 等端口 → 起 → 等 JM+TM 就绪（jps 轮询、超时给 taskexecutor 日志尾）"的流程与残留清理的理由（stop-cluster.sh 只认 pid 文件，丢失时旧 JM 仍占 6123，下一轮 start 报 BindException 而作业被交到旧集群）来自 上游 e2e 测试脚本的 `restart_flink_local.sh`；jar 切换 GlutenFlink 走 config.sh 的 `raw_gluten_jars`（extra-jars.txt），本 skill 改为在 lib 与备份目录之间挪 jar——与 lib-state-check 的判据（lib 里有无 gluten/velox jar）直接对应。
