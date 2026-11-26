# 1:n 内存超分

用运行 AgentENV microVM 的宿主机容量 `t` 和每沙箱 4 GiB 计算名义超分。`n` 为正整数。

- 默认探测宿主机 `dmidecode --type 17` 的已安装容量。访问不到完整的 SMBIOS 容量时，脚本向 stderr 提示并退回到 `/proc/meminfo` 的 `MemTotal`。`--memory-source memtotal` 可直接选用内核口径。
- `N = floor(t / 4)` 是 1:1 基数，`k = floor(t × n / 4)` 是 1:n 沙箱数。`t` 用 GiB 表达，脚本用整数 KiB 计算，每档最后向下取整一次。
- 以安装容量 1536 GiB 为例，`N=384`，1:2、1:3、1:6、1:10 分别是 768、1152、2304、3840。若取内核读数 `MemTotal=1578474656 KiB`，`N=376`，1:3 应生成 1129 个。
- 仓库 `oversub-bench --running-concurrency N` 用 `n × N`。1536 GiB 时两个算法相等。上例若用内核口径，1:3 原生工具会生成 1128 个。按公式准确放量时，用本技能生成每档 YAML。
- 记录使用的容量来源、主机实际在线内存、`MemAvailable` 和服务资源上限。`4 × k` GiB 是名义分配，实际驻留与 OOM 行为取决于客户机用量、回收、其它进程和 cgroup 限制。

## 探测与预览

在 AgentENV 宿主机运行脚本，或从本地把脚本送入已授权的 SSH 会话执行。计算本身仅使用 Python 标准库。

```bash
# 从已安装的 agent-benchmark skill 目录运行。
SKILL_DIR=$(pwd -P)

# BENCH_HOST 是 AgentENV 宿主机的 SSH 目标。
ssh "$BENCH_HOST" 'python3 - 1 2 3 4 6 8 10' \
    < "$SKILL_DIR/scripts/plan_oversub.py"

# 用确认的名义容量做离线规划。
python3 "$SKILL_DIR/scripts/plan_oversub.py" --mem-total-gib 1536 1 2 3

# 用已有的内核读数做离线规划。
python3 "$SKILL_DIR/scripts/plan_oversub.py" --mem-total-kib 1578474656 1 2 3
```

## 精确生成每档配置

按 [replay.md](replay.md) 准备 `RUN_DIR/lifecycle-base.yaml` 和私有 `E2B_CONFIG`，复用目标服务已就绪的映射模板。默认用例为仓库内置 55 条，也可沿用自选清单或目录生成的配置。每档的数量只取决于 Host 内存和比例。若 1:1 的槽数少于模板数，一轮只能执行部分模板，按轨迹索引报告覆盖。

把技能脚本放到 driver 可读路径。driver 跟 AgentENV 主机分开时，先探测宿主机，将计划的 `memory_kib` 用 `--mem-total-kib` 传给 driver，并保留原始探测来源。可从本地执行 `scp "$SKILL_DIR/scripts/plan_oversub.py" "$BENCH_DRIVER:$REMOTE_RUN_DIR/plan_oversub.py"`，其中 `BENCH_DRIVER` 和 `REMOTE_RUN_DIR` 分别是 driver 的 SSH 目标和它已创建的运行目录。

```bash
cd "${BENCH_ROOT:?set BENCH_ROOT to the verified checkout}"
source .venv/bin/activate
PLANNER="$SKILL_DIR/scripts/plan_oversub.py"
SWEEP_DIR="$RUN_DIR/oversub-exact"

python "$PLANNER" --mem-total-kib "${HOST_MEMORY_KIB:?use the AgentENV host reading}" \
    --base-config "$RUN_DIR/lifecycle-base.yaml" \
    --output-dir "$SWEEP_DIR" 1 2 3
```

其中 `HOST_MEMORY_KIB` 取自 Host 探测输出的 `memory_kib`，另存该输出以保留容量来源。driver 与 Host 同机时也可省略 `--mem-total-kib` 直接探测。单独传输脚本时把 `PLANNER` 改为 driver 上的实际路径。脚本写 `plan.json` 和每档的 `1-to-<n>/trial.yaml`，在调用方选定的真实父目录内使用尚不存在的目录名。已有文件、目录或同名符号链接会使脚本以非零状态退出。每档的沙箱 ID 清单和结果各自独立；基准配置通过私有 `E2B_CONFIG` 提供凭据。

