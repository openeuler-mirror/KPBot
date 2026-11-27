#!/usr/bin/env bash
# Tier-A executable contract for smoke_test.sh (stub obclient, no real OB needed).
# Usage: bash cases/skill-integrity/run.sh   (from the skill root)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "${TMP}/obclient" <<'STUB_EOF'
#!/usr/bin/env bash
# Deterministic stub: log every invocation, return fixed TSV for smoke_test.sh.
printf '%s\n' "$*" >> "${STUB_LOG:?}"
case "$*" in
  *'SELECT VERSION();'*) printf '4.3.5.0\n' ;;
  *'DBA_OB_SERVERS'*) printf '127.0.0.1\tACTIVE\n' ;;
  *'DBA_OB_ZONES'*) printf 'z1\tACTIVE\n' ;;
  *'DBA_OB_TENANTS'*) printf 'mysql_tenant\tNORMAL\n' ;;
  *'DBA_OB_UNITS'*) printf '1001\t1001\tACTIVE\n' ;;
  # 必须带 SQL 引号形态 name='<param>'；若脚本拼成 name=<param>（无引号），
  # 此分支不命中，参数段转红，避免自测假绿掩盖真实集群的 Unknown column
  *"GV\$OB_PARAMETERS WHERE name='"*) printf '4\n' ;;
  *"SHOW VARIABLES LIKE 'read_only'"*) printf 'read_only\t%s\n' "${STUB_READ_ONLY:-OFF}" ;;
  *'SELECT TABLE_NAME FROM information_schema.TABLES'*) printf 't1\n' ;;
  *'SELECT COUNT(*)'*) printf '10\n' ;;
  *) printf '1\n' ;;
esac
STUB_EOF
chmod +x "${TMP}/obclient"

PASS=0
FAIL=0
MESSAGES=""

pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() {
  printf 'FAIL  %s\n' "$1"
  FAIL=$((FAIL + 1))
  MESSAGES="${MESSAGES}${1}\n"
}

# 运行冒烟：$@ 为 env 赋值（如 SPACE_DB=obtest），STUB_LOG 每次清空
run_smoke() {
  : > "${TMP}/stub.log"
  env STUB_LOG="${TMP}/stub.log" PATH="${TMP}:${PATH}" \
    OB_HOST=127.0.0.1 OB_PORT=2882 OB_USER=root@sys OB_DB=oceanbase \
    "$@" bash "${SKILL_DIR}/scripts/smoke_test.sh" > "${TMP}/out" 2>&1
}

assert_rc() {
  # $1=用例名 $2=期望退出码；run_smoke 须刚执行过
  local got=$1 want=$2 name=$3
  if [ "${got}" = "${want}" ]; then
    pass "${name}: exit=${got}"
  else
    fail "${name}: exit=${got} (expected ${want}); output=$(head -3 "${TMP}/out" | tr '\n' ' ')"
  fi
}

assert_stub_silent() {
  if [ ! -s "${TMP}/stub.log" ]; then
    pass "$1: 未调用任何 obclient"
  else
    fail "$1: 非法输入仍调用了 obclient: $(head -1 "${TMP}/stub.log")"
  fi
}

# 1) 非法端口 -> exit 2，无 obclient 调用
run_smoke OB_PORT=70000
assert_rc "$?" 2 "T1 invalid port"
assert_stub_silent "T1"

# 2) 非法 SPACE_DB（反引号）-> exit 2，无 obclient 调用
run_smoke 'SPACE_DB=bad`db' BIZ_USER=u BIZ_PASSWORD=p
assert_rc "$?" 2 "T2 invalid SPACE_DB"
assert_stub_silent "T2"

# 3) SAMPLE_COL='*' -> exit 2，无 obclient 调用
run_smoke SPACE_DB=obtest BIZ_USER=u BIZ_PASSWORD=p SAMPLE_TABLE=t1 'SAMPLE_COL=*'
assert_rc "$?" 2 "T3 SAMPLE_COL=*"
assert_stub_silent "T3"

