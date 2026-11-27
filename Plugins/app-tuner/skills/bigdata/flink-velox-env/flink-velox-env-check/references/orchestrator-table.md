# 统筹分组与路由表（orchestrator-table）

> `flink-velox-env-check` 的判据细则：goal 分组、委派契约、子检查定位、唯一 next_step 的路由。
> 事实来源：`scripts/env_check.py`（实现）。

## 1. goal 分组与六组结构

| 组（报告顺序） | 子检查（skill → 脚本） | goal=build | goal=native |
|---|---|---|---|
| build_env | flink-velox-toolchain-check → `toolchain_check.py`；flink-velox-build-tools-check → `build_tools_check.py`；flink-velox-cpp-deps-check → `cpp_deps_check.py` | 执行 | `disabled` |
| repos | flink-velox-source-repos-check → `source_repos_check.py` | 执行 | `disabled` |
| java | flink-velox-jdk-check → `jdk_check.py`（带 `--goal build\|native` 透传：build 要 17，native 8/11/17 均可） | 执行 | 执行（native 口径） |
| flink | flink-velox-flink-dist-check → `flink_dist_check.py` | 执行 | 执行 |
| lib | flink-velox-lib-state-check → `lib_state_check.py`（goal=build 不带 `--expect`，默认 any 只告知现状；goal=native 带 `--expect native`——原生目标跑在原生构建上，栈不匹配即 fail、工单回到切栈步骤） | 执行 | 执行（`--expect native`） |
| cluster | flink-velox-cluster-check → `cluster_check.py`；flink-velox-nexmark-check → `nexmark_check.py` | 执行 | 执行 |

- `disabled`：detail 固定 `goal=native does not need it`，不算 fail 也不算 missing，`success` 为 null
- goal=build 共 9 项；goal=native 跳过 build_env + repos 共 4 项、执行 5 项

## 2. 委派契约

| 项 | 约定 |
|---|---|
| 调用形态 | 子进程 `python3 <子脚本> --json`（解释器用运行本工具的 `sys.executable`），每项默认 120s 超时（`--timeout` 覆盖，须为正数） |
| 进程组 | 子检查在自有会话中运行（`start_new_session`）；超时对**整个进程组** SIGKILL（`os.killpg`）——其派生的 java/mvn/flink 孙进程一并收掉，不留孤儿 |
| 结果解析 | 子检查 stdout 的**首个** `*_result` 键 → 取 `success` / `items` / `missing` / `next_step`；退出码不参与判定（判据在 JSON 里） |
| 子检查状态映射 | `success: false` → `fail`；`success: true` 且 `status: warn` → `warn`；其余 → `ok`。warn 的 detail 转述子检查的 warn 条目（name + detail）；ok 的 detail 用子检查自己的 next_step（多为 ready: …）；fail 的 detail 用子检查的 error_message |
| 崩溃 / 超时 / 无输出 | 该项 `fail`：超时 detail 记 `did not finish within Ns`；起不来记异常；stdout 无可解析 JSON 记 `produced no result (exit N)`；next_step 给出**复用本次实际调用**的手跑命令（解释器 + 绝对路径 + 本次全部定位参数：argv 里的 `--repos-dir` / `--velox-dir` / `--goal` / `--expect`，env 前缀 `FLINK_HOME` / `NEXMARK_HOME` / `GFV_REPOS_DIR`）——复查落在同一套安装上 |
| 畸形结果 | 子检查打出**不是对象**的 `*_result` 值 → 工具中止（exit 2，`tool aborted:`）——统筹层不替畸形子结果编语义（与安装家族 verify 的先例一致） |
| 定位参数透传 | 见 §5 |

## 3. 子检查定位（U2 同款）

1. `ENV_PREPARE_ENV_FAMILY_DIR` 指向家族目录时权威：只查 `<family>/<check>/scripts/<script>`（tier-A 桩专用）
2. 否则查本 skill 目录旁的 `../../<check>/scripts/<script>`——源码树同子域平铺（`flink-velox-env/<check>/`）与安装后平布局（`<config>/skills/<check>/`）是同一相对路径，一个候选覆盖两种布局
3. **goal 需要的任一项定位失败 → 该项 `missing`，整体 `success=false` 退出 1**（U1 纪律：必需检查缺失≠成功）；人读报告打 `NOT VERIFIED: N check(s) not run (sibling skills not installed): …`

