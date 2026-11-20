#!/usr/bin/env bash
# =============================================================================
# One-click runner for nginx-optimization skill cases. Self-contained: it does
# not modify KPBot core. It discovers sibling cases/*/case.yaml, builds the
# declared image, calls each case's own run_ab.sh, and evaluates the case.yaml
# asserts against the saved summary.
#
# 本脚本内置 PR#100 检视的四项加固：
#   1. PyYAML 为硬依赖：缺失时明确报错退出（不再静默跳过断言）；
#   2. --strict 真正比较 with > without（按 case.yaml ab.metric），与帮助文本一致；
#   3. with 臂必须真实调用过 skill（skills_invoked 硬门禁），否则判 invalid 而非 pass；
#   4. 输出目录安全：OUT 必须绝对路径；case name 为空跳过并报错；旧目录改为
#      时间戳备份（打印路径），绝不 rm -rf 未校验路径；断言总数为 0 视为 fail。
#
# Usage:
#   ./cases/run_all.sh [options]
#
# Options:
#   --case NAME      run only one case directory (default: all)
#   --model MODEL    forwarded to the case container as MODEL
#   --out DIR        artifact root, must be absolute (default: /tmp/nginx-cases)
#   --timeout SEC    per docker build/run timeout (default: 900)
#   --no-build       skip docker build (use existing images)
#   --dry-run        print build/run commands, do not execute
#   --list           list discovered cases and exit
#   --strict         require with-success, with-invoked-skill and with > without
#   --lenient        always exit 0 (still prints the summary)
#   -h, --help       show this help
#
# Requirements: bash, python3 + PyYAML (hard), docker, opencode binary/auth
# for the case containers. It never touches a real nginx.
# =============================================================================
set -uo pipefail

CASES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${TMPDIR:-/tmp}/nginx-cases"
MODEL="${OPENCODE_MODEL:-}"
ONLY_CASE=""
DO_BUILD=1
DRY_RUN=0
LIST=0
STRICT=0
LENIENT=0
TIMEOUT_SEC=900

while [[ $# -gt 0 ]]; do
    case "$1" in
        --case) ONLY_CASE="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --timeout) TIMEOUT_SEC="${2:?}"; shift 2 ;;
        --no-build) DO_BUILD=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --list) LIST=1; shift ;;
        --strict) STRICT=1; shift ;;
        --lenient) LENIENT=1; shift ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ── 加固 1：PyYAML 硬依赖 ─────────────────────────────────────────────
if ! python3 -c "import yaml" >/dev/null 2>&1; then
    echo "error: PyYAML is required (python3 -c 'import yaml' failed)." >&2
    echo "       install with: dnf install -y python3-pyyaml  # or pip3 install pyyaml" >&2
    exit 2
fi