# 4) 正常只读冒烟（带系统口令）-> exit 0，failures=0
run_smoke OB_PASSWORD=syspw
assert_rc "$?" 0 "T4 read-only smoke"
if grep -Eq 'failures=0' "${TMP}/out"; then pass "T4 summary failures=0"; else fail "T4 summary failures!=0"; fi

# 5) EXPECT_ROWS 不匹配 -> exit 1，summary failures>=1
run_smoke SPACE_DB=obtest BIZ_USER=u BIZ_PASSWORD=p SAMPLE_TABLE=t1 EXPECT_ROWS=99
assert_rc "$?" 1 "T5 expectation mismatch"
if grep -Eq 'failures=[1-9]' "${TMP}/out"; then pass "T5 summary failures>=1"; else fail "T5 summary missing failure"; fi

# 6) 临时 defaults-file 全部清理（系统租户 + 业务租户）
DFILES="$(grep -o -- '--defaults-extra-file=[^ ]*' "${TMP}/stub.log" | cut -d= -f2 | sort -u)"
if [ -n "${DFILES}" ]; then
  LEFTOVER=""
  while IFS= read -r df; do
    [ -n "${df}" ] && [ -e "${df}" ] && LEFTOVER="${LEFTOVER} ${df}"
  done <<EOF
${DFILES}
EOF
  if [ -z "${LEFTOVER}" ]; then pass "T6 defaults-file cleaned"; else fail "T6 defaults-file left:${LEFTOVER}"; fi
else
  fail "T6 no defaults-file recorded in stub log"
fi

# 7) EXPECT_WRITABLE 缺业务口令 -> exit 2（写验收目标必须显式指定）
run_smoke EXPECT_WRITABLE=1 BIZ_USER=u
assert_rc "$?" 2 "T7 writable missing credential"
assert_stub_silent "T7"

# 8) EXPECT_WRITABLE 走业务连接，业务租户只读 -> exit 1 且打印租户名
run_smoke EXPECT_WRITABLE=1 BIZ_USER=u BIZ_PASSWORD=p STUB_READ_ONLY=ON
assert_rc "$?" 1 "T8 read-only business tenant"
if grep -Fq 'read_only(u)=ON' "${TMP}/out"; then pass "T8 prints read_only(u)=ON"; else fail "T8 tenant name not printed"; fi
if grep -F "SHOW VARIABLES LIKE 'read_only'" "${TMP}/stub.log" | grep -Fq -- '-u u'; then
  pass "T8 read_only checked on business tenant"
else
  fail "T8 read_only checked on wrong tenant"
fi

# 9) 业务租户可写 -> exit 0 且打印 read_only(u)=OFF
run_smoke EXPECT_WRITABLE=1 BIZ_USER=u BIZ_PASSWORD=p STUB_READ_ONLY=OFF
assert_rc "$?" 0 "T9 writable business tenant"
if grep -Fq 'read_only(u)=OFF' "${TMP}/out"; then pass "T9 prints read_only(u)=OFF"; else fail "T9 missing writable PASS line"; fi

# 10) 关键参数 SQL 必须保留 name='<param>' 引号：若 shell 拼接吞掉引号，
#     真实集群报 Unknown column，stub 也应收不到带引号的 SQL，用例在此转红
if grep -Fq "WHERE name='cpu_count'" "${TMP}/stub.log"; then
  pass "T10 parameter SQL keeps name='<param>' quoting"
else
  fail "T10 parameter SQL lost SQL quoting: $(grep -F 'OB_PARAMETERS' "${TMP}/stub.log" | head -1)"
fi

python3 - "$RESULTS_DIR/result.json" "$PASS" "$FAIL" "$MESSAGES" <<'PY'
import json
import sys
from pathlib import Path

out, passed, failures, messages = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
out.write_text(json.dumps({
    "success": failures == 0,
    "checks": passed + failures,
    "failures": failures,
    "messages": [m for m in messages.splitlines() if m],
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "[case] result: ${RESULTS_DIR}/result.json (checks=$PASS pass, $FAIL fail)"
[ "${FAIL}" -eq 0 ]
