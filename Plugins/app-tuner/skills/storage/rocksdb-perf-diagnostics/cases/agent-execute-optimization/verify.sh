#!/usr/bin/env bash
# 校验 agent 是否真的执行了 "baseline → 单变量 → 复测 → before/after → 结论" 循环。
set -uo pipefail

cd /workspace
mkdir -p results

python3 - <<'PY'
import json
import os
import re

inv_path = "results/mock_invocations.log"
report_path = "results/report.md"

lines = []
if os.path.exists(inv_path):
    lines = [ln.strip() for ln in open(inv_path, encoding="utf-8", errors="replace") if ln.strip()]

SKIP = {"--out", "--statistics", "--histogram", "--perf_level"}


def params(line):
    d = {}
    for tok in line.split():
        if tok.startswith("--") and "=" in tok:
            k, v = tok.split("=", 1)
            if k in SKIP:
                continue
            d[k] = v
    return d


one_variable = False
changed = []
if len(lines) >= 2:
    a, b = params(lines[0]), params(lines[1])
    keys = set(a) | set(b)
    changed = sorted(k for k in keys if a.get(k) != b.get(k))
    one_variable = len(changed) == 1

text = ""
if os.path.exists(report_path):
    text = open(report_path, encoding="utf-8", errors="replace").read()

has_before_after = bool(re.search(r"(before|baseline|改动前)", text, re.I)) and bool(
    re.search(r"(after|对照|复测|改动后)", text, re.I)
)
has_metrics = ("stall" in text.lower()) and (
    "p99" in text.lower() or "throughput" in text.lower()
)
has_conclusion = bool(re.search(r"(保留|keep|回退|revert)", text, re.I))

success = one_variable and has_before_after and has_metrics and has_conclusion

json.dump(
    {
        "success": success,
        "one_variable": one_variable,
        "changed_param": changed[0] if len(changed) == 1 else None,
        "invocations": len(lines),
        "report_has_before_after": has_before_after,
        "report_has_metrics": has_metrics,
        "report_has_conclusion": has_conclusion,
    },
    open("results/result.json", "w"),
    ensure_ascii=False,
    indent=2,
)
PY

python3 -c 'import json,sys;sys.exit(0 if json.load(open("results/result.json"))["success"] else 1)'
