---
name: sandbox-bench-flame
description: 解决 AgentENV 官方 snapshot/resume/concurrent benchmark 单项时延过短、无法采集火焰图的问题。自动生成"长时化 + 48 连续核绑定"的单项运行脚本与配套采集脚本：sample-size 100 + 拉长 measurement-time，perf record -a -g -F 99 全系统采样 60s，clean_flame.sh 生成全景/聚焦火焰图。触发词：官方 benchmark 采火焰图、snapshot_creation / snapshot_resume / concurrent_resume 等单项热点、时延太短采不到图、给 XX benchmark 出火焰图。
---

# 沙箱官方 benchmark 短时延 → 火焰图采集

## 0. 输入契约（开工前必须确认）

| 输入 | 说明 | 缺省处理 |
|---|---|---|
| benchmark 名 | 9 选 1：`snapshot_creation` / `snapshot_creation_1gdisk` / `snapshot_creation_1gmem` / `snapshot_resume_cold` / `snapshot_resume` / `concurrent_resume` / `concurrent_resume_throughput` / `concurrent_snapshot_capture_throughput` / `concurrent_pause_throughput` | **必须问用户**（"需要我直接帮你跑哪一项就说"） |
| 整机采集授权 | 共享主机上 `perf record -a` 采集**整台主机**的调用栈，事后无法从 perf.data 剔除其他用户/任务的样本。须确认：整机采集的范围、窗口（60s）与产物访问权限 | **必须确认**；若仅获准分析单项负载，不得使用本 skill 的 `-a` 整机口径——改用获准范围或停止并说明 |
| 绑核范围 | 48 个连续核 | 默认 `47-94`（用户惯例 47-95 减 1 核凑整 48） |
| 计量时长 | 拉长后的 measurement 秒数 | 默认 120s（保证 60s 采样窗稳稳落在忙期内） |
| 路径参数 | `AENV_ROOT`（AgentENV 仓库）、`FLAME`（出图器）、`WORK_ROOT`（产物根） | 本机缺省见 §1；换机时作为输入提供，**启动后台负载前逐一校验存在且可执行**，缺即停 |

## 1. 路径输入（缺省为本机现值；换机即输入，勿硬编码）

| 路径 | 本机现值（缺省） |
|---|---|
| AENV_ROOT（仓库 + bench 二进制） | `/home/wangsi/AgentENV`（二进制在 `target/release/deps/snapshot-*`） |
| capabilities 包装器 | `$AENV_ROOT/scripts/run-with-capabilities.sh` |
| FLAME（火焰图生成器） | `/home/wangsi/agent-workload-benchmark-suite-main/baseline-driver/clean_flame.sh` |
| WORK_ROOT（产物根） | `/home/wangsi/flame-runs` |

bench 二进制名带构建哈希（如 `snapshot-c30cb69e660fe4b5`），**每次先 `ls -t target/release/deps/snapshot-* | grep -v '\.d$'` 现查**，不要写死。

## 2. 自动生成脚本一：`bench_run.sh`（单项长时 + 绑核运行）

写入 `$WORK_ROOT/<bench>-<ts>/bench_run.sh`（占位符 `<bench>` 替换为实际项名）：

