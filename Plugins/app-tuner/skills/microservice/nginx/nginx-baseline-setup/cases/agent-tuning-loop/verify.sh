#!/usr/bin/env bash
# 校验 agent 是否真的执行了 "baseline → 单变量 → 复测 → before/after → 保留/回退" 循环。
#
# P2 教训：MOCK_LOG 为结构化 JSONL（无参 baseline 记录 [<脚本路径>]，长度 1）；
# baseline 判定基于「调用语义」（config_lines 为空或所有调优开关均为 false），
# 不依赖 argv 长度——传 --config 的合格 baseline 也必须是 baseline。
# config 内容已由 mock 快照嵌入日志，此处无文件竞态。
set -uo pipefail

ROOT="${VERIFY_ROOT:-/workspace}"
cd "${ROOT}"
mkdir -p results
# fail-closed：先清掉任何预置结果，verify 未产出 result.json 时不得沿用旧内容。
rm -f results/result.json

python3 - <<'PY'
import json
import os
import re

inv_path = "work/mock_invocations.jsonl"
report_path = "work/report.md"

entries = []
if os.path.exists(inv_path):
    for ln in open(inv_path, encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if not ln:
            continue  # 仅跳过物理空行
        try:
            e = json.loads(ln)
        except Exception:
            entries.append({"argv": ["<unparsable>"], "config_lines": []})
            continue
        if not isinstance(e, dict):
            entries.append({"argv": ["<invalid>"], "config_lines": []})
            continue
        if not isinstance(e.get("argv"), list):
            e["argv"] = []
        if not isinstance(e.get("config_lines"), list):
            e["config_lines"] = []
        entries.append(e)

# 从每次调用的 config 快照提取关键开关；注释行不构成生效配置。
def flags(entry):
    lines = [l for l in (entry.get("config_lines") or []) if not l.lstrip().startswith("#")]
    low = "\n".join(lines).lower()
    return {
        "reuseport": "reuseport" in low,
        "access_log_off": "access_log off" in low,
        "sendfile_on": "sendfile on" in low,
        "affinity": "worker_cpu_affinity" in low,
    }

# baseline 语义：没有 config 快照，或 config 中一个调优开关都未开启。
# 这样「写 base.conf 并传 --config」的合格 baseline 不会被误杀。
def is_baseline(entry):
    lines = [l for l in (entry.get("config_lines") or []) if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return True
    return not any(flags(entry).values())

rounds = len(entries)
baseline_idx = next((i for i, e in enumerate(entries) if is_baseline(e)), None)
has_baseline = baseline_idx is not None and baseline_idx < rounds - 1

changed = []
one_variable = False
if has_baseline:
    a, b = flags(entries[baseline_idx]), flags(entries[-1])
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    one_variable = len(changed) == 1

text = ""
if os.path.exists(report_path):
    text = open(report_path, encoding="utf-8", errors="replace").read()
low = text.lower()

has_before_after = bool(re.search(r"(before|baseline|改动前|基线)", low)) and bool(
    re.search(r"(after|对照|复测|改动后)", low)
)
has_conclusion = ("保留" in text) or ("回退" in text) or ("keep" in low) or ("rollback" in low)
has_rps = "requests/sec" in low or "rps" in low

checks = {
    "rounds_ge_2": rounds >= 2,
    "has_baseline": has_baseline,
    "one_variable": one_variable,
    "before_after_in_report": has_before_after,
    "keep_rollback_conclusion": has_conclusion,
    "metric_in_report": has_rps,
}
passed = sum(1 for v in checks.values() if v)

result = {
    "success": passed == len(checks),
    "passed": passed,
    "total": len(checks),
    "one_variable": one_variable,
    "rounds": rounds,
    "baseline_round": baseline_idx,
    "changed": changed,
    "checks": {k: bool(v) for k, v in checks.items()},
}
json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False))
PY
