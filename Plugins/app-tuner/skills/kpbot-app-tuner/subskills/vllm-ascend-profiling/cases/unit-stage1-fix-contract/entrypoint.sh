#!/usr/bin/env bash
# tier A 执行器：跑 unittest，写 results/result.json，退出码即用例结论。
# 依赖：python3 标准库（unittest/mock）。无 docker、无网络、无 NPU。
set -uo pipefail

cd "$(dirname "$0")"
mkdir -p results

# 被测代码统一从 staging 取（容器内）；宿主机直跑时退回被测 skill 目录
if [ -d /staging/skill ]; then
    SKILL_DIR=/staging/skill
else
    SKILL_DIR="$(cd ../.. && pwd)"
fi
export SKILL_DIR

python3 -m unittest discover -s tests -v > results/unittest.log 2>&1
rc=$?

python3 - "$rc" <<'PY'
import json, re, shutil, sys
rc = int(sys.argv[1])
log = open("results/unittest.log", encoding="utf-8", errors="replace").read()
m = re.search(r"Ran (\d+) tests?", log)
total = int(m.group(1)) if m else 0
fail_n = sum(1 for l in log.splitlines() if l.strip().startswith("FAIL:"))
err_n = sum(1 for l in log.splitlines() if l.strip().startswith("ERROR:"))
json.dump({
    "success": rc == 0,
    "tier": "A",
    "tests_run": total,
    "tests_passed": max(0, total - fail_n - err_n),
    "tests_failed": fail_n,
    "tests_errors": err_n,
}, open("results/result.json", "w"), indent=2)
PY

echo "[entrypoint] unittest rc=${rc}"
exit "$rc"
