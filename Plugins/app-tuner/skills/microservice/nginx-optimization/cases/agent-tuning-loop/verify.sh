#!/usr/bin/env bash
# 校验 agent 是否真的执行了 "baseline → 单变量 → 复测 → before/after → 保留/回退" 循环。
# P2 教训：MOCK_LOG 为结构化 JSONL（无参 baseline 记录空 argv，非空行，不丢弃）；
# config 内容已由 mock 快照嵌入日志，此处无文件竞态。
set -uo pipefail

cd /workspace
mkdir -p results

python3 - <<'PY'
import json
import os
import re

inv_path = "results/mock_invocations.jsonl"
report_path = "results/report.md"

entries = []
if os.path.exists(inv_path):
    for ln in open(inv_path, encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if not ln:
            continue  # 仅跳过物理空行；空参调用是 {"argv":[]} 合法条目，不会被丢
        try:
            e = json.loads(ln)
        except Exception:
            entries.append({"argv": ["<unparsable>"], "config_lines": []})
            continue
        e.setdefault("argv", [])
        e.setdefault("config_lines", [])
        entries.append(e)

# 从每次调用的 config 快照提取关键开关（--key=value 与 mock 的配置行语义统一）；
# 注释行（# 开头）不构成生效配置，剔除后再匹配
def flags(entry):
    lines = [l for l in (entry.get("config_lines") or []) if not l.lstrip().startswith("#")]
    low = "\n".join(lines).lower()
    return {
        "reuseport": "reuseport" in low,
        "access_log_off": "access_log off" in low,
        "sendfile_on": "sendfile on" in low,
        "affinity": "worker_cpu_affinity" in low,
    }

rounds = len(entries)
changed = []
one_variable = False
if rounds >= 2:
    a, b = flags(entries[0]), flags(entries[-1])
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    one_variable = len(changed) == 1

# 无参 baseline 必须存在且可识别（空 argv 是合法记录）
has_baseline = any(len(e.get("argv", [])) <= 1 for e in entries)

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
    "changed": changed,
    "checks": {k: bool(v) for k, v in checks.items()},
}
json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False))
PY
