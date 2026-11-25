---
name: sandbox-bench-topdown
description: 解决 AgentENV 官方 benchmark 单项时延过短、无法采集 top-down 比例分析的问题（与 sandbox-bench-flame 同思路，采集工具换为鲲鹏 DevKit Tuner CLI 的 top-down PMU 分析）。通过 sample-size 100 + 拉长 measurement-time 把单项跑长，等基准进入计量期后用 DevKit 在指定核上采集 top-down（frontend/backend/retiring/bad-speculation），产物 top-down.tar 可用 devkit report 或 WebUI 复核。触发词：benchmark 采 top-down、topdown 比例、DevKit 采集、前端/后端瓶颈分类、短时延单项采不到 PMU 数据。
---

# 沙箱官方 benchmark 短时延 → DevKit top-down 采集

**核心思想**（与 `sandbox-bench-flame` 一致）：官方 benchmark 单项仅 ~30ms 量级，
PMU 采集窗口不够。先把单项**长时化**——`--sample-size 100`（样本数固定）+
`--measurement-time` 拉长（如 120s），让计量期足够长；等基准进入计量期后，
再用 DevKit 在绑定的核上采集 top-down，窗口稳稳落在忙期内。要调用栈火焰图走
`sandbox-bench-flame`；要 top-down 比例分类（前端/后端/退休/预测错误）走本技能。

采集工具为鲲鹏 DevKit 系统性能分析命令行版（本技能按 **DevKit 26.3.T5 tar 解压版**
验证，已识别 Kunpeng 950；RPM 安装版亦可）。工具来源与下载：
<https://www.hikunpeng.com/developer/devkit/download?tab=commandLine>。
DevKit 原生支持 `-c` 指定核、`-p` PID、`-G` cgroup 范围化采集。

## 0. 输入契约（开工前必须确认）

| 输入 | 说明 | 缺省处理 |
|---|---|---|
| benchmark 名 | 9 选 1 白名单：`snapshot_creation` / `snapshot_creation_1gdisk` / `snapshot_creation_1gmem` / `snapshot_resume_cold` / `snapshot_resume` / `concurrent_resume` / `concurrent_resume_throughput` / `concurrent_snapshot_capture_throughput` / `concurrent_pause_throughput`。脚本侧有同名白名单校验，白名单之外一律拒绝 | **必须问用户**（"需要我直接帮你跑哪一项就说"） |
| 采集范围授权 | top-down 按指定的 `-c` 核范围采集，不采整机；但共享主机上仍须确认所选核在采集窗内承载本轮负载（或为本轮专用），以及 PMU 访问权限（devkit 需要 perf 权限） | **必须确认**；所选核承载他人负载时换核或停止 |
| 绑核范围 | benchmark 与采集共用同一核集合：48 个连续核；内存侧 `MEMBIND`（NUMA 节点） | 核默认 `47-94`，MEMBIND 默认 `2`，均可环境变量覆盖 |
| 计量时长 | 拉长后的 measurement 秒数 | 默认 120s（保证采集窗落在忙期内） |
| 采集参数 | top-down 采集时长 `TD_SEC`（默认 10s）、分析层级 `TD_LEVEL`（`-L 0-6`，默认 2） | 可环境变量覆盖；语义见 DevKit 官方文档 |
| 路径参数 | `AENV_ROOT` / `DEVKIT_BIN` / `WORK_ROOT` / `AENV_RUN_USER` / `AENV_HOME_PATH` / `AENV_RUNTIME_PATH` / `AENV_CONFIG_PATH` | 缺省见 §1；`DEVKIT_BIN` 与 `AENV_CONFIG_PATH` **必填**（自动探测失败即停），启动前逐一校验 |

## 1. 路径输入（缺省 `$HOME` 相对；换机作为输入提供，勿硬编码绝对路径）

| 路径 | 缺省 | 说明 |
|---|---|---|
| AENV_ROOT | `$HOME/AgentENV` | 仓库根；bench 二进制在 `target/release/deps/snapshot-*` |
| DEVKIT_BIN | **必填**（自动探测） | devkit 可执行文件；tar 解压位置各异，先按 `$HOME/devkit-tuner*/DevKit-Tuner-CLI-*/devkit` 自动探测（取 mtime 最新），失败则要求显式提供 |
| WORK_ROOT | `$HOME/bench-topdown-runs` | 产物根（本轮目录 `$WORK_ROOT/<bench>-<时间戳>/`） |
| AENV_RUN_USER | `root` | capabilities 包装器运行用户 |
| AENV_HOME_PATH | `$HOME/aenv-home` | AgentENV 数据根 |
| AENV_RUNTIME_PATH | `$HOME/aenv-home/run` | AgentENV 运行时目录 |
| AENV_CONFIG_PATH | **必填** | 对齐配置 toml（承载本机绑核/NUMA 口径，机器相关，不设缺省） |

