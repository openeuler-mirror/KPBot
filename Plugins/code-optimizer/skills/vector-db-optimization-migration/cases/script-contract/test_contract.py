#!/usr/bin/env python3
"""Tier-A contract tests for vector-db-optimization-migration scripts.

Groups:

  compare_topk  G0/G3 equivalence gates: order swap, ULP-level distance
                drift, extra/missing candidate query, row-count mismatch
                must all exit 1 by default; distance equality in strict
                mode is bit-pattern equality (-0.0 vs +0.0 fails); rank
                columns must be contiguous and duplicate-free; omitting
                --k compares the full dumps (candidate extra rows are
                not truncated away); --allow-order-change and explicit
                tolerances are the only relaxations; output fields use
                overlap@k naming (no recall-vs-ground-truth claim).
  check_flags   compile_commands.json parsing: -U cancels an earlier -D
                (both "-UNAME" and "-U NAME"), later -D wins after -U,
                space-separated "-march value" is folded, negative
                boolean flags (-fno-lto) leave the flag explicitly off,
                a TU missing a uniform flag counts as its own <absent>
                value, non-uniform flags fail by default and
                --allow-inconsistent downgrades to report-only.
  scan_delta    version-delta audit: CMake option() without a default is
                implicit OFF (no false "added"), and every defined() in a
                combined #if condition is captured.
  skill         SKILL.md frontmatter intact, referenced assets exist,
                and the G5 noise-floor wording is consistent between
                the gates doc and the delivery checklist.

Prints "PYTEST_SUMMARY: {json}" on the last line.
"""

import json
import os
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


def dump_pair(tmp, base_text, cand_text):
    return write(tmp, "base.dump", base_text), write(tmp, "cand.dump", cand_text)


