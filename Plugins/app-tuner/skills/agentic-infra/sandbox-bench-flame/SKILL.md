---
name: sandbox-bench-flame
description: 解决 AgentENV 官方 snapshot/resume/concurrent benchmark 单项时延过短、无法采集火焰图的问题。自动生成"长时化 + 48 连续核绑定"的单项运行脚本与配套采集脚本：sample-size 100 + 拉长 measurement-time，perf record -a -g -F 99 全系统采样 60s，clean_flame.sh 生成全景/聚焦火焰图。触发词：官方 benchmark 采火焰图、snapshot_creation / snapshot_resume / concurrent_resume 等单项热点、时延太短采不到图、给 XX benchmark 出火焰图。
---

# 沙箱官方 benchmark 短时延 → 火焰图采集

## 0. 输入契约（开工前必须确认）

| 输入 | 说明 | 缺省处理 |
|---|---|---|
| benchmark 名 | 9 选 1：`snapshot_creation` / `snapshot_creation_1gdisk` / `snapshot_creation_1gmem` / `snapshot_resume_cold` / `snapshot_resume` / `concurrent_resume` / `concurrent_resume_throughput` / `concurrent_snapshot_capture_throughput` / `concurrent_pause_throughput` | **必须问用户**（"需要我直接帮你跑哪一项就说"） |
| 绑核范围 | 48 个连续核 | 默认 `47-94`（用户惯例 47-95 减 1 核凑整 48） |
| 计量时长 | 拉长后的 measurement 秒数 | 默认 120s（保证 60s 采样窗稳稳落在忙期内） |

## 1. 固定资产（本机已就绪，勿重复建设）

| 资产 | 路径 |
|---|---|
| 参考实现（原版 6 项一键脚本） | `/home/wangsi/run-latency6.sh` |
| AgentENV 仓库 + bench 二进制 | `/home/wangsi/AgentENV/target/release/deps/snapshot-*` |
| capabilities 包装器 | `/home/wangsi/AgentENV/scripts/run-with-capabilities.sh` |
| 火焰图生成器 | `/home/wangsi/agent-workload-benchmark-suite-main/baseline-driver/clean_flame.sh` |
| 工作目录（本 skill 产物） | `/home/wangsi/flame-runs/<bench>-<时间戳>/` |

bench 二进制名带构建哈希（如 `snapshot-c30cb69e660fe4b5`），**每次先 `ls /home/wangsi/AgentENV/target/release/deps/snapshot-* | grep -v '\.d$'` 现查**，不要写死。

## 2. 自动生成脚本一：`bench_run.sh`（单项长时 + 绑核运行）

写入 `/home/wangsi/flame-runs/<bench>-<ts>/bench_run.sh`（占位符 `<bench>` 替换为实际项名）：

```bash
#!/bin/bash
# sandbox-bench-flame: 单项 benchmark 长时化运行（绑 48 连续核）
set -uo pipefail
BENCH="<bench>"
CORES="${CORES:-47-94}"        # 48 个连续核，可用环境变量覆盖
MEAS_SEC="${MEAS_SEC:-120}"    # 计量时长(拉长)，保证能罩住 60s 采样窗
cd /home/wangsi/AgentENV || exit 1
BIN=$(ls -t target/release/deps/snapshot-* 2>/dev/null | grep -v '\.d$' | head -1)
[ -n "$BIN" ] && [ -x "$BIN" ] || { echo "[err] bench binary not found"; exit 1; }
echo "== bin=$BIN cores=$CORES samples=100 measure=${MEAS_SEC}s"
UB=$(ls /sys/class/ublk-char 2>/dev/null | wc -l)
echo "== ublk 残留: $UB 个（预算 64）"
pkill -9 -x "$(basename "$BIN")" 2>/dev/null && sleep 2
LOG="$(cd "$(dirname "$0")" && pwd)/bench-run.log"
AENV_RUN_USER=root \
AENV_HOME_PATH=/home/wangsi/aenv-home \
AENV_RUNTIME_PATH=/home/wangsi/aenv-home/run \
AENV_CONFIG_PATH=/home/wangsi/align-108.toml \
AENV_UBLK_DAEMON_METRICS_LISTEN_ADDR=0.0.0.0:9113 \
AENV_BENCH_PRINT_SAMPLES=1 \
./scripts/run-with-capabilities.sh numactl --membind=2 taskset -c "$CORES" \
  "$BIN" "$BENCH" --sample-size 100 --measurement-time "$MEAS_SEC" \
  --warm-up-time 10 --noplot 2>&1 | tee "$LOG"
```

