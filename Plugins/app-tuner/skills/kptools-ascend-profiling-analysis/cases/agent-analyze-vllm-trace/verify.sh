#!/usr/bin/env bash
# 对照 input/ground_truth.json 校验 opt/answer.json（每字段容差 ±TOLERANCE_PP 百分点）
set -uo pipefail

cd /workspace
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}"
TOL="${TOLERANCE_PP:-0.5}"

if [ ! -f input/ground_truth.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"ground_truth.json missing","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

if ! python3 -c 'import json; json.load(open("opt/answer.json"))' 2>/dev/null; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/answer.json is not valid JSON","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

export SKILL_MODE TOL

python3 - <<'PY'
import json, os

gt = json.load(open("input/ground_truth.json"))
try:
    ans = json.load(open("opt/answer.json"))
except Exception:
    ans = {}

FIELDS = ("computing", "communication", "comm_not_overlapped", "free")
tol = float(os.environ["TOL"])
questions, n_pass, details = {}, 0, {}
for q, truth in gt.items():
    got = ans.get(q) or {}
    errs, ok = {}, True
    for f in FIELDS:
        t = float(truth[f])
        g = got.get(f)
        if g is None:
            errs[f] = "missing"
            ok = False
            continue
        try:
            e = abs(float(g) - t)
        except (TypeError, ValueError):
            errs[f] = "non-numeric"
            ok = False
            continue
        errs[f] = round(e, 4)
        if e > tol:
            ok = False
    questions[q] = ok
    details[q] = {"errors_pp": errs}
    n_pass += ok

total = len(gt)
analysis_correct = int(n_pass == total)
success = analysis_correct == 1
json.dump({
    "success": success,
    "reason": "ok" if success else f"{total - n_pass}/{total} question(s) wrong",
    "skill_mode": os.environ["SKILL_MODE"],
    "analysis_correct": analysis_correct,
    "n_questions_passed": n_pass,
    "n_questions_total": total,
    "tolerance_pp": tol,
    "questions": questions,
    "details": details,
}, open("results/result.json", "w"), ensure_ascii=False, indent=2)
PY

rc=0
[ "$(python3 -c 'import json;print(json.load(open("results/result.json"))["success"])')" = "True" ] || rc=1
exit ${rc}
