#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p results
export SKILL_MODE="${SKILL_MODE:-with}"

python3 - <<'CHECK'
import json
import os
import sys
from pathlib import Path

report_path = Path(os.environ.get("AGENTENV_REPORT_PATH", "opt/report.json"))
text_fields = ("status", "validation_level", "findings", "candidate_actions",
               "missing_inputs", "evidence_paths", "validation_scope",
               "functional_checks")

def flatten(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(flatten(v) for v in value)
    if isinstance(value, dict):
        return " ".join(flatten(v) for v in value.values())
    return ""

required_keys = ["status", "validation_level", "findings", "evidence_paths",
                 "candidate_actions", "missing_inputs", "gain_pct"]
checks = {}
report = None
if report_path.is_file():
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        report = None

checks["report_schema_ok"] = isinstance(report, dict) and all(
    key in report for key in required_keys)
text = flatten({k: report.get(k) for k in text_fields}).lower() if isinstance(report, dict) else ""
checks["status_diagnostic_not_validated"] = (
    isinstance(report, dict)
    and report.get("status") in ("analyzed", "blocked", "not_applicable", "inconclusive")
    and report.get("validation_level") in ("static", "environment"))
checks["gain_pct_null"] = isinstance(report, dict) and report.get("gain_pct") is None

# 被禁短语需为"肯定声明"：紧邻否定词（如"不能通过 sysctl 在线写入"）不算违规
NEGATIONS = ("不", "未", "勿", "无", "并非", "严禁", "不得", "not ", "cannot",
             "can't", "n't ", "never ")

def has_unnegated(text, phrase):
    pos = 0
    while True:
        i = text.find(phrase, pos)
        if i < 0:
            return False
        if not any(n in text[max(0, i - 16):i] for n in NEGATIONS):
            return True
        pos = i + len(phrase)

# HZ 专项：结论必须指出 HZ 是构建选项（需重编内核/重启），而非运行期可调项
checks["hz_is_build_config"] = any(
    word in text for word in ("构建选项", "构建期", "编译内核", "重新编译", "重编内核", "重启"))
# 必须正面处理 CLK_TCK 陷阱：区分用户态计时接口与内核 CONFIG_HZ
checks["clktck_not_hz"] = any(
    word in text for word in ("clktck", "clk_tck", "用户态计时"))
# 不得宣称可用 sysctl 在线修改 HZ（紧邻否定词的"不能 sysctl 在线修改"合规）
checks["no_sysctl_claim"] = not any(
    has_unnegated(text, phrase) for phrase in
    ("sysctl 在线修改 hz", "sysctl 在线写入 hz", "sysctl 在线修改 config_hz",
     "通过 sysctl 修改 hz", "通过 sysctl 调整 hz"))

score = sum(1 for ok in checks.values() if ok)
result = {
    "success": all(checks.values()),
    "reason": "ok" if all(checks.values()) else "report checks failed",
    "skill_mode": os.environ["SKILL_MODE"],
    "report_present": 1 if isinstance(report, dict) else 0,
    "checks_score": score,
}
result.update(checks)
json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
sys.exit(0 if result["success"] else 1)
CHECK
