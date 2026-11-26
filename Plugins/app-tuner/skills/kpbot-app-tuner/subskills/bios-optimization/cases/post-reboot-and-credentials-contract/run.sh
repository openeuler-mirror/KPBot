#!/bin/bash
# run.sh — tier A：bios-optimization 生命周期契约用例
# 覆盖：post_reboot_verify.sh（快照参数表比对、Redfish 需确认路径、错误处理）
#       collect_bmc_credentials.sh（--help/必填校验、凭据经环境变量传递给 backup 脚本、密码不落盘）
# 运行（技能根目录）: bash cases/post-reboot-and-credentials-contract/run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d /tmp/bio-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_fail()    { if [ "$1" -ne 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (期望非零)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_not_contains(){ if echo "$1" | grep -qF -- "$2"; then red "  [FAIL] $3 (不应出现: $2)"; TEST_FAILED=$((TEST_FAILED+1)); else green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

echo "===== bios-optimization 生命周期契约用例 ====="

echo "PR-01: post_reboot_verify 无参数 → usage 退出"
bash "$SCRIPT_DIR/post_reboot_verify.sh" >/dev/null 2>&1
assert_fail $? "PR-01: 无参数 exit 1"

echo "PR-02: post_reboot_verify 快照文件不存在"
bash "$SCRIPT_DIR/post_reboot_verify.sh" "$WORK/no-such.json" >/dev/null 2>&1
assert_fail $? "PR-02: 文件不存在 exit 1"

echo "PR-03: 无采集器的参数（如 power_profile）→ 需 Redfish 确认路径"
cat > "$WORK/snap_redfish.json" <<'EOF'
{"bios_settings": {"power_profile": {"value": "performance"}, "cstate_profile": {"value": "single"}}}
EOF
OUT=$(bash "$SCRIPT_DIR/post_reboot_verify.sh" "$WORK/snap_redfish.json" 2>/dev/null)
assert_success $? "PR-03: rc=0"
assert_contains "$OUT" "需 Redfish 确认" "PR-03: 无采集器参数标注 Redfish"
assert_contains "$OUT" "部分参数需通过 Redfish 确认" "PR-03: 汇总声明部分需 Redfish"

echo "PR-04: 有采集器的参数（smt）→ 逐行输出当前值并完成比对"
cat > "$WORK/snap_smt.json" <<'EOF'
{"bios_settings": {"smt": {"value": "999999"}}}
EOF
OUT=$(bash "$SCRIPT_DIR/post_reboot_verify.sh" "$WORK/snap_smt.json" 2>/dev/null)
assert_success $? "PR-04: rc=0"
assert_contains "$OUT" "smt" "PR-04: smt 行输出"
assert_contains "$OUT" "=== 验证完成" "PR-04: 验证完成汇总"

echo "BMC-01: collect_bmc_credentials --help"
bash "$SCRIPT_DIR/collect_bmc_credentials.sh" --help >/dev/null 2>&1
assert_success $? "BMC-01: --help rc=0"

echo "BMC-02: 缺 --output-dir → exit 1"
bash "$SCRIPT_DIR/collect_bmc_credentials.sh" >/dev/null 2>&1
assert_fail $? "BMC-02: 缺参 exit 1"

echo "BMC-03: 凭据经环境变量传递给 backup 脚本；密码不落盘"
# mock backup_environment.sh：记录调用参数与 BMC_HOST/USER 及密码长度（不记录密码本身）
cat > "$WORK/mock_backup.sh" <<'EOF'
#!/bin/bash
echo "args: $*" >> "$MOCK_CALL_LOG"
echo "BMC_HOST=[$BMC_HOST] BMC_USER=[$BMC_USER] PASS_LEN=${#BMC_PASS}" >> "$MOCK_CALL_LOG"
EOF
chmod +x "$WORK/mock_backup.sh"
export MOCK_CALL_LOG="$WORK/mock_call.log"; : > "$MOCK_CALL_LOG"
printf '10.0.0.9\nadmin\nsecret123\n' | \
  BACKUP_SCRIPT_PATH="$WORK/mock_backup.sh" \
  bash "$SCRIPT_DIR/collect_bmc_credentials.sh" --output-dir "$WORK/bmc_out" >/dev/null 2>&1
assert_success $? "BMC-03: rc=0（管道喂凭据走通交互）"
assert_contains "$(cat "$MOCK_CALL_LOG")" "args: --output-dir $WORK/bmc_out --non-interactive" "BMC-03: 以 --non-interactive 调用 backup 脚本"
assert_contains "$(cat "$MOCK_CALL_LOG")" "BMC_HOST=[10.0.0.9] BMC_USER=[admin] PASS_LEN=9" "BMC-03: 凭据经环境变量传递（仅验证长度）"
assert_not_contains "$(cat "$WORK"/bmc_out/* 2>/dev/null; cat "$MOCK_CALL_LOG"; cat "$WORK"/*.log 2>/dev/null)" "secret123" "BMC-03: 密码不落盘（所有产物中无密码原文）"

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
