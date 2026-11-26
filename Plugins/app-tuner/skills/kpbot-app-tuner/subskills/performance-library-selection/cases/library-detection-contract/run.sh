#!/bin/bash
# test_detect_all_libraries.sh — performance-library-selection 库识别 UT（skill-ut-supplement）
# 对象：detect_all_libraries.sh（三种输入格式识别 + 18 类静态/动态检测 + ascend_runtime 硬门控）
# fixture：
#   perf_report_rank.txt ← journal r2-kptools-devkit/app-tuner/hotspot_rank.txt（旧 perf 管道真机基线）
#   devkit_hotspot.txt   ← journal r2-kptools-devkit/profiling-skill/hotspot.txt（DevKit 26.1.RC1 真机报告）
#   合成 symbol JSON     ← 2026-11-11 devkit 桥接验证实测数据（PyObject_Malloc/libruntime_v100 锁竞争 8%）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX="$CASE_DIR/fixtures"
WORK="$(mktemp -d /tmp/pl-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

# 从 detect_all_libraries 输出提取某 category 的 evidence_source（无该类别输出空）
field_of() { # $1=输出JSON $2=category
    python3 -c "
import json, sys
d = json.loads('''$1''')
for it in d.get('detected_libraries', []):
    if it.get('category') == '$2':
        print(it.get('evidence_source', ''))
        sys.exit(0)
print('ABSENT')" 2>/dev/null
}

echo "===== performance-library detect_all_libraries UT ====="

echo "PL-01: 真机 perf report 基线（call-graph 格式）→ 正常解析 + 无误报"
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$FIX/perf_report_rank.txt" 2>/dev/null); RC=$?
assert_success $RC "PL-01: rc=0"
assert_eq "$(echo "$OUT" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("detected_libraries",[])))' 2>/dev/null)" "0" "PL-01: 真实数据零误报（__libc_start_main/main 不命中任何类别）"

echo "PL-01b: 合成 perf report（malloc/memcpy 帧）→ allocators+memory_operations 动态命中"
cat > "$WORK/synth_perf.txt" <<'EOF'
   12.34%  a.out  libc.so.6  [.] malloc
    8.20%  a.out  libc.so.6  [.] __memcpy_avx_aarch64
    2.00%  a.out  a.out      [.] main
EOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/synth_perf.txt" 2>/dev/null)
assert_eq "$(field_of "$OUT" allocators)" "current" "PL-01b: allocators 动态 current"
assert_eq "$(field_of "$OUT" memory_operations)" "current" "PL-01b: memory_operations 动态 current"

echo "PL-02: symbol JSON 桥接契约（format 1）→ 动态命中 + ascend_runtime 门控 current"
cat > "$WORK/symbol_bridge.json" <<'EOF'
{"hotspots": [{"symbol": "hot_a", "module": "hotspot_demo", "overhead": "70.00%"}, {"symbol": "memcpy", "module": "libc.so.6", "overhead": "20.00%"}, {"symbol": "__pthread_mutex_lock", "module": "libruntime_v100.so", "overhead": "8.00%"}]}
EOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/symbol_bridge.json" 2>/dev/null)
assert_eq "$(field_of "$OUT" memory_operations)" "current" "PL-02: symbol 字段驱动态命中"
assert_eq "$(field_of "$OUT" ascend_runtime)" "current" "PL-02: ascend_runtime 门控打开（锁竞争 8%>5%，非 blocked）"
assert_eq "$(echo "$OUT" | python3 -c "
import json, sys
for it in json.load(sys.stdin).get('detected_libraries', []):
    if it.get('category') == 'ascend_runtime':
        print('强' if '锁竞争' in str(it) and '信号弱' not in str(it) else '弱')" 2>/dev/null)" "强" "PL-02: 8%>5% 阈值 → CANN 锁竞争（强信号分支）"

echo "PL-03: devkit 原生表格（真机）→ 识别边界锁定（不识别原生格式，配合指引桥接）"
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$FIX/devkit_hotspot.txt" 2>/dev/null); RC=$?
assert_success $RC "PL-03: rc=0"
assert_eq "$(echo "$OUT" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("detected_libraries",[])))' 2>/dev/null)" "0" "PL-03: 原生表格零命中（静默失效边界——消费方必须走 symbol JSON 桥接）"

echo "PL-04: evidence_report_json 格式 → 外部报告引用提取 + 静态库命中"
cat > "$WORK/synth_perf2.txt" <<'EOF'
    6.00%  a.out  libc.so.6  [.] malloc
    1.00%  a.out  a.out      [.] main