默认 `lifecycle` 场景为 `sandbox.total_count=k`、`test.round_size=k`、`replay.running_concurrency=k`。沙箱创建后按每步 resume → exec → pause。整个 fleet 在一组内，运行槽上限为 k，实际活跃数受轨迹、创建速度和服务端限制。使用准备脚本生成的配置时，各档引用同一份轨迹快照和模板映射，保留完整用例池。直接传入已有 YAML 时沿用其显式 `trajectory_limit`。无模板映射的旧式配置默认沿用基线数量上限。常规一轮 `round_count=1`，持续窗口使用 round-robin 的 `round_count=0`，`test.duration` 是压测阶段的时间上限，预创建和清理会增加整档墙钟时间。

若用户要做轮转实验，可显式将 `lifecycle` 的 `replay.running_concurrency` 改成 `N`，保留 `total_count=round_size=k`，并在结果里注明运行槽上限。`exec_only` 用长寿命沙箱、不 pause/resume。`exec_ephemeral` 每条创建和清理沙箱，`running_concurrency` 在这两种 exec 模式下用于计算报告及轨迹池，运行不受它限槽。测 `exec_ephemeral` 时可用 `full.yaml` 作 base，保留 `fixed + multiplier=1` 实现每槽一次，使用 `launch_interval_sec` 控制创建速度时记录实际值。

## 运行各档

1. 按指定的比例和顺序直接运行对应配置。需要比较 1:1 基线时，可复用同口径的现有结果。
2. 每档使用对应 YAML。从 driver 不在宿主机的场景启动时添加 `--no-vm-monitor`，在 AgentENV 主机用同一时间窗口采集。
3. 固定模板、工具调用轨迹池、`delay_scale`、资源规格、服务配置和采集口径。每组记录 `round_count` 或 `multiplier` 和时间窗。需要重复时给每次重复独立目录。
4. OOM、不可中断任务积累、create/resume 超时或存储无法回收时，暂停继续放量。诊断 `MemAvailable`、memory PSI、OOM、`procs_blocked`、I/O 和 AgentENV 日志。结束任务后只处理本次确认拥有的实例。

```bash
n=1  # 改为本次指定的档位
set -o pipefail
bench-core --provider aenv --config "$SWEEP_DIR/1-to-$n/trial.yaml" \
    2>&1 | tee "$SWEEP_DIR/1-to-$n/bench.log"
```

## 使用仓库原生扫描

当每档 `sandbox_count` 都等于 `native_count`，且采用原生并发语义时，可直接用原生工具扫描并汇总。原生工具会设置 `trajectory_limit=N`，在多模板或小内存下可能截短用例池。要完整保留自选集合时使用上面的逐档 YAML。仅需预览 `trial.yaml` 和命令时加 `--dry-run`，使用单独的输出目录。原生 exec 模式有分片选项 `--shards`，应在跨档对比中记录。共享 endpoint 显式使用 `--cleanup-between-trials off`，避免原生默认的 pre-trial 清理影响其它任务。

```bash
N=384  # 替换为 plan.json 的 baseline_count
oversub-bench --provider aenv --config "$RUN_DIR/lifecycle-base.yaml" \
    --running-concurrency "$N" --ratios 1,2,3 --modes lifecycle \
    --multiplier 0 --repeats 1 --test-duration 3600 \
    --cleanup-between-trials off --stop-on-failure \
    --output-root "$RUN_DIR/oversub-native"
```

## 读取结果

- 以 `plan.json` 的容量来源、`n` 和沙箱数标注每档。直接调用 `bench-core` 时，summary 的 `overcommit_ratio` 是 `total_count/running_concurrency`，全量 `lifecycle` 往往显示 `1.0`。硬件内存超分比来自计划。
- 每档比较创建成功数、轨迹尝试/完整走完/中断数、步骤错误、`wall_sec`、轨迹吞吐、resume/pause 延迟和内存峰值。用 `trajectories/index.json` 与日志核对覆盖范围和失败原因。
- `oversub-bench` 自动生成 `trial-summary.csv`、`ratio-summary.csv` 等汇总。逐档直接跑 `bench-core` 时从每档 `*_run_summary.json`、`trajectories/index.json` 和主机采集汇总。
- 标明真实已完成、超时截断、进程中断与无法核验的档位。主机重启后先看 boot ID 和日志，再决定是否续跑所缺档位。

仓库原生参数和 CSV 定义见 `docs/guide/oversub-bench-usage-zh.md`。
