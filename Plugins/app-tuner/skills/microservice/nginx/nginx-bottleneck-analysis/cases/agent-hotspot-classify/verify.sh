#!/usr/bin/env bash
# 校验 agent 是否按判定树给出两场景的正确判定与路由（含量化依据）。
set -uo pipefail
cd /workspace
mkdir -p results
python3 - <<'PY'
import json, os, re

text = ""
if os.path.exists("results/report.md"):
    text = open("results/report.md", encoding="utf-8", errors="replace").read()
low = text.lower()

def has_any(*words):
    return any(w in text or w in low for w in words)

checks = {
    # 场景 A：中断不均（判定树第 4 类）
    "scenario_a_irq": has_any("中断不均") and ("45" in text or "80%" in text or "4" in text and "类" in text),
    "scenario_a_threshold": bool(re.search(r"20%|50%|3倍|3×|三倍", text)),
    "scenario_a_route_env": has_any("nginx-env-tuning", "环境级", "IRQ 绑核", "irq 绑核", "多队列"),
    # 场景 B：TLS 握手（判定树第 1 类）
    "scenario_b_tls": has_any("TLS 握手", "tls", "SSL", "ssl") and has_any("记录层", "握手"),
    "scenario_b_threshold": bool(re.search(r"20%|35%|18", text)),
    "scenario_b_route_conf_first": (has_any("nginx-conf-tuning", "配置级") and
                                    (has_any("证书") or has_any("ECDSA") or has_any("TLS1.3", "TLS 1.3", "tls1.3", "session"))),
    "structured": has_any("场景") and has_any("路由") and has_any("判定"),
}
passed = sum(1 for v in checks.values() if v)
result = {
    "success": passed == len(checks),
    "passed": passed,
    "total": len(checks),
    "checks": {k: bool(v) for k, v in checks.items()},
}
json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False))
PY
