#!/usr/bin/env bash
# collect-mock-service 用例驱动：用 python stdlib mock 的 pprof 服务验证 pprof_collect.sh
# 用法: bash run.sh
set -euo pipefail

case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
collect_script="$case_dir/../../scripts/pprof_collect.sh"
mock_server="$case_dir/fixtures/mock_pprof_server.py"
results_dir="$case_dir/results"
mkdir -p "$results_dir"

port=16060
out_dir=$(mktemp -d)
server_pid=""
cleanup() {
    [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
    rm -rf "$out_dir"
}
trap cleanup EXIT

python3 "$mock_server" "$port" &
server_pid=$!
for _ in $(seq 1 50); do
    curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:$port/debug/pprof/" && break
    sleep 0.1
done

WORK="$out_dir" RESULTS_DIR="$results_dir" PORT="$port" COLLECT="$collect_script" \
python3 - <<'EOF'
import glob, json, os, subprocess, sys

port = os.environ["PORT"]
out_dir = os.environ["WORK"]
results_dir = os.environ["RESULTS_DIR"]
collect = os.environ["COLLECT"]

checks = []
def check(name, cond, detail=""):
    checks.append({"name": name, "pass": bool(cond), "detail": str(detail)})

# 正常路径：采集全部 5 类产物
run = subprocess.run(["bash", collect, f"127.0.0.1:{port}", out_dir, "1"],
                     capture_output=True, text=True)
check("collect exit code == 0", run.returncode == 0, run.stderr.strip()[:200])

expected = {
    "cpu-*.pprof": b"FAKE-CPU-PROFILE",
    "heap-*.pprof": b"FAKE-HEAP-PROFILE",
    "allocs-*.pprof": b"FAKE-ALLOCS-PROFILE",
    "goroutine-*.txt": b"FAKE-GOROUTINE-STACK",
    "trace-*.out": b"FAKE-TRACE",
}
for pattern, payload in expected.items():
    matches = glob.glob(os.path.join(out_dir, pattern))
    check(f"artifact {pattern} exists", len(matches) == 1)
    if matches:
        content = open(matches[0], "rb").read()
        check(f"artifact {pattern} content matches mock payload", payload in content)

# 失败路径：端口未监听应立即报错退出
bad = subprocess.run(["bash", collect, "127.0.0.1:1", out_dir, "1"],
                     capture_output=True, text=True)
check("unreachable target exits non-zero", bad.returncode != 0)
check("unreachable target prints diagnostic", "pprof" in (bad.stderr + bad.stdout))

passed = sum(1 for c in checks if c["pass"])
result = {
    "success": passed == len(checks),
    "checks_total": len(checks),
    "checks_passed": passed,
    "failed_checks": [c for c in checks if not c["pass"]],
}
with open(os.path.join(results_dir, "result.json"), "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print(f"checks: {passed}/{len(checks)} passed -> {results_dir}/result.json")
if result["failed_checks"]:
    print(json.dumps(result["failed_checks"], indent=2, ensure_ascii=False))
sys.exit(0 if result["success"] else 1)
EOF