# ── 加固 4：OUT 必须绝对路径 ──────────────────────────────────────────
if [[ "${OUT}" != /* ]]; then
    echo "error: --out must be an absolute path, got: ${OUT}" >&2
    exit 2
fi

mapfile -t CASE_YAMLS < <(find "$CASES_DIR" -mindepth 2 -maxdepth 2 -name case.yaml | sort)

if [[ ${LIST} -eq 1 ]]; then
    for y in "${CASE_YAMLS[@]}"; do
        python3 - "$y" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
print(f"{d.get('name') or '<missing-name>'}\ttier={d.get('tier')}\timage={d.get('image')}")
PY
    done
    exit 0
fi

FAILED=0

# 读取 case.yaml 元数据（name 为空时输出哨兵 __missing_name__：bash read 会吞前导
# IFS 空白，直接输出空字段会被错位读取，哨兵保证空 name 一定被跳过并报错）
read_case_meta() { # <case.yaml>
    python3 - "$1" <<'PY'
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1])) or {}
except Exception as e:
    print(f"__parse_error__\t{e}")
    sys.exit(0)
name = d.get("name") or ""
skill = d.get("skill") or ""
tier = d.get("tier") or ""
image = d.get("image") or ""
ctx = (d.get("build") or {}).get("context") or ""
df = (d.get("build") or {}).get("dockerfile") or ""
if not name:
    print("__missing_name__\t")
    sys.exit(0)
print(f"{name}\t{skill}\t{tier}\t{image}\t{ctx}\t{df}")
PY
}

# 评估一个 case 的断言（读取 run_ab 保存的 summary.json）
eval_case() { # <summary.json> <case.yaml>
    python3 - "$1" "$2" <<'PY'
import json, sys, yaml

summary_path, case_yaml = sys.argv[1], sys.argv[2]
case = yaml.safe_load(open(case_yaml)) or {}
skill = case.get("skill") or ""
asserts = case.get("assert") or []

try:
    summary = json.load(open(summary_path))
except Exception:
    summary = {"rows": []}
rows = summary.get("rows") or []
with_row = rows[0] if rows else {}
without_row = rows[1] if len(rows) > 1 else {}

total = len(asserts)
passed = 0
details = []

for a in asserts:
    t = a.get("type")
    ok = False
    note = ""
    if t == "exit_code":
        ok = str(with_row.get("exit")) == str(a.get("expect"))
        note = f"exit={with_row.get('exit')} expect={a.get('expect')}"
    elif t == "json_field":
        field = a.get("field")
        val = with_row.get(field)
        if a.get("op") == "contains":
            ok = isinstance(val, list) and a.get("value") in val
            note = f"{field}={val}"
        else:
            expect = a.get("expect")
            ok = (str(val).lower() == str(expect).lower()) if isinstance(val, bool) or isinstance(expect, bool) else (val == expect)
            note = f"{field}={val} expect={expect}"
    else:
        note = f"unknown assert type: {t}"
    details.append({"assert": t, "field": a.get("field"), "pass": bool(ok), "note": note})
    if ok:
        passed += 1

# 加固 3：with 臂必须真实调用过 skill（否则 invalid，不算 pass）
invoked = isinstance(with_row.get("skills_invoked"), list) and skill in with_row["skills_invoked"]
invoked_valid = invoked or (skill == "")

def rate(row):
    tt = row.get("total") or 0
    return (row.get("passed") or 0) / tt if tt else 0.0

with_rate, without_rate = rate(with_row), rate(without_row)

# 加固 4：断言总数为 0 视为 fail（unknown != pass）
if total == 0:
    status = "fail"
elif passed == total and invoked_valid:
    status = "pass"
elif not invoked_valid:
    status = "invalid"   # skill 未被调用：不产生 uplift 证据
else:
    status = "fail"

strict_ok = (status == "pass") and (with_row.get("success") is True) and (with_rate > without_rate)

out = {
    "case": case.get("name"),
    "status": status,
    "passed": passed,
    "total": total,
    "skill_invoked": invoked,
    "pass_rate_with": round(with_rate, 4),
    "pass_rate_without": round(without_rate, 4),
    "strict_ok": bool(strict_ok),
    "details": details,
}
print(json.dumps(out, ensure_ascii=False))
PY
}

for y in "${CASE_YAMLS[@]}"; do
    case_dir="$(dirname "$y")"
    IFS=$'\t' read -r CNAME CSKILL CTIER CIMAGE CCTX CDF <<<"$(read_case_meta "$y")"

    # 加固 4：name 为空/缺失 → 跳过并报错（绝不拼出 OUT/ 空名导致误删根）
    if [[ -z "${CNAME}" || "${CNAME}" == "__parse_error__" || "${CNAME}" == "__missing_name__" ]]; then
        echo "error: case at ${y} has missing/invalid name, skipped" >&2
        FAILED=1
        continue
    fi
    if [[ -n "${ONLY_CASE}" && "${CNAME}" != "${ONLY_CASE}" ]]; then
        continue
    fi

    echo "==> case: ${CNAME} (tier=${CTIER})"

    run_dir="${OUT}/${CNAME}"
    if [[ -e "${run_dir}" ]]; then
        # 加固 4：旧目录改名备份而非 rm -rf，删除动作可见
        backup="${run_dir}.bak.$(date +%Y%m%d%H%M%S)"
        echo "    backup: ${run_dir} -> ${backup}"
        [[ ${DRY_RUN} -eq 1 ]] || mv "${run_dir}" "${backup}"
    fi
    [[ ${DRY_RUN} -eq 1 ]] || mkdir -p "${run_dir}"

    if [[ ${DO_BUILD} -eq 1 && -n "${CCTX}" && -n "${CDF}" ]]; then
        ctx_dir="$(cd "${case_dir}" && cd "${CCTX}" && pwd)"
        build_cmd=(docker build --network host -f "${ctx_dir}/${CDF}" -t "${CIMAGE}" "${ctx_dir}")
        echo "    build: ${build_cmd[*]}"
        if [[ ${DRY_RUN} -eq 1 ]]; then
            :
        elif ! timeout "${TIMEOUT_SEC}" "${build_cmd[@]}" >/dev/null; then
            echo "    build FAILED" >&2
            FAILED=1
            continue
        fi
    fi

    model_args=()
    [[ -n "${MODEL}" ]] && model_args=(--model "${MODEL}")
    run_cmd=("${case_dir}/run_ab.sh" --save "${run_dir}" "${CIMAGE}")
    echo "    run: ${run_cmd[*]}"
    if [[ ${DRY_RUN} -eq 1 ]]; then
        continue
    fi
    if ! timeout "${TIMEOUT_SEC}" "${run_cmd[@]}" >/dev/null 2>&1; then
        echo "    run_ab FAILED" >&2
        FAILED=1
        continue
    fi

    verdict="$(eval_case "${run_dir}/summary.json" "$y")"
    echo "    ${verdict}"

    status="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["status"])' "${verdict}")"
    strict_ok="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["strict_ok"])' "${verdict}")"
    if [[ "${status}" != "pass" ]]; then
        FAILED=1
    elif [[ ${STRICT} -eq 1 && "${strict_ok}" != "True" ]]; then
        echo "    strict: with>without not satisfied" >&2
        FAILED=1
    fi
done

echo
if [[ ${FAILED} -eq 1 ]]; then
    echo "RESULT: FAILED (see above; status fail/invalid counts as failure)"
    [[ ${LENIENT} -eq 1 ]] && exit 0
    exit 1
fi
echo "RESULT: PASSED"
exit 0