bench 二进制名带构建哈希（如 `snapshot-c30cb69e660fe4b5`），**每次先 `ls -t target/release/deps/snapshot-* | grep -v '\.d$'` 现查**，不要写死。

## 2. 自动生成脚本一：`bench_run.sh`（单项长时 + 绑核运行）

与 `sandbox-bench-flame` 的运行脚本同骨架（**核心思想：sample 数固定 + 时长拉长**）。
写入 `$WORK_ROOT/<bench>-<ts>/bench_run.sh`（占位符 `<bench>` 替换为白名单内项名）：

```bash
#!/bin/bash
# sandbox-bench-topdown: 单项 benchmark 长时化运行（绑核，为 PMU 采集拉出窗口）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 先固定本轮绝对路径，再做任何 cd
BENCH="<bench>"
case "$BENCH" in
  snapshot_creation|snapshot_creation_1gdisk|snapshot_creation_1gmem|\
  snapshot_resume_cold|snapshot_resume|concurrent_resume|\
  concurrent_resume_throughput|concurrent_snapshot_capture_throughput|\
  concurrent_pause_throughput) ;;
  *) echo "[err] 非法 benchmark 名: $BENCH（白名单之外，拒绝执行）"; exit 2 ;;
esac
CORES="${CORES:-47-94}"
MEMBIND="${MEMBIND:-2}"
MEAS_SEC="${MEAS_SEC:-120}"
AENV_ROOT="${AENV_ROOT:-$HOME/AgentENV}"
AENV_RUN_USER="${AENV_RUN_USER:-root}"
AENV_HOME_PATH="${AENV_HOME_PATH:-$HOME/aenv-home}"
AENV_RUNTIME_PATH="${AENV_RUNTIME_PATH:-$HOME/aenv-home/run}"
AENV_CONFIG_PATH="${AENV_CONFIG_PATH:-}"
DEVKIT_BIN="${DEVKIT_BIN:-}"
LOG="$DIR/bench-run.log"
printf '%s\n' "$$" > "$DIR/runner.pgid"               # 本轮进程组号，供收尾按范围清理

# 启动前校验（换机/资产缺失即刻失败，不进入后台）
if [ -z "$DEVKIT_BIN" ]; then                          # tar 解压位置各异：自动探测 + 显式兜底
  DEVKIT_BIN=$(ls -t "$HOME"/devkit-tuner*/DevKit-Tuner-CLI-*/devkit 2>/dev/null | head -1 || true)
fi
[ -n "$DEVKIT_BIN" ] && [ -x "$DEVKIT_BIN" ] || { echo "[err] 需提供可执行的 DEVKIT_BIN=<devkit 路径>"; exit 1; }
[ -n "$AENV_CONFIG_PATH" ] && [ -r "$AENV_CONFIG_PATH" ] || { echo "[err] 需提供可读的 AENV_CONFIG_PATH=<对齐配置 toml>"; exit 1; }
[ -x "$AENV_ROOT/scripts/run-with-capabilities.sh" ] || { echo "[err] 缺 capabilities 包装器: $AENV_ROOT"; exit 1; }

cd "$AENV_ROOT"
BIN=$(ls -t target/release/deps/snapshot-* 2>/dev/null | grep -v '\.d$' | head -1)
[ -n "$BIN" ] && [ -x "$BIN" ] || { echo "[err] bench binary not found"; exit 1; }
BIN="$AENV_ROOT/${BIN#./}"                            # 绝对路径，供精确进程核对

# 范围化清理：仅终止 cmdline 同时含本 BIN 绝对路径与本项精确过滤器的进程
LEFT=0
for pid in $(pgrep -u "$(id -u)" -f "snapshot-" || true); do
  CMD=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$CMD" == *"$BIN"* && "$CMD" == *"--bench ^${BENCH}$"* ]]; then
    kill -9 "$pid" && LEFT=$((LEFT + 1))
  fi
done
[ "$LEFT" -gt 0 ] && { echo "== 清理本项旧残留 $LEFT 个"; sleep 2; }

echo "== bin=$BIN cores=$CORES membind=$MEMBIND samples=100 measure=${MEAS_SEC}s"

AENV_RUN_USER="$AENV_RUN_USER" \
AENV_HOME_PATH="$AENV_HOME_PATH" \
AENV_RUNTIME_PATH="$AENV_RUNTIME_PATH" \
AENV_CONFIG_PATH="$AENV_CONFIG_PATH" \
AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR="${AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR:-127.0.0.1:9113}" \
AENV_BENCH_PRINT_SAMPLES=1 \
"$AENV_ROOT/scripts/run-with-capabilities.sh" numactl --membind="$MEMBIND" taskset -c "$CORES" \
  "$BIN" --bench "^${BENCH}$" --sample-size 100 --measurement-time "$MEAS_SEC" \
  --warm-up-time 10 --noplot 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
```

