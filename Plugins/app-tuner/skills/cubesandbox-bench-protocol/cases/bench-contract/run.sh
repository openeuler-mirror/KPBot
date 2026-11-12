#!/bin/bash
# Tier-A case runner: drive analyze_bench_results.py against mock cube-bench
# reports with planted latency shapes, then write results/result.json.
# Exit 0 only if every group is failure-free.
#
# Groups:
#   static   python3 -m py_compile syntax check
#   analyze  shape classification (quantized-tail / staircase /
#            uniform-lift), error warnings, multi-round aggregation,
#            --dir mtime ordering, --output file, exit codes
#
# Usage: bash cases/bench-contract/run.sh   (from anywhere)
set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"
TMP="$(mktemp -d)"
trap '[ "${BASH_SUBSHELL:-0}" -eq 0 ] && rm -rf "$TMP"' EXIT

ANALYZE="$SKILL_DIR/scripts/analyze_bench_results.py"

declare -i AN_RAN=0 AN_FAIL=0 STATIC_RAN=0 STATIC_FAIL=0

log() { echo "[case] $*"; }

check() {
    local group="$1" desc="$2" rc="$3"
    case "$group" in
        static) STATIC_RAN+=1; [ "$rc" -ne 0 ] && STATIC_FAIL+=1 ;;
        an)     AN_RAN+=1;     [ "$rc" -ne 0 ] && AN_FAIL+=1 ;;
    esac
    if [ "$rc" -ne 0 ]; then log "FAIL: $desc"; else log "ok: $desc"; fi
}

# ---------------- static checks ----------------
python3 -m py_compile "$ANALYZE"
check static "analyze_bench_results.py py_compile" $?

# ---------------- mock cube-bench reports ----------------
MOCK="$TMP/reports"
mkdir -p "$MOCK"

python3 - "$MOCK" <<'PYEOF'
import json
import random
import sys
import time

out = sys.argv[1]
random.seed(7)