def compile_commands(tmp, entries):
    path = os.path.join(tmp, "cc.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(entries, handle)
    return path


def cc_entry(name, args):
    return {"directory": "/build", "file": "/src/" + name, "arguments": args}


def test_compare_topk(tmp):
    # Reviewer counterexample: order swap + distance change must fail.
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 B 20\nq0 2 A 10\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2"])
    record("compare_topk", "order swap + distance change exits 1",
           p.returncode == 1, f"rc={p.returncode}")
    record("compare_topk", "order swap reported as order_mismatch",
           "order_mismatch=1" in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")

    # ULP-level drift with default zero tolerance must fail.
    b, c = dump_pair(tmp, "q0 1 A 1\n", "q0 1 A 1.0000005\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "ULP-level distance drift exits 1 (default zero tolerance)",
           p.returncode == 1, f"rc={p.returncode}")

    # Bit-pattern equality: -0.0 vs +0.0 are numerically equal but must
    # still fail in strict mode (reviewer round-2 counterexample).
    b, c = dump_pair(tmp, "q0 1 A -0.0\n", "q0 1 A 0.0\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "-0.0 vs +0.0 exits 1 (bit-pattern equality)",
           p.returncode == 1 and "dist_mismatch=1" in p.stdout, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Same bit pattern on both sides still passes.
    b, c = dump_pair(tmp, "q0 1 A -0.0\n", "q0 1 A -0.0\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "identical -0.0 on both sides exits 0",
           p.returncode == 0, f"rc={p.returncode}")

    # Rank columns must be contiguous and duplicate-free (reviewer
    # round-2 counterexample: candidate writes ranks 1,2 as 1,1).
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 A 1\nq0 1 B 2\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2"])
    record("compare_topk", "duplicate rank in candidate exits 1",
           p.returncode == 1 and "rank_mismatch=1" in p.stdout, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Default full-dump comparison: a candidate with extra rows must not
    # be silently truncated to the baseline row count (round-2 example:
    # baseline 1 row A, candidate 2 rows A,B used to return 0).
    b, c = dump_pair(tmp, "q0 1 A 1\n", "q0 1 A 1\nq0 2 B 2\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "full mode: candidate extra rows exit 1 (no silent truncation)",
           p.returncode == 1 and "row_count_mismatch=1" in p.stdout, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # ...but an explicit --k truncates both sides (intended relaxation).
    b, c = dump_pair(tmp, "q0 1 A 1\n", "q0 1 A 1\nq0 2 B 2\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "1"])
    record("compare_topk", "explicit --k truncates both sides and exits 0",
           p.returncode == 0, f"rc={p.returncode}")

    # Identical dumps pass.
    b, c = dump_pair(tmp, "q0 1 A 1.5\nq0 2 B 2.5\n", "q0 1 A 1.5\nq0 2 B 2.5\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2"])
    record("compare_topk", "identical dumps exit 0",
           p.returncode == 0, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Candidate-only query must fail (query-set check is two-sided).
    b, c = dump_pair(tmp, "q0 1 A 1\n", "q0 1 A 1\nq9 1 X 3\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "extra candidate query exits 1",
           p.returncode == 1 and "extra_candidate=1" in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")

    # Baseline query missing from candidate must fail.
    b, c = dump_pair(tmp, "q0 1 A 1\nq1 1 B 2\n", "q0 1 A 1\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c])
    record("compare_topk", "missing candidate query exits 1",
           p.returncode == 1 and "missing_candidate=1" in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")

    # Row-count mismatch must fail.
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 A 1\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2"])
    record("compare_topk", "row count mismatch exits 1",
           p.returncode == 1 and "row_count_mismatch=1" in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")

    # OPT_IN relaxation: same set + same distances, different order.
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 B 2\nq0 2 A 1\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2", "--allow-order-change"])
    record("compare_topk", "--allow-order-change accepts pure reordering",
           p.returncode == 0, f"rc={p.returncode}")

    # ...but relaxation must not excuse a distance change.
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 B 20\nq0 2 A 10\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2", "--allow-order-change"])
    record("compare_topk", "--allow-order-change still rejects distance change",
           p.returncode == 1, f"rc={p.returncode}")

    # Explicit tolerance is honored.
    b, c = dump_pair(tmp, "q0 1 A 1\n", "q0 1 A 1.0000005\n")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--atol", "1e-5"])
    record("compare_topk", "explicit --atol accepts sub-tolerance drift",
           p.returncode == 0, f"rc={p.returncode}")

    # Output contract: overlap naming, no recall fields, JSON carries summary.
    b, c = dump_pair(tmp, "q0 1 A 1\nq0 2 B 2\n", "q0 1 A 1\nq0 2 B 2\n")
    j = os.path.join(tmp, "report.json")
    p = run_py("compare_topk.py", ["--baseline", b, "--candidate", c, "--k", "2", "--json", j])
    data = json.load(open(j, encoding="utf-8")) if os.path.exists(j) else {}
    summary = data.get("summary", {})
    record("compare_topk", "stdout uses overlap@k naming, no recall",
           "mean_overlap" in p.stdout and "recall" not in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")
    record("compare_topk", "json summary carries overlap + order fields",
           "mean_overlap" in summary and "order_mismatch_queries" in summary
           and "extra_candidate_queries" in summary and "atol" in summary,
           json.dumps(sorted(summary.keys())))


def test_check_flags(tmp):
    # -D followed by -U cancels the macro: require must fail.
    cc = compile_commands(tmp, [cc_entry("a.cpp", ["g++", "-DFEATURE=1", "-UFEATURE", "-c", "/src/a.cpp"])])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-macro", "FEATURE=1"])
    record("check_flags", "-U cancelled macro exits 1",
           p.returncode == 1 and "MISSING" in p.stdout, f"rc={p.returncode}")

    # Order-aware: -U before -D keeps the later definition.
    cc = compile_commands(tmp, [cc_entry("a.cpp", ["g++", "-UFEATURE", "-DFEATURE=2", "-c", "/src/a.cpp"])])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-macro", "FEATURE=2"])
    record("check_flags", "-U then -D keeps later definition",
           p.returncode == 0, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Space-separated "-U NAME" cancels the macro (round-2 counterexample:
    # `-DFEATURE=1 -U FEATURE` used to leave the macro defined).
    cc = compile_commands(tmp, [cc_entry("a.cpp", ["g++", "-DFEATURE=1", "-U", "FEATURE", "-c", "/src/a.cpp"])])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-macro", "FEATURE=1"])
    record("check_flags", "space-separated -U cancels macro, exits 1",
           p.returncode == 1 and "MISSING" in p.stdout, f"rc={p.returncode}")

    # Space-separated "-D NAME=VALUE" defines the macro.
    cc = compile_commands(tmp, [cc_entry("a.cpp", ["g++", "-D", "FEATURE=1", "-c", "/src/a.cpp"])])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-macro", "FEATURE=1"])
    record("check_flags", "space-separated -D defines macro, exits 0",
           p.returncode == 0, f"rc={p.returncode}")

    # Negative boolean flags: `-flto -fno-lto` leaves LTO explicitly off,
    # so --require-flag=-flto must fail for that TU (round-2 counterexample).
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-flto", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-flto", "-fno-lto", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-flag=-flto"])
    record("check_flags", "-flto overridden by -fno-lto exits 1",
           p.returncode == 1 and "MISSING" in p.stdout and "b.cpp" in p.stdout,
           f"rc={p.returncode} {p.stdout.splitlines()[:3] if p.stdout else ''}")

    # ...and one TU on, one explicitly off is also a non-uniform pair.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-flto", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-fno-lto", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc])
    record("check_flags", "-flto vs -fno-lto across TUs exits 1 as non-uniform",
           p.returncode == 1 and "INCONSISTENT" in p.stdout and "-fno-lto" in p.stdout,
           f"rc={p.returncode} {p.stdout.splitlines()[:2] if p.stdout else ''}")

    # Explicitly requiring the negative form is satisfied by -fno-lto.
    cc = compile_commands(tmp, [cc_entry("a.cpp", ["g++", "-fno-lto", "-c", "/src/a.cpp"])])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--require-flag=-fno-lto"])
    record("check_flags", "--require-flag=-fno-lto exits 0 when flag is off",
           p.returncode == 0, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # A TU missing a uniform flag is its own <absent> value and fails.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-march=armv8.2-a+sve", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc])
    record("check_flags", "single TU missing -march exits 1 as non-uniform",
           p.returncode == 1 and "<absent>" in p.stdout, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Space-separated "-march value": no false MISSING, uniform TUs pass.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-march", "armv8.2-a+sve", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-march=armv8.2-a+sve", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py",
               ["--compile-commands", cc, "--require-flag=-march=armv8.2-a+sve"])
    record("check_flags", "space-separated -march folds like -march=val",
           p.returncode == 0 and "MISSING" not in p.stdout, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")

    # Different space-separated -march values are detected as non-uniform.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-march", "armv8.2-a+sve", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-march", "armv8.6-a", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc])
    record("check_flags", "different space-separated -march exits 1",
           p.returncode == 1 and "INCONSISTENT" in p.stdout, f"rc={p.returncode}")

    # --allow-inconsistent downgrades non-uniformity to report-only.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-march", "armv8.2-a+sve", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-march", "armv8.6-a", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py", ["--compile-commands", cc, "--allow-inconsistent"])
    record("check_flags", "--allow-inconsistent keeps exit 0 but reports",
           p.returncode == 0 and "INCONSISTENT" in p.stdout, f"rc={p.returncode}")

    # Uniform TUs with required macro and flag pass.
    cc = compile_commands(tmp, [
        cc_entry("a.cpp", ["g++", "-DFOO=1", "-march=armv8.2-a+sve", "-flto", "-c", "/src/a.cpp"]),
        cc_entry("b.cpp", ["g++", "-DFOO=1", "-march=armv8.2-a+sve", "-flto", "-c", "/src/b.cpp"]),
    ])
    p = run_py("check_compile_flags.py",
               ["--compile-commands", cc, "--require-macro", "FOO=1",
                "--require-flag=-march=armv8.2-a+sve"])
    record("check_flags", "uniform TUs with requirements exit 0",
           p.returncode == 0, f"rc={p.returncode} {p.stdout.splitlines()[0] if p.stdout else ''}")


def svd_trees(tmp, src_files, tgt_files):
    src = os.path.join(tmp, "src")
    tgt = os.path.join(tmp, "tgt")
    os.makedirs(src, exist_ok=True)
    os.makedirs(tgt, exist_ok=True)
    for name, text in src_files.items():
        with open(os.path.join(src, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    for name, text in tgt_files.items():
        with open(os.path.join(tgt, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    return src, tgt


def test_scan_delta(tmp):
    src, tgt = svd_trees(
        tmp,
        {"CMakeLists.txt": 'option(OPT "docs")\n'},
        {"CMakeLists.txt": 'option(OPT "docs" ON)\n'},
    )
    j = os.path.join(tmp, "delta1.json")
    p = run_py("scan_version_delta.py", ["--source", src, "--target", tgt, "--json", j])
    data = json.load(open(j, encoding="utf-8")) if os.path.exists(j) else {}
    opts = data.get("cmake_options", {})
    record("scan_delta", "implicit OFF -> ON is default_changed, not added",
           "OPT" not in opts.get("added", [])
           and any(x.get("name") == "OPT" for x in opts.get("default_changed", [])),
           json.dumps(opts))

    src, tgt = svd_trees(
        tmp,
        {"a.cpp": "#if defined(COMMON) && defined(OLD_OPT)\nint x;\n#endif\n"},
        {"a.cpp": "#if defined(COMMON) && defined(NEW_OPT)\nint x;\n#endif\n"},
    )
    j = os.path.join(tmp, "delta2.json")
    p = run_py("scan_version_delta.py", ["--source", src, "--target", tgt, "--json", j])
    data = json.load(open(j, encoding="utf-8")) if os.path.exists(j) else {}
    macros = data.get("macros", {})
    record("scan_delta", "combined defined(): second macro only in source",
           "OLD_OPT" in macros.get("only_in_source", []), json.dumps(macros))
    record("scan_delta", "combined defined(): replacement only in target",
           "NEW_OPT" in macros.get("only_in_target", []), json.dumps(macros))

    src, tgt = svd_trees(
        tmp,
        {"CMakeLists.txt": 'option(OPT "docs" ON)\n', "a.cpp": "#ifdef FOO\nint y;\n#endif\n"},
        {"CMakeLists.txt": 'option(OPT "docs" ON)\n', "a.cpp": "#ifdef FOO\nint y;\n#endif\n"},
    )
    p = run_py("scan_version_delta.py", ["--source", src, "--target", tgt])
    record("scan_delta", "identical trees exit 0",
           p.returncode == 0, f"rc={p.returncode}")


def test_skill(tmp):
    skill_md = os.path.join(SKILL_DIR, "SKILL.md")
    with open(skill_md, encoding="utf-8") as handle:
        text = handle.read()
    record("skill", "SKILL.md frontmatter name intact",
           text.startswith("---\n") and "name: vector-db-optimization-migration" in text, "")
    record("skill", "SKILL.md documents strict compare_topk contract",
           "allow-order-change" in text and "overlap@k" in text, "")
    record("skill", "SKILL.md documents default-fail flag check",
           "退出 1" in text and "path-filter" in text, "")

    for rel in [
        "references/api-abi-triage.md",
        "references/engine-routing.md",
        "references/engines/diskann.md",
        "references/engines/faiss.md",
        "references/equivalence-and-gates.md",
        "references/failure-modes.md",
        "references/known-cases.md",
        "references/migration-checklist.md",
        "references/migration-contract.md",
        "references/optimization-output-format.md",
        "references/quickstart.md",
        "references/version-delta-audit.md",
        "scripts/check_compile_flags.py",
        "scripts/compare_topk.py",
        "scripts/scan_version_delta.py",
        "templates/HANDOFF.md",
        "templates/MIGRATION-MATRIX.md",
        "templates/SOURCE-TARGET-IDENTITY.json",
        "templates/kernel_probe.cpp",
    ]:
        record("skill", f"asset exists: {rel}", os.path.isfile(os.path.join(SKILL_DIR, rel)), "")

    with open(os.path.join(SKILL_DIR, "references", "known-cases.md"), encoding="utf-8") as handle:
        known = handle.read()
    record("skill", "known-cases.md carries [unverified] traceability tags",
           known.count("[unverified]") >= 3 and "STANDARDS.md" in known, "")

    with open(os.path.join(SKILL_DIR, "references", "equivalence-and-gates.md"), encoding="utf-8") as handle:
        gates = handle.read()
    record("skill", "noise floor uses same-config A/A calibration",
           "同配置自对照" in gates and "不得用" in gates, "")

    # The delivery checklist must not contradict the gates doc: the old
    # "惰性/全 OFF 对照" calibration wording is forbidden, and the item
    # must reference the G5 gates (round-2 drift between the two files).
    with open(os.path.join(SKILL_DIR, "references", "migration-checklist.md"), encoding="utf-8") as handle:
        checklist = handle.read()
    record("skill", "checklist noise-floor wording matches G5 gates",
           "同配置" in checklist and "equivalence-and-gates.md" in checklist
           and "惰性/全 OFF 对照实测标定" not in checklist,
           checklist.splitlines()[71] if len(checklist.splitlines()) > 71 else "")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_compare_topk(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_check_flags(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_scan_delta(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_skill(tmp)

    failed = [r for r in results if not r["ok"]]
    summary = {
        "ran": len(results),
        "failures": len(failed),
        "skipped": len(skipped),
        "groups": {},
        "failed_names": [f'[{r["group"]}] {r["name"]}' for r in failed],
    }
    for r in results:
        summary["groups"][r["group"]] = summary["groups"].get(r["group"], 0) + 1
    print("PYTEST_SUMMARY: " + json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
