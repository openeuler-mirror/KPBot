---
name: sandbox-bench-flame
description: 解决 AgentENV 官方 snapshot/resume/concurrent benchmark 单项时延过短、无法采集火焰图的问题。自动生成"长时化 + 48 连续核绑定"的单项运行脚本与配套采集脚本：sample-size 100 + 拉长 measurement-time，perf -g -F 99 采样 60s（整机 -a 需授权，亦可 cgroup 范围化），clean_flame.sh 生成全景/聚焦火焰图。触发词：官方 benchmark 采火焰图、snapshot_creation / snapshot_resume / concurrent_resume 等单项热点、时延太短采不到图、给 XX benchmark 出火焰图。
---

# 沙箱官方 benchmark 短时延 → 火焰图采集

## 0. 输入契约（开工前必须确认）

| 输入 | 说明 | 缺省处理 |
|---|---|---|
| benchmark 名 | 9 选 1 白名单：`snapshot_creation` / `snapshot_creation_1gdisk` / `snapshot_creation_1gmem` / `snapshot_resume_cold` / `snapshot_resume` / `concurrent_resume` / `concurrent_resume_throughput` / `concurrent_snapshot_capture_throughput` / `concurrent_pause_throughput`。脚本侧有同名白名单校验，白名单之外一律拒绝 | **必须问用户**（"需要我直接帮你跑哪一项就说"） |
| 采集范围授权 | `PERF_MODE=host`（整机 `-a`）会把共享主机上**其他用户/租户的调用栈**采入 perf.data，事后不可剔除，属敏感数据。须确认整机采集的范围、窗口与产物访问权限；仅获准分析单项负载时用 `PERF_MODE=cgroup` + `PERF_CGROUP`（或停止） | **必须确认**；未确认整机授权不得以 host 模式运行 |
| 绑核范围 | 48 个连续核；内存侧 `MEMBIND`（NUMA 节点） | 核默认 `47-94`，MEMBIND 默认 `2`，均可环境变量覆盖 |
| 计量时长 | 拉长后的 measurement 秒数 | 默认 120s（保证 60s 采样窗落在忙期内） |
| 路径参数 | `AENV_ROOT` / `FLAME`（出图器）/ `WORK_ROOT`（产物根）/ `AENV_RUN_USER` / `AENV_HOME_PATH` / `AENV_RUNTIME_PATH` / `AENV_CONFIG_PATH` | 缺省见 §1（`$HOME` 相对）；`FLAME` 与 `AENV_CONFIG_PATH` **必填**（无缺省），启动前逐一校验，缺即停 |

## 1. 路径输入（缺省 `$HOME` 相对；换机作为输入提供，勿硬编码绝对路径）

| 路径 | 缺省 | 说明 |
|---|---|---|
| AENV_ROOT | `$HOME/AgentENV` | 仓库根；bench 二进制在 `target/release/deps/snapshot-*` |
| FLAME | **必填** | 火焰图生成器（clean_flame.sh）绝对路径，因部署位置各异不设缺省 |
| WORK_ROOT | `$HOME/flame-runs` | 产物根（本轮目录 `$WORK_ROOT/<bench>-<时间戳>/`） |
| AENV_RUN_USER | `root` | capabilities 包装器运行用户 |
| AENV_HOME_PATH | `$HOME/aenv-home` | AgentENV 数据根 |
| AENV_RUNTIME_PATH | `$HOME/aenv-home/run` | AgentENV 运行时目录 |
| AENV_CONFIG_PATH | **必填** | 对齐配置 toml（承载本机绑核/NUMA 口径，机器相关，不设缺省） |

bench 二进制名带构建哈希（如 `snapshot-c30cb69e660fe4b5`），**每次先 `ls -t target/release/deps/snapshot-* | grep -v '\.d$'` 现查**，不要写死。

## 2. 自动生成脚本一：`bench_run.sh`（单项长时 + 绑核运行）

写入 `$WORK_ROOT/<bench>-<ts>/bench_run.sh`（占位符 `<bench>` 替换为白名单内项名）：

```bash
#!/bin/bash
# sandbox-bench-flame: 单项 benchmark 长时化运行（绑 48 连续核）
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
FLAME="${FLAME:-}"
LOG="$DIR/bench-run.log"
printf '%s\n' "$$" > "$DIR/runner.pgid"               # 本轮进程组号，供收尾按范围清理

# 启动前校验（换机/资产缺失即刻失败，不进入后台）
[ -n "$FLAME" ] && [ -x "$FLAME" ] || { echo "[err] 需提供可执行的 FLAME=<clean_flame.sh 路径>"; exit 1; }
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
UB=$(ls /sys/class/ublk-char 2>/dev/null | wc -l)
echo "== ublk 残留: $UB 个（预算 64）"

AENV_RUN_USER="$AENV_RUN_USER" \
AENV_HOME_PATH="$AENV_HOME_PATH" \
AENV_RUNTIME_PATH="$AENV_RUNTIME_PATH" \
AENV_CONFIG_PATH="$AENV_CONFIG_PATH" \
AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR="${AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR:-0.0.0.0:9113}" \
AENV_BENCH_PRINT_SAMPLES=1 \
"$AENV_ROOT/scripts/run-with-capabilities.sh" numactl --membind="$MEMBIND" taskset -c "$CORES" \
  "$BIN" --bench "^${BENCH}$" --sample-size 100 --measurement-time "$MEAS_SEC" \
  --warm-up-time 10 --noplot 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
```

