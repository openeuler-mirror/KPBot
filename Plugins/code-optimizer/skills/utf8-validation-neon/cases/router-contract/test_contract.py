#!/usr/bin/env python3
"""Tier-C contract tests for utf8-validation-neon.

Three groups:

  skill     SKILL.md invariants: frontmatter, sections, referenced files
            exist on disk, patch-template polarity, no fabricated symbols.
  transform Real-machine semantics (aarch64 + gcc; recorded as SKIP when the
            machine or toolchain is unavailable): fuzz bit-identity, NEON
            disassembly evidence, wrong-polarity no-op proof, A/B speedup
            gates against fixtures in src/.
  contract  the output object carries the fields downstream stages read.

Prints "PYTEST_SUMMARY: {json}" on the last line and writes
results/result.json.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(SKILL_DIR, "scripts")
REFS = os.path.join(SKILL_DIR, "references")
SRC = os.path.join(HERE, "src")
RESULTS = os.path.join(HERE, "results")

results = []
ran = 0
failures = 0


def record(group, name, ok, detail="", skipped=False):
    global ran, failures
    results.append({"group": group, "name": name, "ok": bool(ok), "skipped": skipped})
    if skipped:
        print(f"skip [{group}] {name}")
        return
    ran += 1
    if not ok:
        failures += 1
    print(("ok   " if ok else "FAIL ") + f"[{group}] {name}" + (f" :: {detail}" if detail and not ok else ""))


AARCH64 = os.uname().machine == "aarch64"
CC = shutil.which("cc") or shutil.which("gcc")
HAVE_OBJDUMP = shutil.which("objdump") is not None
CAN_TRANSFORM = AARCH64 and CC is not None and HAVE_OBJDUMP


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- skill group

skill_md = read(os.path.join(SKILL_DIR, "SKILL.md"))
m = re.match(r"^---\n(.*?)\n---\n", skill_md, re.S)
record("skill", "frontmatter present", m is not None, "no YAML frontmatter")
fm = m.group(1) if m else ""
name_ok = re.search(r"^name:\s*utf8-validation-neon$", fm, re.M) is not None
record("skill", "frontmatter name matches directory", name_ok)
desc = re.search(r"^description:\s*(.*)$", fm, re.M)
desc_ok = desc is not None and "__SSE4_1__" in desc.group(1) and "不适用" in desc.group(1)
record("skill", "description carries triggers and non-goals", desc_ok)

sections = ["## 职责边界", "## 输入", "## 辅助脚本", "## 执行步骤", "## 拒绝条件",
            "## 输出", "## 平台适用性", "## 实测收益", "## 参考资料", "## Compatibility", "## 规则"]
missing = [s for s in sections if s not in skill_md]
record("skill", "all required sections present", not missing, f"missing: {missing}")

referenced = [
    "scripts/checker.c", "scripts/validate_bench.c",
    "references/router-patch-guide.md", "references/case-study-protobuf-27.3.md",
    "references/data/aggregate_validate_ab.tsv",
    "references/data/vorg_run1.csv", "references/data/vopt_run1.csv",
    "cases/router-contract/src/utf8_range.c",
    "cases/router-contract/src/utf8_range_opt.c",
    "cases/router-contract/src/range-neon.c",
    "cases/router-contract/src/naive.c",
    "cases/router-contract/src/utf8_range.h",
]
absent = [p for p in referenced if not os.path.isfile(os.path.join(SKILL_DIR, p))]
record("skill", "every referenced file exists", not absent, f"absent: {absent}")

guide = read(os.path.join(REFS, "router-patch-guide.md"))
polarity_ok = "#ifdef __aarch64__" in guide and "utf8_range(data, end - data) == 0 ? 1 : 0" in guide
record("skill", "patch template polarity and dispatch body", polarity_ok)

opt_src = read(os.path.join(SRC, "utf8_range_opt.c"))
org_src = read(os.path.join(SRC, "utf8_range.c"))
no_fabricated = "utf8_range_ValidateUTF8Native" not in opt_src
record("skill", "optimized fixture references no fabricated symbol", no_fabricated)
router_shape = opt_src.count("#ifndef __SSE4_1__\n#ifdef __aarch64__") == 1
record("skill", "optimized fixture router block shape", router_shape)
line_delta = len(opt_src.splitlines()) - len(org_src.splitlines())
record("skill", "optimized fixture adds exactly the router patch (13 lines)", line_delta == 13, f"delta={line_delta}")

# ------------------------------------------------------------ transform group

if CAN_TRANSFORM:
    tmp = tempfile.mkdtemp(prefix="utf8case-")
    cc = CC
    common = ["-O3", "-DNDEBUG", "-I", SRC]

    def build(out, sources):
        return subprocess.run([cc] + common + sources + ["-o", os.path.join(tmp, out)],
                               capture_output=True, text=True).returncode == 0

    checker_c = os.path.join(SCRIPTS, "checker.c")
    bench_c = os.path.join(SCRIPTS, "validate_bench.c")

    record("transform", "build checker (baseline)", build("checker_org", [checker_c, os.path.join(SRC, "utf8_range.c")]))
    record("transform", "build checker (optimized)", build("checker_opt",
            [checker_c, os.path.join(SRC, "utf8_range_opt.c"),
             os.path.join(SRC, "range-neon.c"), os.path.join(SRC, "naive.c")]))

    org_out = subprocess.run([os.path.join(tmp, "checker_org")], capture_output=True, text=True, timeout=120)
    opt_out = subprocess.run([os.path.join(tmp, "checker_opt")], capture_output=True, text=True, timeout=120)
    fuzz_ok = (org_out.returncode == 0 and opt_out.returncode == 0
               and org_out.stdout == opt_out.stdout
               and len(org_out.stdout.splitlines()) == 973)
    record("transform", "fuzz cross-check bit-identical (973 cases)", fuzz_ok,
           f"rc={org_out.returncode}/{opt_out.returncode}, lines={len(org_out.stdout.splitlines())}")

    def disasm(binary, symbol):
        out = subprocess.run(["objdump", "-d", binary, f"--disassemble={symbol}"],
                              capture_output=True, text=True).stdout
        norm = []
        for line in out.splitlines():
            if "file format" in line:
                continue
            norm.append(re.sub(r"^\s*[0-9a-f]+:\s+[0-9a-f ]+\s\t", " ", line))
        return "\n".join(norm)

    opt_isvalid = disasm(os.path.join(tmp, "checker_opt"), "utf8_range_IsValid")
    org_isvalid = disasm(os.path.join(tmp, "checker_org"), "utf8_range_IsValid")
    opt_neon = disasm(os.path.join(tmp, "checker_opt"), "utf8_range")
    route_call = re.search(r"bl\s+[0-9a-f]+ <utf8_range>", opt_isvalid) is not None
    neon_tbl = "\ttbl\t" in opt_neon or re.search(r"\btbl\tv", opt_neon) is not None
    record("transform", "optimized IsValid routes to NEON utf8_range (bl + tbl)",
           route_call and neon_tbl,
           f"route_call={route_call}, neon_tbl={neon_tbl}")
    record("transform", "baseline IsValid has no NEON routing",
           re.search(r"<utf8_range>", org_isvalid) is None)

    wrong_src = opt_src.replace("#ifndef __SSE4_1__\n#ifdef __aarch64__",
                                "#ifndef __SSE4_1__\n#ifndef __aarch64__")
    assert wrong_src != opt_src
    wrong_path = os.path.join(tmp, "utf8_range_wrong.c")
    with open(wrong_path, "w", encoding="utf-8") as f:
        f.write(wrong_src)
    record("transform", "build wrong-polarity variant (no-op trap)",
           build("checker_wrong", [checker_c, wrong_path,
                                   os.path.join(SRC, "range-neon.c"), os.path.join(SRC, "naive.c")]))
    wrong_isvalid = disasm(os.path.join(tmp, "checker_wrong"), "utf8_range_IsValid")
    wrong_prefix = disasm(os.path.join(tmp, "checker_wrong"), "utf8_range_ValidPrefix")
    org_prefix = disasm(os.path.join(tmp, "checker_org"), "utf8_range_ValidPrefix")
    noop_ok = wrong_isvalid == org_isvalid and wrong_prefix == org_prefix
    record("transform", "wrong-polarity disassembly identical to baseline (no-op proof)", noop_ok)

    def build_bench(out, impl):
        return subprocess.run(
            [cc, "-O3", "-DNDEBUG", "-I", SRC, f'-DIMPL="{impl}"', bench_c]
            + ([os.path.join(SRC, "range-neon.c"), os.path.join(SRC, "naive.c")]
               if impl == "utf8_range_opt.c" else [])
            + ["-o", os.path.join(tmp, out)],
            capture_output=True, text=True).returncode == 0

    record("transform", "build bench (baseline)", build_bench("vbench_org", "utf8_range.c"))
    record("transform", "build bench (optimized)", build_bench("vbench_opt", "utf8_range_opt.c"))

    borg = subprocess.run([os.path.join(tmp, "vbench_org")], capture_output=True, text=True, timeout=300)
    bopt = subprocess.run([os.path.join(tmp, "vbench_opt")], capture_output=True, text=True, timeout=300)
    record("transform", "both bench runs report correctness PASS",
           "correctness: PASS" in borg.stdout and "correctness: PASS" in bopt.stdout)

    def parse_bench(stdout):
        d = {}
        for line in stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) == 6 and parts[0].startswith("utf8_range_Validate"):
                d[(parts[0], parts[1], int(parts[2]))] = float(parts[4])
        return d

    d_org, d_opt = parse_bench(borg.stdout), parse_bench(bopt.stdout)
    key_mixed = ("utf8_range_Validate(rp=0)", "mixed_utf8", 1024)
    key_ascii = ("utf8_range_Validate(rp=0)", "ascii", 1024)
    key_rp1 = ("utf8_range_Validate(rp=1)", "mixed_utf8", 1024)
    sp_mixed = d_org[key_mixed] / d_opt[key_mixed] if key_mixed in d_org and key_mixed in d_opt else 0.0
    sp_ascii = d_org[key_ascii] / d_opt[key_ascii] if key_ascii in d_org and key_ascii in d_opt else 99.0
    sp_rp1 = d_org[key_rp1] / d_opt[key_rp1] if key_rp1 in d_org and key_rp1 in d_opt else 99.0
    record("transform", "mixed utf8 rp=0 speedup >= 2.0x @1KB", sp_mixed >= 2.0, f"{sp_mixed:.2f}x")
    record("transform", "ascii rp=0 within noise band @1KB", 0.75 <= sp_ascii <= 1.35, f"{sp_ascii:.2f}x")
    record("transform", "rp=1 naive fallback within noise band @1KB", 0.85 <= sp_rp1 <= 1.15, f"{sp_rp1:.2f}x")

    shutil.rmtree(tmp, ignore_errors=True)
else:
    reason = "requires aarch64 + cc + objdump" if not AARCH64 else "requires cc + objdump in PATH"
    for name in ["build checker (baseline)", "build checker (optimized)",
                 "fuzz cross-check bit-identical (973 cases)",
                 "NEON table instructions present in optimized IsValid",
                 "baseline IsValid has no vector instructions",
                 "build wrong-polarity variant (no-op trap)",
                 "wrong-polarity disassembly identical to baseline (no-op proof)",
                 "build bench (baseline)", "build bench (optimized)",
                 "both bench runs report correctness PASS",
                 "mixed utf8 rp=0 speedup >= 2.0x @1KB",
                 "ascii rp=0 within noise band @1KB",
                 "rp=1 naive fallback within noise band @1KB"]:
        record("transform", name, False, skipped=True)
    print(f"[case] transform group SKIP: {reason}")

# ------------------------------------------------------------ contract group

out_block = skill_md[skill_md.index("## 输出"):]
required_fields = ["utf8_route_result", "success", "router_patch", "functional_test",
                   "performance", "risk_notes", "error_message"]
miss_fields = [f for f in required_fields if f not in out_block]
record("contract", "output object carries all required fields", not miss_fields, f"missing: {miss_fields}")

summary = {"ran": ran, "failures": failures, "skipped": 0 if CAN_TRANSFORM else 13,
           "groups": {}, "arch": os.uname().machine, "transform_skipped": not CAN_TRANSFORM}
for r in results:
    summary["groups"].setdefault(r["group"], {"ran": 0, "failures": 0})
    if not r["skipped"]:
        summary["groups"][r["group"]]["ran"] += 1
        if not r["ok"]:
            summary["groups"][r["group"]]["failures"] += 1

os.makedirs(RESULTS, exist_ok=True)
payload = {
    "success": failures == 0 and ran > 0,
    "checks_ran": ran,
    "checks_failures": failures,
    "checks_skipped": summary["skipped"],
    "arch": summary["arch"],
    "transform_skipped": summary["transform_skipped"],
    "groups": summary["groups"],
}
with open(os.path.join(RESULTS, "result.json"), "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("PYTEST_SUMMARY: " + json.dumps(summary, ensure_ascii=False))
sys.exit(0 if payload["success"] else 1)