EOF
python3 - "$WORK" <<'PYEOF'
import json, sys
w = sys.argv[1]
# 提取优先级（随上游 !120 后收窄）：perf.perf_report_path（主框架格式）> evidence_files.perf_report
# （顶层 perf_report / perf_self_report_path 已随 perf_sampling_online.sh 删除而移除）
json.dump({"loaded_libraries": ["libz.so.1", "libc.so.6"],
           "perf": {"perf_report_path": f"{w}/synth_perf2.txt"}},
          open(f"{w}/evidence_report.json", "w"))
PYEOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/evidence_report.json" 2>/dev/null)
assert_eq "$(field_of "$OUT" compression)" "static_only" "PL-04: libz.so 静态命中 zlib"
assert_eq "$(field_of "$OUT" allocators)" "current" "PL-04: 引用的 perf report 驱动态命中"

echo "PL-05: ascend_runtime 硬门控——CANN 库在但无 symbol → blocked"
cat > "$WORK/no_symbol.json" <<'EOF'
{"loaded_libraries": ["libruntime_v100.so", "libtorch_npu.so"], "perf_report": ""}
EOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/no_symbol.json" 2>/dev/null)
assert_eq "$(field_of "$OUT" ascend_runtime)" "blocked" "PL-05: 无 symbol 字段 → blocked（红线 #3）"

echo "PL-06: 多类别合成——一次覆盖 13 个未由经验库触达的类别（hash/crypto/json/blas/fft/kv…）"
cat > "$WORK/multi_cat.json" <<'EOF'
{"hotspots": [
  {"symbol": "XXH64", "module": "libxxhash.so", "overhead": "9.00%"},
  {"symbol": "AES_encrypt", "module": "libcrypto.so", "overhead": "8.00%"},
  {"symbol": "rapidjson_parse", "module": "libsonic.so", "overhead": "7.00%"},
  {"symbol": "cblas_sgemv", "module": "libopenblas.so", "overhead": "6.00%"},
  {"symbol": "spmv_csr", "module": "a.out", "overhead": "5.00%"},
  {"symbol": "exp", "module": "libm.so", "overhead": "4.00%"},
  {"symbol": "conv2d_npu", "module": "a.out", "overhead": "3.50%"},
  {"symbol": "fftw_execute", "module": "libfftw3.so", "overhead": "3.00%"},
  {"symbol": "h264_encode_mb", "module": "libx264.so", "overhead": "2.50%"},
  {"symbol": "ParseFromArray", "module": "libprotobuf.so", "overhead": "2.00%"},
  {"symbol": "native_sql_codegen", "module": "a.out", "overhead": "1.50%"},
  {"symbol": "tls_tx", "module": "a.out", "overhead": "1.00%"},
  {"symbol": "rocksdb::DBImpl::Get", "module": "librocksdb.so", "overhead": "0.80%"}
]}
EOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/multi_cat.json" 2>/dev/null)
for cat in hash_functions crypto json linear_algebra sparse_linear_algebra math dnn fft video serialization sql_acceleration network kv_storage; do
    assert_eq "$(field_of "$OUT" $cat)" "current" "PL-06: $cat 动态命中"
done

echo "PL-07: ascend_runtime 锁竞争低于阈值（2%<5%）→ current 且信号弱（三态之三）"
cat > "$WORK/weak_lock.json" <<'EOF'
{"hotspots": [
  {"symbol": "acl_op_exec", "module": "libruntime_v100.so", "overhead": "60.00%"},
  {"symbol": "__pthread_mutex_lock", "module": "libruntime_v100.so", "overhead": "2.00%"}
]}
EOF
OUT=$(bash "$SCRIPT_DIR/detect_all_libraries.sh" "$WORK/weak_lock.json" 2>/dev/null)
assert_eq "$(field_of "$OUT" ascend_runtime)" "current" "PL-07: 采集成功 → current（非 blocked）"
assert_eq "$(echo "$OUT" | python3 -c "
import json, sys
for it in json.load(sys.stdin).get('detected_libraries', []):
    if it.get('category') == 'ascend_runtime':
        print('弱' if '信号弱' in str(it) else '强')" 2>/dev/null)" "弱" "PL-07: 锁竞争 2%<5% → 信号弱分支（与 PL-02 强竞争、PL-05 blocked 构成三态）"

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
