#!/bin/bash
# run.sh — tier A：diagnose_environment.py 检查引擎契约用例（kpbot-app-tuner 主 skill）
# 覆盖：check_one 十类检查原语的求值（fixture 驱动）、reference-issues 三态
#       （passed/failed/degraded + blocked_items 高严重度）、_invalid 与缺失路径
# 运行（技能根目录）: bash cases/diagnose-environment-contract/run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d /tmp/de-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_fail()    { if [ "$1" -ne 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (期望非零)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }
jget() { python3 -c "import json;d=json.load(open('$1'));print($2)" 2>/dev/null; }

echo "===== diagnose_environment 检查引擎契约用例 ====="

echo "DE-01: reference-issues 混合检查集——passed/failed/degraded + blocked"
BK="$WORK/backup"; mkdir -p "$BK"
echo "virt: kvm" > "$BK/virt.txt"
echo "flag_on" > "$WORK/marker.txt"
cat > "$WORK/issues.json" <<'EOF'
{"issues": [
  {"id": "v1", "severity": "low",  "check": {"type": "backup_file_contains", "path": "virt.txt", "expected": "kvm"}},
  {"id": "v2", "severity": "high", "check": {"type": "backup_file_contains", "path": "virt.txt", "expected": "NOT_PRESENT"}},
  {"id": "v3", "severity": "low",  "check": {"type": "backup_file_not_contains", "path": "virt.txt", "expected": "xen"}},
  {"id": "v4", "severity": "low",  "check": {"type": "file_equals", "path": "MARKER", "expected": "flag_on"}},
  {"id": "v5", "severity": "low",  "check": {"type": "file_exists", "path": "MARKER"}},
  {"id": "v6", "severity": "low",  "check": {"type": "command_contains", "command": ["echo", "hello-de"], "expected": "hello-de"}},
  {"id": "v7", "severity": "low",  "check": {"type": "bogus_type"}}
]}
EOF
export MARKER_PATH="$WORK/marker.txt"
python3 - "$WORK/issues.json" <<'PYEOF'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p))
for i in d["issues"]:
    c = i["check"]
    for k in ("path",):
        if c.get(k) == "MARKER":
            c[k] = os.environ["MARKER_PATH"]
json.dump(d, open(p, "w"))
PYEOF
python3 "$SCRIPT_DIR/diagnose_environment.py" --backup-dir "$BK" \
    --reference-issues "$WORK/issues.json" --output "$WORK/de01.json" >/dev/null 2>&1
assert_success $([ -s "$WORK/de01.json" ] && echo 0 || echo 1) "DE-01: 产出 JSON"
assert_eq "$(jget "$WORK/de01.json" "[c['status'] for c in d['reference_issue_checks'] if c['id']=='v1'][0]")" "passed" "DE-01: v1 backup_file_contains=passed"
assert_eq "$(jget "$WORK/de01.json" "[c['status'] for c in d['reference_issue_checks'] if c['id']=='v2'][0]")" "failed" "DE-01: v2 期望缺失=failed"
assert_eq "$(jget "$WORK/de01.json" "[c['status'] for c in d['reference_issue_checks'] if c['id']=='v7'][0]")" "unknown" "DE-01: v7 不支持类型=unknown"
assert_eq "$(jget "$WORK/de01.json" "d['reference_issue_set_status']")" "failed" "DE-01: 整体 status=failed"
assert_contains "$(jget "$WORK/de01.json" "str(d['blocked_items'])")" "v2" "DE-01: 高严重度失败进 blocked_items"
assert_contains "$(jget "$WORK/de01.json" "str(d['degraded_items'])")" "v7" "DE-01: unknown 进 degraded_items"

echo "DE-02: _invalid 标记 → invalid 状态"
printf '{"_invalid": "yaml parse failed"}' > "$WORK/bad.json"
python3 "$SCRIPT_DIR/diagnose_environment.py" --backup-dir "$BK" \
    --reference-issues "$WORK/bad.json" --output "$WORK/de02.json" >/dev/null 2>&1
assert_eq "$(jget "$WORK/de02.json" "d['reference_issue_set_status']")" "invalid" "DE-02: status=invalid"
assert_contains "$(jget "$WORK/de02.json" "str(d['degraded_items'])")" "reference_issue_set_parse_failed" "DE-02: 降级标记"

echo "DE-03: 缺失 reference-issues 路径 → not_present"
python3 "$SCRIPT_DIR/diagnose_environment.py" --backup-dir "$BK" \
    --reference-issues "$WORK/no-such.json" --output "$WORK/de03.json" >/dev/null 2>&1
assert_eq "$(jget "$WORK/de03.json" "d['reference_issue_set_status']")" "not_present" "DE-03: status=not_present"

echo "DE-04: 缺必填参数"
python3 "$SCRIPT_DIR/diagnose_environment.py" >/dev/null 2>&1
assert_fail $? "DE-04: 缺必填参数 argparse 非零退出"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

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
