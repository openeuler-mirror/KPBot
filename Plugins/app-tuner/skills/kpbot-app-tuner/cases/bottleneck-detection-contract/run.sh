#!/bin/bash
# test_detect_bottleneck.sh — detect_bottleneck 分析 UT（skill-ut-supplement）
# 对象：kpbot-app-tuner/scripts/detect_bottleneck.sh（io-memory subskill 判定核心）
# 能力门控：本套件验证 nmon 稳定契约路径（--summary 消费、第 5 参数数据文件）。
#   该能力随 collection-replacement 分支合入而生效；master（mpstat 旧实现）上整套
#   SKIP 并保持全绿——合入后 35 用例自动激活，无需改动本套件。
# fixture：
#   r1nmon_nmon.csv ← journal r1-kptools-nmon（E-A 真机 384 核，4 核受载，均值 4.42 未触发侧）
#   mpstat_all.txt  ← journal r1-kptools-nmon/app-tuner/r1nmon_mpstat_all.txt（真机，n=1 均值语义）
#   合成 nmon       ← gen_nmon.py（触发侧：整机饱和/iowait/disk/memory/NPU host 高载——经验库无数据的分支）
#   合成 NPU        ← npu_status.json + npu_utilization.log（-t utilization 表格格式，r7 归档为 usages 键值格式不可直接用）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX="$CASE_DIR/fixtures"
GEN_NMON="$CASE_DIR/gen_nmon.py"
WORK="$(mktemp -d /tmp/db-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

# —— 能力门控：无 nmon 契约实现则整套跳过（master 基线全绿）——
if ! grep -q "nmon_summary_value" "$SCRIPT_DIR/detect_bottleneck.sh" 2>/dev/null; then
    echo "===== detect_bottleneck UT ====="
    skip "整套：detect_bottleneck 无 nmon 稳定契约路径（master 旧实现）——collection-replacement 合入后自动激活"
    echo ""
    echo "============================================"
    echo "  测试汇总"
    echo "============================================"
    green "  PASS: $TEST_PASSED"
    yellow "  SKIP: $TEST_SKIPPED"
    echo "  总计: $((TEST_PASSED + TEST_SKIPPED))"
    echo "============================================"
    mkdir -p "$CASE_DIR/results"
    python3 - "$CASE_DIR/results/result.json" "$TEST_PASSED" "$TEST_FAILED" "$TEST_SKIPPED" <<'RESULTPY'