要点：
- **白名单校验先行**：benchmark 名可能来自工单/issue/其他 skill 输出，不经校验直插脚本、正则、路径有命令注入与路径穿越风险
- `--bench` 显式进入 benchmark 模式；`^<bench>$` 精确锚定，避免同前缀变体误匹配
- `--sample-size 100 --measurement-time 120`：**样本数固定、时长拉长**——这是"时延短采不到 PMU"的核心解法
- `DIR` 在任何 `cd` 之前解析为绝对路径；启动即写 `runner.pgid`，收尾只对本轮进程组发信号
- 所有机器相关变量均为 `${VAR:-缺省}` 可覆盖形式，与 §0 输入契约一致
- concurrent_* 项可加 `AENV_BENCH_CONCURRENCY=<n>` 调并发

## 3. 自动生成脚本二：`capture_topdown.sh`（等计量期 → DevKit 采集）

同目录写入（与 bench_run.sh 共用 `$DIR` 绝对路径）：

```bash
#!/bin/bash
# sandbox-bench-topdown: 等基准进入计量期后用 DevKit 采集 top-down
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="<bench>"
case "$BENCH" in
  snapshot_creation|snapshot_creation_1gdisk|snapshot_creation_1gmem|\
  snapshot_resume_cold|snapshot_resume|concurrent_resume|\
  concurrent_resume_throughput|concurrent_snapshot_capture_throughput|\
  concurrent_pause_throughput) ;;
  *) echo "[err] 非法 benchmark 名: $BENCH"; exit 2 ;;
esac
CORES="${CORES:-47-94}"
TD_SEC="${TD_SEC:-10}"               # top-down 采集时长，须罩住计量忙期内的稳态段
TD_LEVEL="${TD_LEVEL:-2}"            # -L 分析层级（0-6），语义见 DevKit 文档
DEVKIT_BIN="${DEVKIT_BIN:-}"
LOG="$DIR/bench-run.log"
TD_LOG="$DIR/devkit-topdown.log"
PGID="$(cat "$DIR/runner.pgid" 2>/dev/null || true)"

[ -n "$PGID" ] || { echo "[err] 缺 runner.pgid（基准未按本流程启动，拒绝采集）"; exit 1; }
if [ -z "$DEVKIT_BIN" ]; then
  DEVKIT_BIN=$(ls -t "$HOME"/devkit-tuner*/DevKit-Tuner-CLI-*/devkit 2>/dev/null | head -1 || true)
fi
[ -n "$DEVKIT_BIN" ] && [ -x "$DEVKIT_BIN" ] || { echo "[err] 需提供可执行的 DEVKIT_BIN=<devkit 路径>"; exit 1; }

# 1) 等目标项进入计量期：Benchmarking <BENCH> 之后出现 "Collecting N samples"
#    进度行（criterion 计量进行中的标准输出，不依赖 PRINT_SAMPLES；最终报告的
#    time: 行出现在测量结束后，不能作门禁）。基准退出或 120s 超时立即失败。
ok=0
for i in $(seq 1 60); do
  if grep -q "Benchmarking ${BENCH}" "$LOG" 2>/dev/null \
      && grep -Eq "Collecting [0-9]+ samples" "$LOG" 2>/dev/null; then
    ok=1; break
  fi
  kill -0 "$PGID" 2>/dev/null || { echo "[err] 基准进程已退出且未见计量期"; exit 1; }
  sleep 2
done
[ "$ok" = 1 ] || { echo "[err] 120s 内 ${BENCH} 未进入计量阶段，放弃采集"; exit 1; }
sleep 5    # Collecting 出现即 warm-up 已完成、计量进行中，稍候落入稳态
kill -0 "$PGID" 2>/dev/null || { echo "[err] 采集前基准已退出"; exit 1; }

# 2) DevKit top-down 采集：范围即 -c 所选核（已按 §0 确认归本轮）；在 $DIR 下
#    运行使产物（top-down.tar 等）落在本轮目录
( cd "$DIR" && "$DEVKIT_BIN" tuner top-down -c "$CORES" -d "$TD_SEC" -L "$TD_LEVEL" ) 2>&1 | tee "$TD_LOG"
[ -s "$TD_LOG" ] || { echo "[err] devkit 日志为空"; exit 1; }

# 3) 产物核验：top-down.tar（或任务 tar 产物）应出现在 $DIR
TAR=$(ls -t "$DIR"/top-down.tar "$DIR"/*.tar 2>/dev/null | head -1 || true)
[ -n "$TAR" ] && [ -s "$TAR" ] || { echo "[err] 缺 top-down.tar 产物"; exit 1; }
echo "OK: top-down 产物 $TAR（devkit report 或 WebUI 可复核）"
```