要点：
- **白名单校验先行**：benchmark 名可能来自工单/issue/其他 skill 输出，不经校验直插脚本、正则、路径有命令注入与路径穿越风险——case 白名单之外一律 exit 2
- `--bench` **显式进入 benchmark 模式**（缺省会落入测试模式、无计量阶段）；过滤器用 `^<bench>$` **精确锚定**，避免 `snapshot_creation` 误匹配 `snapshot_creation_1gdisk` 等同前缀变体
- `DIR` 在任何 `cd` 之前解析为绝对路径——相对启动也不会把日志写进仓库目录
- 启动即写 `runner.pgid`：收尾只对本轮进程组发信号，**绝不 `pkill -f "snapshot-"`**
- 所有机器相关变量（含 `AENV_*` 三项）均为 `${VAR:-缺省}` 可覆盖形式，与 §0 输入契约一致
- concurrent_* 项可加 `AENV_BENCH_CONCURRENCY=<n>` 调并发

## 3. 自动生成脚本二：`capture_flame.sh`（等计量期 → 采样 → 出图）

同目录写入（与 bench_run.sh 共用 `$DIR` 绝对路径）：

```bash
#!/bin/bash
# sandbox-bench-flame: 等基准进入计量期后按授权口径采样并生成火焰图
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
FLAME="${FLAME:-}"
PERF_MODE="${PERF_MODE:-host}"      # host=整机 -a（须已获整机授权）| cgroup=范围化
PERF_CGROUP="${PERF_CGROUP:-}"
DATA="$DIR/perf-${BENCH}.data"
LOG="$DIR/bench-run.log"
PGID="$(cat "$DIR/runner.pgid" 2>/dev/null || true)"

[ -n "$PGID" ] || { echo "[err] 缺 runner.pgid（基准未按本流程启动，拒绝采样）"; exit 1; }
[ -n "$FLAME" ] && [ -x "$FLAME" ] || { echo "[err] 需提供可执行的 FLAME=<clean_flame.sh 路径>"; exit 1; }

# 采样范围：整机样本含他人调用栈，产物按敏感数据管理（umask 077，仅本机留存）
umask 077
case "$PERF_MODE" in
  host)
    [ "${PERF_AUTH_HOST:-0}" = 1 ] || { echo "[err] PERF_MODE=host 须确认整机采集授权后设 PERF_AUTH_HOST=1；仅授权单项请用 PERF_MODE=cgroup PERF_CGROUP=<name>"; exit 1; }
    PERF_ARGS=(-a) ;;
  cgroup)
    [ -n "$PERF_CGROUP" ] || { echo "[err] PERF_MODE=cgroup 需 PERF_CGROUP=<cgroup 名>"; exit 1; }
    PERF_ARGS=(--cgroup "$PERF_CGROUP") ;;
  *) echo "[err] 未知 PERF_MODE: $PERF_MODE"; exit 1 ;;
esac

# 1) 等目标项进入计量期：精确 Benchmarking <BENCH> + time: 计量行；
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

# 2) 采集（禁止 -C 绑核采样；范围由 PERF_MODE 决定）
perf record "${PERF_ARGS[@]}" -g -F 99 -o "$DATA" -- sleep 60
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
# 0) 按 §0 输入契约完成确认：单项名（白名单）+ 采集范围授权 + 路径参数
# 1) 生成本轮目录与两个脚本（替换 <bench> 占位符）
DIR="${WORK_ROOT:-$HOME/flame-runs}/<bench>-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DIR" && cd "$DIR"
# 写入 bench_run.sh / capture_flame.sh 后：
chmod +x bench_run.sh capture_flame.sh
export FLAME=<clean_flame.sh 绝对路径> AENV_CONFIG_PATH=<对齐配置 toml>
[ 已获整机授权 ] && export PERF_MODE=host PERF_AUTH_HOST=1 || export PERF_MODE=cgroup PERF_CGROUP=<name>
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
benchmark: <名> | 样本数: <perf report Samples> | 采集口径: <host 已授权 / cgroup <名>>
火焰图: <DIR>/flame-<bench>.all.svg（全景）
        <DIR>/flame-<bench>.focused.svg（FC+vCPU+aenv+ublk+内核聚焦）
原始数据: <DIR>/perf-<bench>.data（敏感：host 模式含整机样本——仅本机留存、
          限制访问与保留期限，勿作为交付物直接外发；外发仅用过滤后的 SVG）
基准日志: <DIR>/bench-run.log
```

## 6. 踩坑清单

1. **空闲机器图必空**：capture_flame.sh 已内置门禁（计量期确认 + 进程存活检查），不要绕过它直接手跑 perf
2. **采样范围与授权**：`-a` 整机口径须前置授权（`PERF_AUTH_HOST=1` 门禁），仅授权单项时 `PERF_MODE=cgroup` 范围化；口径锁死 `perf record -g -F 99 -- sleep 60`，不用 `-C` 绑核（绑核会漏掉落在其他核上的 I/O/中断处理，实测曾采空）；干扰进程靠 clean_flame.sh 的 EXCLUDE_RE 过滤，不靠采样期限核
3. **二进制名带哈希会变，且旧哈希残留**：deps 目录可能同时存在多个 `snapshot-<hash>`（当前 3 个），**必须 `ls -t` 按 mtime 取最新**，勿拿旧货
4. **ublk 残留 >4 有 EACCES 风险**：跑前查 `/sys/class/ublk-char`，超了先清
5. **进程清理只按本轮范围**：启动前残留清理校验 cmdline 含本 BIN 绝对路径 + 本项过滤器；收尾只 kill 本轮 PGID——共享主机上按 `snapshot-` 前缀盲杀会终结他人批次
6. EXCLUDE_RE 可环境变量覆盖：`EXCLUDE_RE='cannbot|node|python3' bash capture_flame.sh`
7. 若以后想"连续迭代无统计"的纯 profile 模式，criterion 还支持 `--profile-time 60`（只迭代不分析），比 sample-size 方案更省——当前按用户口径用 sample-size 100 + 拉长时间