## 4. 唯一 next_step 路由

优先级 **build_env → flink → java → lib → cluster**（repos 与 build_env 同属构建准备层、紧随其后——两者的工单都指向 flink-velox-build-env-prepare）。取第一个有 fail / missing 的组：

| 组内情形 | next_step |
|---|---|
| 有 `missing` | `install the missing check skill(s) (<全轮所有缺失 skill>), then rerun python3 <abs>/env_check.py --goal <g> [定位参数]` |
| 无 missing、有 `fail` | 该组内**第一个 fail 项自己的 next_step 原文**（工单指向安装家族，见下表） |
| 全绿 | `goal <g> is ready: all checks pass` |

`warn` 不拦截（如实进 groups）；组内 missing 优先于 fail（没跑的检查先装上才有诊断）。

fail 工单的路由（子检查自己的 next_step，统筹层原文转述、不改写）：

| 检查 | 工单指向 |
|---|---|
| toolchain / build-tools / cpp-deps / source-repos / jdk | flink-velox-build-env-prepare |
| flink-dist / nexmark | flink-velox-runtime-install |
| lib-state | flink-velox-stack-build（lib 状态问题也可能是切栈工单：partial 补齐或清空由子检查语义定） |
| cluster | flink-velox-cluster-switch |

## 5. 定位参数透传

| 本工具参数 | 透传给 | 形态 |
|---|---|---|
| `--flink-home DIR` | flink-dist / jdk / cluster / lib-state / nexmark | 子进程 env `FLINK_HOME` |
| `--nexmark-home DIR` | nexmark | 子进程 env `NEXMARK_HOME` |
| `--repos-dir DIR` | source-repos | CLI `--repos-dir`（其自身约定） |
| `--repos-dir DIR` | cpp-deps | 子进程 env `GFV_REPOS_DIR`（它的 velox checkout 根）+ CLI `--velox-dir <repos-dir>/velox`（两个检查钉在同一 checkout，优先于任何 VELOX_HOME） |
| `--goal build\|native` | jdk | CLI `--goal`（其自身约定） |
| goal=native | lib-state | CLI `--expect native`（build 目标两侧都可，不传） |
| （未传） | 全部 | 留给各子检查自己的发现逻辑（FLINK_HOME / NEXMARK_HOME / GFV_REPOS_DIR 环境变量或 PATH / 默认目录） |

**显式路径即唯一目标**（家族 `!324` verify 的同款规则）：`--flink-home` 缺可运行的 bin/flink、`--nexmark-home` 缺可运行的 bin/run_query.sh → **先于任何子检查启动拒绝**，退出 2（stderr，无 JSON）——不给子检查回退 PATH / 默认目录、再凭另一套安装判 READY 的机会。`--repos-dir` 不做此校验：cpp-deps 对"无 checkout"有自己的 skip 语义（not probed 不算 fail），source-repos 的职责就是报告缺哪些仓。

## 6. 输出契约（键集恒定）

顶层键：`success` / `command`（恒 `plan`）/ `goal` / `checked_at` / `flink_home` / `nexmark_home` / `repos_dir`（显式传入值，未传为 null——不代答发现逻辑的结果）/ `timeout` / `groups` / `missing` / `next_step` / `error_message`。每个 group 恒 `name` + `checks`；每个 check 恒 `name` / `skill` / `status` / `success` / `detail` / `missing` / `next_step`。中止态（`tool aborted:`）输出同一套键，观测位（groups / missing）为空值。`missing[]` 是全家族缺失条目汇总（各执行子检查 missing 列表的串接）。

`--out` 护栏：realpath 限当前工作目录内、拒符号链接、已存在需 `--overwrite`、非普通文件（fifo 等）即使 `--overwrite` 也拒绝；**校验发生在任何子检查启动之前**，写入时再校验一遍。显式定位路径无效的拒绝（§5）同样先于任何子检查启动。