```bash
#!/bin/bash
# sandbox-bench-flame: 单项 benchmark 长时化运行（绑 48 连续核）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 先固定本轮绝对路径，再做任何 cd
BENCH="<bench>"
CORES="${CORES:-47-94}"
MEAS_SEC="${MEAS_SEC:-120}"
AENV_ROOT="${AENV_ROOT:-/home/wangsi/AgentENV}"
FLAME="${FLAME:-/home/wangsi/agent-workload-benchmark-suite-main/baseline-driver/clean_flame.sh}"
LOG="$DIR/bench-run.log"
printf '%s\n' "$$" > "$DIR/runner.pgid"               # 本轮进程组号，供收尾按范围清理

# 启动前校验（换机/资产缺失即刻失败，不进入后台）
[ -x "$AENV_ROOT/scripts/run-with-capabilities.sh" ] || { echo "[err] 缺 capabilities 包装器: $AENV_ROOT"; exit 1; }
[ -x "$FLAME" ] || { echo "[err] 缺火焰图生成器: $FLAME"; exit 1; }

cd "$AENV_ROOT"
BIN=$(ls -t target/release/deps/snapshot-* 2>/dev/null | grep -v '\.d$' | head -1)
[ -n "$BIN" ] && [ -x "$BIN" ] || { echo "[err] bench binary not found"; exit 1; }
BIN="$AENV_ROOT/${BIN#./}"                            # 绝对路径，供精确进程核对

# 范围化清理：仅终止 cmdline 同时含本 BIN 绝对路径与本项精确过滤器的进程
# （属本技能同项旧残留）；其余进程一律不动，只计数报告
LEFT=0
for pid in $(pgrep -u "$(id -u)" -f "snapshot-" || true); do
  CMD=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$CMD" == *"$BIN"* && "$CMD" == *"--bench ^${BENCH}$"* ]]; then
    kill -9 "$pid" && LEFT=$((LEFT + 1))
  fi
done
[ "$LEFT" -gt 0 ] && { echo "== 清理本项旧残留 $LEFT 个"; sleep 2; }

echo "== bin=$BIN cores=$CORES samples=100 measure=${MEAS_SEC}s"
UB=$(ls /sys/class/ublk-char 2>/dev/null | wc -l)
echo "== ublk 残留: $UB 个（预算 64）"

AENV_RUN_USER=root \
AENV_HOME_PATH=/home/wangsi/aenv-home \
AENV_RUNTIME_PATH=/home/wangsi/aenv-home/run \
AENV_CONFIG_PATH=/home/wangsi/align-108.toml \
AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR=0.0.0.0:9113 \
AENV_BENCH_PRINT_SAMPLES=1 \
"$AENV_ROOT/scripts/run-with-capabilities.sh" numactl --membind=2 taskset -c "$CORES" \
  "$BIN" --bench "^${BENCH}$" --sample-size 100 --measurement-time "$MEAS_SEC" \
  --warm-up-time 10 --noplot 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
```

要点：
- `--bench` **显式进入 benchmark 模式**（缺省会落入测试模式、无计量阶段）；过滤器用 `^<bench>$` **精确锚定**，避免 `snapshot_creation` 误匹配 `snapshot_creation_1gdisk` 等同前缀变体
- `DIR` 在任何 `cd` 之前解析为绝对路径——`./bench_run.sh` 相对启动也不会把日志写进仓库目录
- 启动即写 `runner.pgid`：收尾只对本轮进程组发信号，**绝不 `pkill -f "snapshot-"`**（共享主机上会误杀他人或其他批次）
- `--sample-size 100 --measurement-time 120 --warm-up-time 10`：samples 固定 100、时长拉长——"时延短采不到图"的核心解法
- concurrent_* 项可加 `AENV_BENCH_CONCURRENCY=<n>` 调并发

## 3. 自动生成脚本二：`capture_flame.sh`（等计量期 → 采样 → 出图）

同目录写入（与 bench_run.sh 共用 `$DIR` 绝对路径）：

```bash
#!/bin/bash
# sandbox-bench-flame: 等基准进入计量期后全系统采样并生成火焰图
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="<bench>"
MEAS_SEC="${MEAS_SEC:-120}"
FLAME="${FLAME:-/home/wangsi/agent-workload-benchmark-suite-main/baseline-driver/clean_flame.sh}"
DATA="$DIR/perf-${BENCH}.data"
LOG="$DIR/bench-run.log"
PGID="$(cat "$DIR/runner.pgid" 2>/dev/null || true)"

[ -n "$PGID" ] || { echo "[err] 缺 runner.pgid（基准未按本流程启动，拒绝采样）"; exit 1; }
[ -x "$FLAME" ] || { echo "[err] 缺火焰图生成器: $FLAME"; exit 1; }

# 1) 等目标项进入计量期：日志须精确出现 Benchmarking <BENCH> 且带 time: 计量行；
#    基准进程退出或 120s 超时都立即失败，不把空闲/他人样本采进本轮报告
ok=0
for i in $(seq 1 60); do
  if grep -q "Benchmarking ${BENCH}" "$LOG" 2>/dev/null && grep -q "time:" "$LOG" 2>/dev/null; then
    ok=1; break
  fi
  kill -0 "$PGID" 2>/dev/null || { echo "[err] 基准进程已退出且未见计量期"; exit 1; }
  sleep 2
done
[ "$ok" = 1 ] || { echo "[err] 120s 内 ${BENCH} 未进入计量阶段，放弃采样"; exit 1; }
sleep 10   # 越过 warm-up 进入计量期
kill -0 "$PGID" 2>/dev/null || { echo "[err] 采样前基准已退出"; exit 1; }

# 2) 采集：固定口径，全系统 -a（禁止 -C 绑核采样）；授权口径见 SKILL 输入契约
perf record -a -g -F 99 -o "$DATA" -- sleep 60
[ -s "$DATA" ] || { echo "[err] perf.data 缺失或为空"; exit 1; }

# 3) 出图：全景 + 聚焦两张 SVG，产出逐一核验
bash "$FLAME" "$DATA" "$DIR/flame-${BENCH}"
for svg in "$DIR/flame-${BENCH}.all.svg" "$DIR/flame-${BENCH}.focused.svg"; do
  [ -s "$svg" ] || { echo "[err] 缺产物 $svg"; exit 1; }
done
echo "OK: 两份 SVG 已生成于 $DIR"
```