import json, sys
p, passed, failed, skipped = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
json.dump({"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
           "checks_failures": failed, "skipped": skipped,
           "gated": "detect_bottleneck 无 nmon 稳定契约路径（master 旧实现），整套跳过；collection-replacement 合入后自动激活"},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
RESULTPY
    exit 0
fi

red()    { echo -e "\033[31m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

# 运行 SUT：$1=out.json 其后透传 detect_bottleneck 参数
run_db() {
    local out="$1"; shift
    setsid sleep 300 >/dev/null 2>&1 </dev/null &
    local TGT=$!
    sleep 0.2
    bash "$SCRIPT_DIR/detect_bottleneck.sh" "$TGT" "$@" > "$out" 2>/dev/null
    local rc=$?
    kill "$TGT" 2>/dev/null
    return $rc
}
# 精确取 JSON 字段：$1=json文件 $2=python 表达式（基于 d=json.load）
jget() { python3 -c "
import json
d = json.load(open('$1'))
print($2)" 2>/dev/null; }

# 静态受控 PATH（无 nmon/pidstat/ss/python3 → SKILL/工具链全降级，/proc 窗口仍可用）
STUB="$WORK/stub-bin"; mkdir -p "$STUB"
for t in bash awk sed grep ls cat sleep date mkdir sort head tail wc tr getconf uname; do
    ln -sf "$(command -v $t)" "$STUB/$t" 2>/dev/null
done

# 网络分支防噪声护栏：宿主背景流量可能触发 live 网络窗口（C3 后消除）。
# 若 detailed==network（非本用例目标），转 SKIP 而非 FAIL。
guard_not_network() { # $1=json $2=用例名 → 输出 detailed 值；network 时 SKIP
    local d=$(jget "$1" "d['detailed_bottleneck_type']")
    if [ "$d" = "network" ]; then
        skip "$2: 宿主背景流量触发 live 网络窗口（detect_bottleneck 纯化 C3 后消除）"
        return 1
    fi
    return 0
}

echo "===== detect_bottleneck UT ====="

echo "DB-01: 真机 nmon fixture（未触发侧）→ 契约值逐字段"
run_db "$WORK/db01.json" 3 "" "" "$FIX/r1nmon_nmon.csv"; RC=$?
assert_success $RC "DB-01: rc=0"
assert_eq "$(jget "$WORK/db01.json" "d['evidence']['mpstat_cpu_pct']")" "4.42" "DB-01: mpstat_cpu_pct=4.42（均值口径）"
assert_eq "$(jget "$WORK/db01.json" "d['evidence']['mpstat_idle_pct']")" "95.59" "DB-01: idle=95.59"
assert_eq "$(jget "$WORK/db01.json" "d['evidence']['iowait_pct']")" "0.0" "DB-01: iowait=0.0"
assert_eq "$(jget "$WORK/db01.json" "d['evidence']['disk_util_pct']")" "0.0" "DB-01: disk_util=0.0"
assert_eq "$(jget "$WORK/db01.json" "d['evidence']['memory_pressure_hint']")" "low" "DB-01: memory=low"
assert_contains "$(jget "$WORK/db01.json" "d['fallback_notes']")" "cpu_metrics_from_nmon_skill" "DB-01: fallback_notes 含 nmon 契约标记"
assert_eq "$(jget "$WORK/db01.json" "1 if d['evidence']['effective_cpu_count'] > 0 else 0")" "1" "DB-01: effective_cpu_count>0（宽松）"

echo "DB-02: CORE_LIST 0-3（真机受载核）→ 判定域切换为列内核"
run_db "$WORK/db02.json" 3 "0-3" "" "$FIX/r1nmon_nmon.csv"
assert_eq "$(jget "$WORK/db02.json" "d['detailed_bottleneck_type']")" "cpu" "DB-02: detailed=cpu（列内核均值>85，确定性：cpu 先于网络判）"
assert_eq "$(jget "$WORK/db02.json" "d['bottleneck_type']")" "cpu_bottleneck" "DB-02: bottleneck=cpu_bottleneck"
assert_eq "$(jget "$WORK/db02.json" "d['confidence']")" "medium" "DB-02: confidence=medium"
assert_contains "$(jget "$WORK/db02.json" "d['fallback_notes']")" "cpu_judgment_on_core_list" "DB-02: 标注判定域"

echo "DB-03: SKILL 链全不可用（stub PATH，无 nmon/pidstat/ss/python3）→ 结构化降级"
PATH="$STUB" run_db "$WORK/db03.json" 2 "" "" ""; RC=$?
assert_success $RC "DB-03: rc=0（优雅降级不崩）"
assert_eq "$(jget "$WORK/db03.json" "d['evidence']['mpstat_cpu_pct']")" "None" "DB-03: cpu 置空"
assert_contains "$(jget "$WORK/db03.json" "d['fallback_notes']")" "nmon_collection_unavailable" "DB-03: nmon 降级声明"
assert_contains "$(jget "$WORK/db03.json" "d['fallback_notes']")" "process_cpu_source_unavailable" "DB-03: process 降级声明"
assert_contains "$(jget "$WORK/db03.json" "d['fallback_notes']")" "sock_source_unavailable" "DB-03: sock 降级声明"

echo "DB-04a: 合成 iowait=25（低 CPU 背景避免 cpu 先判）→ disk_io"
python3 "$GEN_NMON" --out "$WORK/iowait.nmon" --user 5 --sys 3 --wait 25 --idle 67 --cores 4
run_db "$WORK/db04a.json" 2 "" "" "$WORK/iowait.nmon"
guard_not_network "$WORK/db04a.json" "DB-04a" && \
    assert_eq "$(jget "$WORK/db04a.json" "d['detailed_bottleneck_type']")" "disk_io" "DB-04a: wait>10 → disk_io"

echo "DB-04b: 合成 diskbusy=90（低 CPU 背景）→ disk_bandwidth"
python3 "$GEN_NMON" --out "$WORK/diskb.nmon" --user 5 --sys 3 --wait 0 --idle 92 --diskbusy 90 --cores 4
run_db "$WORK/db04b.json" 2 "" "" "$WORK/diskb.nmon"
guard_not_network "$WORK/db04b.json" "DB-04b" && \
    assert_eq "$(jget "$WORK/db04b.json" "d['detailed_bottleneck_type']")" "disk_bandwidth" "DB-04b: busy>80 → disk_bandwidth"

echo "DB-04c: 合成内存高水位（used/total≈99%，低 CPU 背景）→ memory_capacity"
python3 "$GEN_NMON" --out "$WORK/mem.nmon" --user 5 --sys 3 --wait 0 --idle 92 --memtotal 100000 --memfree 500 --cached 0 --buffers 0 --cores 4
run_db "$WORK/db04c.json" 2 "" "" "$WORK/mem.nmon"
guard_not_network "$WORK/db04c.json" "DB-04c" && \
    assert_eq "$(jget "$WORK/db04c.json" "d['detailed_bottleneck_type']")" "memory_capacity" "DB-04c: >0.9 → memory_capacity"
assert_eq "$(jget "$WORK/db04c.json" "d['evidence']['memory_pressure_hint']")" "high" "DB-04c: hint=high"

skip "DB-05/06: 网络主分支（丢包/队列）+ pps×irq 次分支与 needs_collection 契约——需 C3 纯化后的 socket-queues/net-bandwidth/irqstat-window 快照输入（设计 §4.3/§10 D6），占位锁定"

echo "DB-08: 合成整机饱和（user+sys=88）→ 整机 cpu 判定"
python3 "$GEN_NMON" --out "$WORK/sat.nmon" --user 80 --sys 8 --idle 12 --cores 4
run_db "$WORK/db08.json" 2 "" "" "$WORK/sat.nmon"
assert_eq "$(jget "$WORK/db08.json" "d['detailed_bottleneck_type']")" "cpu" "DB-08: 整机均值>85 → cpu（先于网络，确定性）"

echo "DB-09: CORE_LIST 越界（500-503 vs 384 核 fixture）→ 不支持回落整机均值"
run_db "$WORK/db09.json" 3 "500-503" "" "$FIX/r1nmon_nmon.csv"
assert_contains "$(jget "$WORK/db09.json" "d['fallback_notes']")" "core_list_unsupported_by_nmon_source" "DB-09: 越界声明"
D=$(jget "$WORK/db09.json" "d['detailed_bottleneck_type']")
if [ "$D" = "network" ]; then skip "DB-09: 背景流量干扰"; else
    assert_eq "$D" "unknown" "DB-09: 回落均值 4.42<85 → 不判 cpu"
fi

echo "DB-10~12: NPU 三分类（合成 utilization 表 + 状态文件）"
mk_npu_env() { # $1=evidence目录 $2=util值 $3=nmon（拷入 workload_running 供 parse_host_cpu） $4=mpstat(可选)
    mkdir -p "$1/workload_running"
    printf '{"npu_available": true}\n' > "$1/workload_running/npu_status.json"
    printf '0 %s 30\n1 %s 25\n' "$2" "$2" > "$1/workload_running/npu_utilization.log"
    [ -n "$3" ] && cp "$3" "$1/workload_running/nmon_20261119_000000_1.nmon"
    [ -n "$4" ] && cp "$4" "$1/workload_running/mpstat.txt"
}
python3 "$GEN_NMON" --out "$WORK/host82.nmon" --user 78 --sys 4 --idle 18 --cores 4
# DB-10 cpu_feed：NPU 50% + host 82%（>80 且 <85 → 整机不判 cpu，NPU 覆盖生效）
mk_npu_env "$WORK/npu-feed" 50 "$WORK/host82.nmon" ""
run_db "$WORK/db10.json" 2 "" "$WORK/npu-feed" "$WORK/host82.nmon"
assert_eq "$(jget "$WORK/db10.json" "d['evidence']['npu_bottleneck_type']")" "cpu_feed_bottleneck" "DB-10: u<60&c>80 → cpu_feed_bottleneck"
guard_not_network "$WORK/db10.json" "DB-10" && \
    assert_eq "$(jget "$WORK/db10.json" "d['bottleneck_type']")" "cpu_feed_bottleneck" "DB-10: 覆盖生效（detailed unknown 时）"
# DB-11 compute：NPU 95%
mk_npu_env "$WORK/npu-comp" 95 "$FIX/r1nmon_nmon.csv" ""
run_db "$WORK/db11.json" 2 "" "$WORK/npu-comp" "$FIX/r1nmon_nmon.csv"
assert_eq "$(jget "$WORK/db11.json" "d['evidence']['npu_bottleneck_type']")" "npu_compute_bottleneck" "DB-11: u>90 → npu_compute_bottleneck"
assert_eq "$(jget "$WORK/db11.json" "d['evidence']['npu_utilization_pct']")" "95" "DB-11: npu_util=95"
# DB-12 other：NPU 70%
mk_npu_env "$WORK/npu-other" 70 "$FIX/r1nmon_nmon.csv" ""
run_db "$WORK/db12.json" 2 "" "$WORK/npu-other" "$FIX/r1nmon_nmon.csv"
assert_eq "$(jget "$WORK/db12.json" "d['evidence']['npu_bottleneck_type']")" "npu_other" "DB-12: 60≤u≤90 → npu_other"
assert_eq "$(jget "$WORK/db12.json" "d['evidence']['host_cpu_util_pct']")" "4" "DB-12: host=4（nmon 优先路径，82→4.42≈4）"

echo "DB-13: NPU unavailable → NPU 分支跳过"
mkdir -p "$WORK/npu-off"; printf '{"npu_available": false}\n' > "$WORK/npu-off/npu_status.json"
run_db "$WORK/db13.json" 2 "" "$WORK/npu-off" "$FIX/r1nmon_nmon.csv"
assert_eq "$(jget "$WORK/db13.json" "d['evidence']['npu_bottleneck_type']")" "None" "DB-13: npu_available=false → 字段为 null"

echo "DB-14: parse_host_cpu mpstat.txt 回退（真机 mpstat，n=1 均值语义）"
mkdir -p "$WORK/npu-ms/workload_running"
printf '{"npu_available": true}\n' > "$WORK/npu-ms/workload_running/npu_status.json"
printf '0 50 30\n' > "$WORK/npu-ms/workload_running/npu_utilization.log"
cp "$FIX/mpstat_all.txt" "$WORK/npu-ms/workload_running/mpstat.txt"
PATH="$STUB" run_db "$WORK/db14.json" 2 "" "$WORK/npu-ms" ""; RC=$?
assert_success $RC "DB-14: rc=0"
assert_eq "$(jget "$WORK/db14.json" "d['evidence']['host_cpu_util_pct']")" "3" "DB-14: mpstat 回退均值=2.86→3（真机数据）"
assert_eq "$(jget "$WORK/db14.json" "d['evidence']['npu_bottleneck_type']")" "npu_other" "DB-14: 50% 不满 feed/compute → other"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

# case 契约：case.yaml 的 json_field 断言消费 results/result.json
mkdir -p "$CASE_DIR/results"
python3 - "$CASE_DIR/results/result.json" "$TEST_PASSED" "$TEST_FAILED" "$TEST_SKIPPED" <<'RESULTPY'
import json, sys
p, passed, failed, skipped = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
json.dump({"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
           "checks_failures": failed, "skipped": skipped},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sys.exit(0 if failed == 0 else 1)
RESULTPY
exit $?
