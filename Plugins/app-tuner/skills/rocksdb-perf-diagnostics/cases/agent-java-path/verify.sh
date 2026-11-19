#!/usr/bin/env bash
# 校验 agent 产出的 results/answer.md 是否覆盖 Java 路径的关键点。
set -uo pipefail

cd /workspace
mkdir -p results

python3 - <<'PY'
import json
import os
import re

path = "results/answer.md"
text = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""

checks = [
    ("perf_context 不可用并给出替代",
     bool(re.search(r"perf_context", text)) and
     bool(re.search(r"(没有|不可用|无绑定|未绑定|不支持|no binding)", text))),
    ("提到 Java Statistics",
     "Statistics" in text and any(k in text for k in
                                  ("getTickerCount", "getHistogramData", "StatsLevel", "TickerType"))),
    ("提到 Java property API",
     any(k in text for k in ("getProperty", "getLongProperty", "getMapProperty"))),
    ("提到 EventListener/AbstractEventListener",
     "EventListener" in text or "AbstractEventListener" in text),
    ("提到 ColumnFamilyMetaData",
     "ColumnFamilyMetaData" in text or "getColumnFamilyMetaData" in text),
    ("提到 ThreadList/ThreadStatus",
     "getThreadList" in text or "ThreadStatus" in text),
    ("说明 LOG 与语言无关",
     "LOG" in text or "日志" in text),
]

results = [{"name": n, "passed": bool(ok)} for n, ok in checks]
passed = sum(1 for r in results if r["passed"])
rate = passed / len(results) if results else 0.0
success = passed == len(results)

json.dump(
    {"success": success, "assertion_pass_rate": round(rate, 4), "checks": results},
    open("results/result.json", "w"),
    ensure_ascii=False,
    indent=2,
)
PY

python3 -c 'import json,sys;sys.exit(0 if json.load(open("results/result.json"))["success"] else 1)'
