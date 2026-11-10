#!/usr/bin/env python3
"""Tier-A contract tests for allocation-path-optimization.

Groups:

  scripts   alloc_size_audit.py: floor inflation, floor-hit share, bytes per op,
            model-vs-measurement error, all three recommendation branches,
            jemalloc size classes, JSON input.
            alloc_probe.c: compiled when a C compiler is available and checked
            against an exactly known allocation sequence (skipped otherwise).
  skill     SKILL.md invariants: frontmatter, five recipes, no library-name
            routing, allocation invariants documented, referenced assets exist.
  contract  output object carries allocation_result and the fields downstream
            stages read.

Prints "PYTEST_SUMMARY: {json}" on the last line.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(SKILL_DIR, "scripts")

results = []
skipped = []


def record(group, name, ok, detail=""):
    results.append({"group": group, "name": name, "ok": bool(ok), "detail": detail})
    print(("ok   " if ok else "FAIL ") + f"[{group}] {name}" + (f" :: {detail}" if detail and not ok else ""))


def skip(group, name, why):
    skipped.append({"group": group, "name": name, "why": why})
    print(f"skip [{group}] {name} :: {why}")


def run_py(script, args):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + args,
                          capture_output=True, text=True)


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_size_audit(tmp):
    # The canonical case: 64-byte metadata requests behind a 1024-byte floor.
    sizes = write(tmp, "sizes.txt", "\n".join(["64"] * 250048))
    proc = run_py("alloc_size_audit.py", [
        "--sizes", sizes, "--floor", "1024", "--op-count", "200000",
        "--actual-bytes-per-op", "1280.04", "--json",
    ])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "audit computes 16x floor inflation",
           abs(data.get("inflation_ratio", 0) - 16.0) < 1e-9, str(data.get("inflation_ratio")))
    record("scripts", "audit reports every request hitting the floor",
           abs(data.get("floor_hit_share", 0) - 1.0) < 1e-9, str(data.get("floor_hit_share")))
    record("scripts", "audit derives bytes per operation",
           abs(data.get("requested_bytes_per_op", 0) - 80.02) < 0.05,
           str(data.get("requested_bytes_per_op")))
    record("scripts", "audit cross-checks the model against measurement",
           data.get("model_error") is not None and data["model_error"] < 0.01,
           str(data.get("model_error")))
    record("scripts", "audit recommends exact size allocation",
           data.get("recommendation") == "exact_size_allocation",
           str(data.get("recommendation")))

    # Mixed sizes below the floor: still exact-size candidates.
    mixed = write(tmp, "mixed.txt", "\n".join(["64"] * 50 + ["96"] * 50))
    proc = run_py("alloc_size_audit.py", ["--sizes", mixed, "--floor", "1024", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "audit treats multiple sub-floor sizes as candidates",
           data.get("recommendation") == "exact_size_allocation",
           str(data.get("recommendation")))

    # Requests above the floor: no floor inflation, different recommendation.
    above = write(tmp, "above.txt", "\n".join(["2048"] * 10))
    proc = run_py("alloc_size_audit.py", ["--sizes", above, "--floor", "1024", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "audit reports no floor inflation above the floor",
           data.get("recommendation") == "no_floor_inflation",
           str(data.get("recommendation")))

    # Floor hit but the aggregate win is small -> reject.
    weak = write(tmp, "weak.txt", "\n".join(["1000"] * 10 + ["4096"] * 990))
    proc = run_py("alloc_size_audit.py", ["--sizes", weak, "--floor", "1024", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "audit rejects when aggregate inflation is small",
           data.get("recommendation") == "reject", str(data.get("recommendation")))

    # jemalloc size classes differ from glibc, and the audit must say which.
    proc = run_py("alloc_size_audit.py", ["--sizes", sizes, "--floor", "1024",
                                          "--allocator", "jemalloc", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    chunks = {row["allocator_chunk"] for row in data.get("histogram", [])}
    record("scripts", "audit applies jemalloc size classes",
           data.get("allocator") == "jemalloc" and chunks == {1024}, str(chunks))

    # JSON count input.
    counts = write(tmp, "counts.json", json.dumps({"64": 100, "128": 20}))
    proc = run_py("alloc_size_audit.py", ["--sizes", counts, "--floor", "1024", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "audit accepts JSON count maps",
           data.get("total_requests") == 120, str(data.get("total_requests")))

    proc = run_py("alloc_size_audit.py", ["--sizes", os.path.join(tmp, "missing.txt"), "--json"])
    record("scripts", "audit fails on missing input", proc.returncode != 0)


PROBE_PROGRAM = r"""
#include <stdlib.h>
#include <string.h>
int main(void)
{
    void *a = malloc(1000);       /* one 1000-byte request */
    void *b = malloc(64);         /* one 64-byte request   */
    void *c = calloc(4, 16);      /* one 64-byte request   */
    if (!a || !b || !c) return 1;
    free(b);
    a = realloc(a, 2000);         /* one 2000-byte request */
    if (!a) return 1;
    free(a);
    free(c);
    return 0;
}
"""


def test_alloc_probe(tmp):
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        skip("scripts", "alloc_probe compiles and reports exact bytes", "no C compiler")
        return
    prog = write(tmp, "prog.c", PROBE_PROGRAM)
    exe = os.path.join(tmp, "prog")
    compile_cmd = [cc, "-O2", "-I", SCRIPTS, prog, os.path.join(SCRIPTS, "alloc_probe.c"),
                   "-Wl,--wrap=malloc", "-Wl,--wrap=calloc",
                   "-Wl,--wrap=realloc", "-Wl,--wrap=free", "-o", exe]
    proc = subprocess.run(compile_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        record("scripts", "alloc_probe compiles", False, proc.stderr[-300:])
        return
    record("scripts", "alloc_probe compiles", True)
    run = subprocess.run([exe], capture_output=True, text=True)
    record("scripts", "alloc_probe program runs", run.returncode == 0, run.stderr[-200:])
    report = {}
    for token in run.stderr.split():
        if "=" in token:
            key, _, value = token.partition("=")
            report[key] = value
    # 1000 + 64 + 64 + 2000 bytes requested across 4 allocations; 4 frees because
    # the probe counts the free implied by realloc in addition to the explicit ones.
    record("scripts", "alloc_probe counts requested bytes exactly",
           report.get("bytes") == "3128", str(report))
    record("scripts", "alloc_probe counts allocations exactly",
           report.get("calls") == "4", str(report.get("calls")))
    record("scripts", "alloc_probe counts frees exactly",
           report.get("frees") == "4", str(report.get("frees")))
    # Live bytes peak after realloc: 64 (calloc) + 2000 = 2064.
    record("scripts", "alloc_probe tracks peak live bytes",
           report.get("peak_live") == "2064", str(report.get("peak_live")))

    silenced = subprocess.run([exe], capture_output=True, text=True,
                              env={**os.environ, "BENCH_ALLOC_REPORT": "0"})
    record("scripts", "alloc_probe honours BENCH_ALLOC_REPORT=0",
           "alloc_report" not in silenced.stderr, silenced.stderr[-120:])


def test_skill_invariants():
    with open(os.path.join(SKILL_DIR, "SKILL.md"), "r", encoding="utf-8") as handle:
        text = handle.read()

    front = text.split("---")[1] if text.startswith("---\n") else ""
    record("skill", "frontmatter has name",
           "name: allocation-path-optimization" in front)
    record("skill", "frontmatter has description", "description:" in front)
    record("skill", "frontmatter name matches directory",
           os.path.basename(SKILL_DIR) == "allocation-path-optimization")

    recipes = ["exact_size_allocation", "size_class_audit", "churn_coalescing",
               "paired_release", "allocator_cost_attribution"]
    missing = [r for r in recipes if r not in text]
    record("skill", "all five recipes documented", not missing, str(missing))

    body = text.split("## Compatibility")[0]
    record("skill", "routing body free of library names",
           "libevent" not in body.lower() and "kvrocks" not in body.lower())

    for invariant in ("所有权", "配对", "生命周期", "跨线程", "失败路径", "结构与 ABI", "引用计数"):
        record("skill", f"allocation invariant documented: {invariant}", invariant in text)

    for asset in ("scripts/alloc_size_audit.py", "scripts/alloc_probe.c",
                  "references/methodology.md",
                  "references/case-study-metadata-allocation.md"):
        record("skill", f"referenced asset exists: {asset}",
               os.path.exists(os.path.join(SKILL_DIR, asset)))
        record("skill", f"asset referenced from SKILL.md: {asset}",
               os.path.basename(asset) in text)

    for field in ("allocation_result", "measured_before", "inflation_ratio",
                  "ownership_contract_preserved", "invariants_checked", "error_message"):
        record("contract", f"output contract documents {field}", field in text)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_size_audit(tmp)
        test_alloc_probe(tmp)
        test_skill_invariants()

    failures = [r for r in results if not r["ok"]]
    summary = {
        "ran": len(results),
        "failures": len(failures),
        "skipped": len(skipped),
        "groups": {
            group: {
                "ran": sum(1 for r in results if r["group"] == group),
                "failures": sum(1 for r in results if r["group"] == group and not r["ok"]),
            }
            for group in sorted({r["group"] for r in results})
        },
        "failed_names": [r["name"] for r in failures],
    }
    print("PYTEST_SUMMARY: " + json.dumps(summary, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