def report(samples, total_time_s, errors=0, successful=None):
    ok = len(samples)
    if successful is None:
        successful = ok + errors
    return {
        "timestamp": "2026-11-07T00:00:00Z",
        "config": {
            "template": "tpl-mock",
            "concurrency": 100,
            "total": successful + errors,
            "warmup": 2,
            "mode": "create-only",
        },
        "summary": {
            "total_time_s": total_time_s,
            "successful": successful,
            "errors": errors,
            "success_rate": successful / (successful + errors),
            "throughput_qps": successful / total_time_s,
        },
        "create": {
            "count": ok,
            "min": min(samples),
            "max": max(samples),
            "avg": sum(samples) / ok,
            "std": 5.0,
            "p50": sorted(samples)[ok // 2],
            "p90": sorted(samples)[int(ok * 0.9)],
            "p95": sorted(samples)[int(ok * 0.95)],
            "p99": sorted(samples)[int(ok * 0.99)],
        },
        "raw": [{"seq": i, "create_ms": v} for i, v in enumerate(samples)],
    }


def dump(name, data):
    with open("%s/%s" % (out, name), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    time.sleep(0.02)  # distinct mtimes for --dir ordering


# quantized-tail: bulk ~137ms, a narrow 601-602ms cluster (5 of 100)
bulk = [137 + random.uniform(-10, 10) for _ in range(95)]
tail = [601.0, 601.0, 601.5, 602.0, 602.0]
dump("quantized.json", report(bulk + tail, 0.31))

# staircase: half ~100ms, half ~400ms (queueing layers)
stairs = [100 + random.uniform(-5, 5) for _ in range(50)] + \
         [400 + random.uniform(-10, 10) for _ in range(50)]
dump("staircase.json", report(stairs, 0.42))

# uniform-lift: everything within a narrow 200-220ms band
uniform = [200 + random.uniform(0, 20) for _ in range(100)]
dump("uniform.json", report(uniform, 0.25))

# errors report: 12 failures of 200 -> success_rate 0.94
err_bulk = [140 + random.uniform(-8, 8) for _ in range(188)]
err_raw = ([{"seq": i, "create_ms": v} for i, v in enumerate(err_bulk)] +
           [{"seq": 188 + i, "create_ms": 0, "error": "no more resource"}
            for i in range(12)])
dump("errors.json", {
    "config": {"concurrency": 50, "total": 200, "warmup": 2,
               "mode": "create-only", "template": "tpl-mock"},
    "summary": {"total_time_s": 1.1, "successful": 188, "errors": 12,
                "success_rate": 0.94, "throughput_qps": 170.9},
    "create": {"count": 188, "min": min(err_bulk),
               "max": max(err_bulk), "avg": sum(err_bulk) / 188,
               "std": 5.0, "p50": 140.0, "p90": 146.0, "p95": 147.0,
               "p99": 148.0},
    "raw": err_raw,
})

# three stable rounds for aggregation
for index, total in enumerate((0.30, 0.31, 0.29)):
    samples = [137 + random.uniform(-10, 10) for _ in range(100)]
    dump("round_%d.json" % index, report(samples, total))

# same scenario, different template: template A-vs-B comparison must
# never be pooled into one group (review !114 P2)
samples = [137 + random.uniform(-10, 10) for _ in range(100)]
other = report(samples, 0.33)
other["config"]["template"] = "tpl-other"
dump("round_other_tpl.json", other)

# non-bench json in the dir: array root must degrade to an entry error,
# never crash the whole verdict (review !114 P2)
with open("%s/broken.json" % out, "w", encoding="utf-8") as handle:
    json.dump([1, 2, 3], handle)
time.sleep(0.02)

# report with non-object summary block: schema guard must reject it
with open("%s/badsummary.json" % out, "w", encoding="utf-8") as handle:
    json.dump({"summary": [1, 2], "raw": []}, handle)
time.sleep(0.02)

# report with a non-numeric success_rate: must degrade to a warning,
# never crash the batch (review !114 P2)
with open("%s/badrate.json" % out, "w", encoding="utf-8") as handle:
    json.dump({
        "config": {"concurrency": 10, "total": 5, "warmup": 0,
                   "mode": "create-only", "template": "tpl-mock"},
        "summary": {"total_time_s": 0.9, "successful": 5, "errors": 0,
                    "success_rate": "0.94", "throughput_qps": 5.5},
        "create": {"count": 5, "min": 100.0, "max": 150.0, "avg": 120.0,
                   "std": 5.0, "p50": 120.0, "p90": 140.0, "p95": 145.0,
                   "p99": 150.0},
        "raw": [{"seq": i, "create_ms": 120.0 + i} for i in range(5)],
    }, handle)
time.sleep(0.02)
PYEOF

# ---------------- analyze contract ----------------
while IFS= read -r line; do
    rc="${line%% *}"
    desc="${line#* }"
    check an "$desc" "$rc"
done < <(python3 - "$ANALYZE" "$MOCK" "$TMP" <<'PYEOF'
import json
import os
import subprocess
import sys

analyze, mock, tmp = sys.argv[1], sys.argv[2], sys.argv[3]
outcomes = []


def record(desc, ok):
    outcomes.append((desc, 0 if ok else 1))


def run(extra):
    return subprocess.run(
        [sys.executable, analyze] + extra,
        capture_output=True, text=True)


def verdict_of(args_list):
    proc = run(args_list)
    try:
        return proc, json.loads(proc.stdout)
    except ValueError:
        return proc, None


proc, data = verdict_of(["--files", mock + "/quantized.json"])
record("quantized report classifies as quantized-tail",
       data is not None
       and data["files"][0]["shape"] == "quantized-tail"
       and data["files"][0]["shape_evidence"].get("tail_lift", 0) >= 1.5)

proc, data = verdict_of(["--files", mock + "/staircase.json"])
record("staircase report classifies as staircase",
       data is not None and data["files"][0]["shape"] == "staircase")

proc, data = verdict_of(["--files", mock + "/uniform.json"])
record("uniform report classifies as uniform-lift",
       data is not None and data["files"][0]["shape"] == "uniform-lift")

proc, data = verdict_of(["--files", mock + "/errors.json"])
record("error report carries warnings and is not judged failure-free",
       data is not None
       and any("errors" in w for w in data["files"][0]["warnings"])
       and any("success_rate" in w for w in data["files"][0]["warnings"]))

proc, data = verdict_of(["--files",
                         mock + "/round_0.json",
                         mock + "/round_1.json",
                         mock + "/round_2.json"])
rounds = (data or {}).get("rounds") or {}
groups = rounds.get("groups") or []
group = groups[0] if groups else {}
record("three same-scenario rounds aggregate into one group",
       rounds.get("total_files") == 3
       and len(groups) == 1
       and group.get("n") == 3
       and group.get("rounds_sufficient") is True
       and isinstance(group.get("median_total_time"), float)
       and "stability" in group
       and group["stability"] == "stable"
       and group["cv_total_time"] <= 0.10)

proc, data = verdict_of(["--files",
                         mock + "/errors.json",
                         mock + "/round_0.json",
                         mock + "/round_1.json",
                         mock + "/round_2.json"])
rounds = (data or {}).get("rounds") or {}
groups = rounds.get("groups") or []
record("mixed scenarios aggregate per group, never pooled",
       len(groups) == 2
       and {g["scenario"]["concurrency"] for g in groups} == {50, 100}
       and all(g["n"] in (1, 3) for g in groups))
record("single-round groups are flagged insufficient, never stable",
       all(g["rounds_sufficient"] is False
           and "insufficient rounds" in g["stability"]
           and "fewer than 3" in g["median_total_time_note"]
           for g in groups if g["n"] == 1))

proc, data = verdict_of(["--files",
                         mock + "/round_0.json",
                         mock + "/round_other_tpl.json"])
rounds = (data or {}).get("rounds") or {}
groups = rounds.get("groups") or []
record("same scenario with different templates stays two groups",
       len(groups) == 2
       and {g["scenario"]["template"] for g in groups}
       == {"tpl-mock", "tpl-other"}
       and all(g["n"] == 1 for g in groups))
record("template A/B single-round groups carry insufficient markers",
       all(g["rounds_sufficient"] is False
           and g["stability"] != "stable"
           for g in groups))

proc, data = verdict_of(["--files",
                         mock + "/round_0.json",
                         mock + "/round_1.json"])
rounds = (data or {}).get("rounds") or {}
groups = rounds.get("groups") or []
record("two-round group is insufficient too (n=2 < 3)",
       len(groups) == 1
       and groups[0]["n"] == 2
       and groups[0]["rounds_sufficient"] is False
       and "insufficient rounds (n=2)" in groups[0]["stability"])

proc, data = verdict_of(["--files", mock + "/badrate.json"])
record("non-numeric success_rate degrades to a warning, no crash",
       proc.returncode == 0
       and (data or {}).get("files")
       and any("non-numeric" in w
               for w in data["files"][0]["warnings"]))

proc = run(["--dir", mock])
try:
    data = json.loads(proc.stdout)
except ValueError:
    data = None
files = (data or {}).get("files") or []
record("--dir picks up all 11 reports (9 good + broken + badsummary) by mtime",
       proc.returncode == 3 and len(files) == 11
       and files[0]["file"].endswith("quantized.json")
       and files[-1]["file"].endswith("badrate.json"))
record("array-root json degrades to entry error, verdict still complete",
       len(files) >= 3
       and files[-3].get("error", "").startswith("unsupported schema")
       and "root is list" in files[-3].get("error", ""))
record("non-object summary rejected by schema guard",
       len(files) >= 2
       and "summary is list" in files[-2].get("error", ""))
record("top-level errors list summarizes the two bad files",
       (data or {}).get("errors") is not None
       and len(data["errors"]) == 2
       and {e["file"].replace("\\", "/").rsplit("/", 1)[-1]
            for e in data["errors"]}
       == {"broken.json", "badsummary.json"})
record("good files still judged despite broken neighbours",
       files and files[0].get("shape") == "quantized-tail"
       and len([e for e in (data or {}).get("errors", [])]) == 2)

# first pass writes a verdict into the dir; second pass must not treat
# that verdict as a bench report (verify via the written file: --output
# mode prints nothing to stdout)
verdict_in_dir = mock + "/verdict.json"
run(["--dir", mock, "--output", verdict_in_dir])
proc = run(["--dir", mock, "--output", verdict_in_dir])
try:
    with open(verdict_in_dir, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    data = None
record("--dir excludes the --output verdict file from analysis",
       (data or {}).get("files") is not None
       and len(data["files"]) == 11)

# missing file mixed with a good one: exit 3, errors summarized,
# the good report is still judged
proc = run(["--files", mock + "/quantized.json",
            mock + "/no-such-report.json"])
try:
    data = json.loads(proc.stdout)
except ValueError:
    data = None
record("missing file mixed with good one exits 3 with errors summary",
       proc.returncode == 3 and (data or {}).get("errors")
       and any("no-such-report" in e["file"] for e in data["errors"])
       and data["files"][0].get("shape") == "quantized-tail")

out_json = tmp + "/verdict.json"
proc = run(["--files", mock + "/uniform.json", "--output", out_json])
try:
    with open(out_json, encoding="utf-8") as handle:
        data_file = json.load(handle)
except (OSError, ValueError):
    data_file = None
record("--output writes parseable utf-8 verdict",
       data_file is not None
       and data_file["files"][0]["shape"] == "uniform-lift"
       and data_file["notes"])

proc = run(["--files", mock + "/uniform.json", "--dir", mock])
record("--files and --dir are mutually exclusive (non-zero exit)",
       proc.returncode != 0)

proc = run([])
record("missing source args exits non-zero", proc.returncode != 0)

proc = run(["--dir", tmp + "/no-such-dir"])
record("bad --dir exits non-zero", proc.returncode != 0)

proc = subprocess.run([sys.executable, analyze, "--help"],
                      capture_output=True, text=True)
record("--help exits 0", proc.returncode == 0)

for desc, rc in outcomes:
    print("%d %s" % (rc, desc))
PYEOF
)

# ---------------- result.json ----------------
SUCCESS=false
if [ "$AN_FAIL" -eq 0 ] && [ "$STATIC_FAIL" -eq 0 ] && [ "$AN_RAN" -gt 0 ]; then
    SUCCESS=true
fi

python3 - "$RESULTS_DIR/result.json" "$SUCCESS" "$AN_RAN" "$AN_FAIL" \
    "$STATIC_RAN" "$STATIC_FAIL" <<'EOF'
import json
import sys

(out_path, success, an_ran, an_fail, static_ran, static_fail) = sys.argv[1:]
result = {
    "success": success == "true",
    "analyze_ran": int(an_ran),
    "analyze_failures": int(an_fail),
    "static_ran": int(static_ran),
    "static_failures": int(static_fail),
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
EOF

log "result: $RESULTS_DIR/result.json"
if [ "$SUCCESS" = true ]; then
    exit 0
fi
exit 1