要点：
- `--sample-size 100 --measurement-time 120 --warm-up-time 10`：samples 固定 100、时长拉长——这是"时延短采不到图"的核心解法
- `taskset -c $CORES` 替换原脚本整节点绑核；`numactl --membind=2` 保留内存侧绑定
- `--noplot` 省掉绘图开销
- concurrent_* 项可加 `AENV_BENCH_CONCURRENCY=<n>` 调并发

## 3. 自动生成脚本二：`capture_flame.sh`（等忙 → 采样 → 出图）

同目录写入：

```bash
#!/bin/bash
# sandbox-bench-flame: 等基准进入计量期后全系统采样并生成火焰图
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH="<bench>"
DATA="$DIR/perf-${BENCH}.data"
FLAME=/home/wangsi/agent-workload-benchmark-suite-main/baseline-driver/clean_flame.sh

# 1) 等 benchmark 真正跑起来（日志出现 Benchmarking，最多等 120s）
for i in $(seq 1 60); do
  grep -q "Benchmarking" "$DIR/bench-run.log" 2>/dev/null && break
  sleep 2
done
sleep 10   # 越过 warm-up 进入计量期

# 2) 采集：固定口径，全系统 -a（禁止 -C 绑核采样）
perf record -a -g -F 99 -o "$DATA" -- sleep 60

# 3) 出图：全景 + 聚焦两张 SVG（EXCLUDE_RE 默认滤 cannbot/node/htop/top/sshd/bash/HeapHelper）
bash "$FLAME" "$DATA" "$DIR/flame-${BENCH}"
```

## 4. 执行流程

```bash
DIR=/home/wangsi/flame-runs/<bench>-$(date +%Y%m%d-%H%M%S)
mkdir -p "$DIR" && cd "$DIR"
# 写入两个脚本（替换 <bench> 占位符），然后：
chmod +x bench_run.sh capture_flame.sh
setsid nohup ./bench_run.sh > runner.out 2>&1 < /dev/null &   # 后台起基准
./capture_flame.sh                                             # 前台等忙+采样+出图
```

收尾：`pkill -f "snapshot-"` 确认基准退出；检查 ublk 残留（`ls /sys/class/ublk-char | wc -l`）。

## 5. 汇报格式（固定）

```
benchmark: <名> | 样本数: <perf report Samples>
火焰图: <DIR>/flame-<bench>.all.svg（全景）
        <DIR>/flame-<bench>.focused.svg（FC+vCPU+aenv+ublk+内核聚焦）
原始数据: <DIR>/perf-<bench>.data（可复析）
基准日志: <DIR>/bench-run.log
```

## 6. 踩坑清单

1. **空闲机器图必空**：采样前确认 bench 进程活着（`pgrep -f snapshot-`），日志已出现 `Benchmarking`；机器上没负载时 60s 只采到几百个 idle 样本
2. **采样口径锁死 `perf record -a -g -F 99 -- sleep 60`**：全系统 `-a`，不用 `-C` 绑核（绑核会漏掉落在其他核上的 I/O/中断处理，实测曾采空）；干扰进程靠 clean_flame.sh 的 EXCLUDE_RE 过滤，不靠采样期限核
3. **二进制名带哈希会变，且旧哈希残留**：deps 目录可能同时存在多个 `snapshot-<hash>`（当前 3 个），**必须 `ls -t` 按 mtime 取最新**，勿拿旧货
4. **ublk 残留 >4 有 EACCES 风险**：跑前查 `/sys/class/ublk-char`，超了先清
5. **杀旧进程**：起基准前 `pkill -9 -x <bin>`，否则新旧互踩
6. EXCLUDE_RE 可环境变量覆盖：`EXCLUDE_RE='cannbot|node|python3' bash capture_flame.sh`
7. 若以后想"连续迭代无统计"的纯 profile 模式，criterion 还支持 `--profile-time 60`（只迭代不分析），比 sample-size 方案更省——当前按用户口径用 sample-size 100 + 拉长时间