## 4. 执行流程

```bash
# 0) 按 §0 输入契约完成确认：单项名（白名单）+ 采集核范围授权 + 路径参数
# 1) 生成本轮目录与两个脚本（替换 <bench> 占位符）
DIR="${WORK_ROOT:-$HOME/bench-topdown-runs}/<bench>-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DIR" && cd "$DIR"
# 写入 bench_run.sh / capture_topdown.sh 后：
chmod +x bench_run.sh capture_topdown.sh
export AENV_CONFIG_PATH=<对齐配置 toml>
[ -n "${DEVKIT_BIN:-}" ] || export DEVKIT_BIN=<devkit 路径>   # 留空则脚本自动探测
setsid nohup ./bench_run.sh > runner.out 2>&1 < /dev/null &   # setsid：独立进程组，号已记入 runner.pgid
./capture_topdown.sh                                          # 前台：等计量期 + DevKit 采集 + 产物核验
```

收尾（**仅限本轮范围**）：
```bash
kill -- -"$(cat runner.pgid)" 2>/dev/null   # 只终止本轮进程组，等待退出后再核对
ls /sys/class/ublk-char | wc -l             # ublk 残留检查
```

## 5. 汇报格式（固定）

```
benchmark: <名> | 采集: devkit tuner top-down -c <核> -d <秒> -L <层级>
top-down: <DIR>/top-down.tar（devkit report 或 WebUI 导入复核，比例分类
          frontend / backend / retiring / bad-speculation）
采集日志: <DIR>/devkit-topdown.log
基准日志: <DIR>/bench-run.log
瓶颈分流: 比例指向 frontend 瓶颈 → 指令取指/分支预测方向；
          backend 瓶颈 → 数据通路/访存方向（可转 sandbox-bench-flame 看调用栈，
         或 agentenv-snapshot-fs / kernel-hz 等专项）
```

## 6. 踩坑清单

1. **短时单项采不到 PMU**：不要对原版时长直接采集——先长时化（sample-size 100 +
   拉长 measurement-time），窗口落在计量忙期内才有意义
2. **计量期门禁**：以 "Collecting N samples" 进度行为准（进行中出现）；最终报告
   `time:` 行出现在测量结束后，不能作门禁
3. **采集核范围**：benchmark 绑核与 `-c` 采集核保持同一集合；共享主机上确认所选核
   归本轮（devkit 原生 `-p`/`-G` 亦可作更细的范围化）
4. **PMU 权限**：devkit 依赖 perf 权限，权限不足会非零退出（脚本即失败，不产出）
5. **二进制名带哈希会变，且旧哈希残留**：`ls -t` 按 mtime 取最新
6. **进程清理只按本轮范围**：残留清理校验 cmdline 含 BIN 绝对路径 + 本项过滤器；
   收尾只 kill 本轮 PGID
7. **其他分析任务**：`devkit tuner` 还有 hotspot / miss / memory / numafast /
   hpc-perf / roofline / turbostat 等子任务（参数语义见官方文档），本技能的
   长时化+门禁骨架同样适用，替换 `tuner top-down` 子命令与产物核验即可