## 4. 执行流程

```bash
# 0) 按 §0 输入契约完成确认：单项名 + 整机采集授权 + 路径参数（换机时）
# 1) 生成本轮目录与两个脚本（替换 <bench> 占位符）
DIR=${WORK_ROOT:-/home/wangsi/flame-runs}/<bench>-$(date +%Y%m%d-%H%M%S)
mkdir -p "$DIR" && cd "$DIR"
# 写入 bench_run.sh / capture_flame.sh 后：
chmod +x bench_run.sh capture_flame.sh
setsid nohup ./bench_run.sh > runner.out 2>&1 < /dev/null &   # setsid：独立进程组，号已记入 runner.pgid
./capture_flame.sh                                            # 前台：等计量期 + 采样 + 出图
```

收尾（**仅限本轮范围**）：
```bash
kill -- -"$(cat runner.pgid)" 2>/dev/null   # 只终止本轮进程组，等待退出后再核对
ls /sys/class/ublk-char | wc -l             # ublk 残留检查
```

## 5. 汇报格式（固定）

```
benchmark: <名> | 样本数: <perf report Samples> | 采集口径: 全机 -a（已获授权）
火焰图: <DIR>/flame-<bench>.all.svg（全景）
        <DIR>/flame-<bench>.focused.svg（FC+vCPU+aenv+ublk+内核聚焦）
原始数据: <DIR>/perf-<bench>.data（可复析）
基准日志: <DIR>/bench-run.log
```

## 6. 踩坑清单

1. **空闲机器图必空**：capture_flame.sh 已内置门禁（计量期确认 + 进程存活检查），不要绕过它直接手跑 perf
2. **采样口径锁死 `perf record -a -g -F 99 -- sleep 60`**：全系统 `-a`，不用 `-C` 绑核（绑核会漏掉落在其他核上的 I/O/中断处理，实测曾采空）；`-a` 的整机授权在输入契约中前置确认；干扰进程靠 clean_flame.sh 的 EXCLUDE_RE 过滤，不靠采样期限核
3. **二进制名带哈希会变，且旧哈希残留**：deps 目录可能同时存在多个 `snapshot-<hash>`（当前 3 个），**必须 `ls -t` 按 mtime 取最新**，勿拿旧货
4. **ublk 残留 >4 有 EACCES 风险**：跑前查 `/sys/class/ublk-char`，超了先清
5. **进程清理只按本轮范围**：启动前残留清理校验 cmdline 含本 BIN 绝对路径 + 本项过滤器；收尾只 kill 本轮 PGID——共享主机上按 `snapshot-` 前缀盲杀会终结他人批次
6. EXCLUDE_RE 可环境变量覆盖：`EXCLUDE_RE='cannbot|node|python3' bash capture_flame.sh`
7. 若以后想"连续迭代无统计"的纯 profile 模式，criterion 还支持 `--profile-time 60`（只迭代不分析），比 sample-size 方案更省——当前按用户口径用 sample-size 100 + 拉长时间
