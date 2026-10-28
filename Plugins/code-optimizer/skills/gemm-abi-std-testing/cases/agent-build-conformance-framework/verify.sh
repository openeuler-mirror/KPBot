#!/usr/bin/env bash
# verify.sh —— agent-build-conformance-framework 的产物判定
#
# 判定 agent 在 opt/conformance/ 下产出的「标准符合性测试框架」是否成立：
#   1) fixture 自检：DUT 与独立 oracle 一致（case 前置，与 skill 无关）
#   2) 产物齐备：opt/conformance/ 非空，且含条款绑定（出现标准号 MS-1）
#   3) 一键入口可运行：优先 ci/run_conformance.sh，其次 Makefile，再次任一 *.sh
#   4) 运行产出结果文件（results*.csv）
#
# 输出：results/result.json（success / skill_mode / 各判定位 / 入口 / 日志摘要）
# 退出码：0 表示全部成立，非 0 表示未成立（供 case.yaml 的 exit_code 断言使用）。
set -uo pipefail

cd /workspace
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}"
OPT=opt/conformance

# ---- 1) fixture 自检（不依赖 agent 产物）----
FIXTURE_OK=0
if gcc -O2 -std=c11 -Isrc bench/main.c src/kernel.c -lm -o build_fixture_check \
        >results/fixture_build.log 2>&1; then
    if ./build_fixture_check >results/fixture_check.out 2>&1 \
            && grep -q '^CORRECT=1$' results/fixture_check.out; then
        FIXTURE_OK=1
    fi
fi

# ---- 2) 产物齐备 + 条款绑定 ----
PRESENT=0
CLAUSE=0
if [ -d "${OPT}" ] && [ -n "$(find "${OPT}" -type f -print -quit 2>/dev/null)" ]; then
    PRESENT=1
    if grep -rqs "MS-1" "${OPT}" 2>/dev/null; then
        CLAUSE=1
    fi
fi

# ---- 3) 一键入口可运行（ENTRY 一律相对框架根，执行时切到框架根）----
ENTRY=""
if [ -f "${OPT}/ci/run_conformance.sh" ]; then
    ENTRY="./ci/run_conformance.sh"
elif [ -f "${OPT}/Makefile" ] || [ -f "${OPT}/makefile" ]; then
    ENTRY="make -C ."
else
    first_sh="$(find "${OPT}" -maxdepth 3 -type f -name '*.sh' -print -quit 2>/dev/null || true)"
    [ -n "${first_sh}" ] && ENTRY="./${first_sh#"${OPT}"/}"
fi

RUN_RC=127
if [ "${PRESENT}" = "1" ] && [ -n "${ENTRY}" ]; then
    ( cd "${OPT}" && timeout 300 bash -lc "${ENTRY}" ) >results/framework_run.log 2>&1
    RUN_RC=$?
fi

# ---- 4) 结果产物 ----
CSV_FOUND=0
if [ -n "$(find "${OPT}" -maxdepth 4 -type f -name 'results*.csv' -print -quit 2>/dev/null)" ]; then
    CSV_FOUND=1
fi

RUN_OK=0
if [ "${RUN_RC}" = "0" ] && [ "${CSV_FOUND}" = "1" ]; then
    RUN_OK=1
fi

export SKILL_MODE FIXTURE_OK PRESENT CLAUSE ENTRY RUN_RC CSV_FOUND RUN_OK
python3 - <<'PY'
import json, os
present  = os.environ["PRESENT"] == "1"
fixture  = os.environ["FIXTURE_OK"] == "1"
clause   = os.environ["CLAUSE"] == "1"
run_ok   = os.environ["RUN_OK"] == "1"
success  = present and fixture and clause and run_ok
reason = "ok" if success else ",".join(
    name for name, ok in (("fixture_failed", fixture), ("framework_absent", present),
                          ("clause_not_bound", clause), ("framework_run_failed", run_ok))
    if not ok)
json.dump({
    "success": success,
    "reason": reason,
    "skill_mode": os.environ["SKILL_MODE"],
    "fixture_ok": int(fixture),
    "framework_present": int(present),
    "clause_bound": int(clause),
    "entry": os.environ.get("ENTRY", ""),
    "run_rc": int(os.environ["RUN_RC"]),
    "csv_found": int(os.environ["CSV_FOUND"] == "1"),
    "conformance_ready": int(success),
}, open("results/result.json", "w"), ensure_ascii=False, indent=2)
print("verify:", "PASS" if success else "FAIL", "reason=" + reason)
PY

if [ "$(python3 -c 'import json;print(1 if json.load(open("results/result.json"))["success"] else 0)')" = "1" ]; then
    exit 0
fi
exit 1
