#!/usr/bin/env python3
"""Tier-A integrity checks for the generic-timer-optimization skill.

Validates that the skill stays well-formed and self-consistent as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description
     present, leaf-skill declaration kept (review-fix regression guard)
  2. output contract: the `generic_timer_optimization_result` JSON block is
     parseable, has all required fields, and its techniques_used values are
     within the documented enum
  3. numeric invariants recomputed from first principles: overflow bound
     2^64/1e9 ticks, 184 s safe span @100 MHz, counter wraparound horizon,
     split-conversion second-term bound, tick-domain limit bound
  4. template invariants: `#if defined(__aarch64__)` gating with `#else`
     fallback, `asm volatile` + `mrs` reads, no-hardcoded-frequency rule,
     unsigned-subtraction wraparound handling, overflow protection variant
  5. review-fix negative checks: the retired apply-optimization dispatch
     wording must not come back
  6. no sensitive content (private IPs, credentials, personal home paths)

Writes results/result.json; exits non-zero on any failure.
Tier-A: python3 standard library only; no target hardware required.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
anchors_found = 0
numeric_checks = {}

def fail(msg):
    failures.append(msg)

text = SKILL_MD.read_text(encoding="utf-8")

# ---- 1. frontmatter ----
fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not fm_match:
    fail("SKILL.md has no frontmatter block")
else:
    fm = fm_match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
    if not name_match:
        fail("frontmatter: missing name")
    elif name_match.group(1) != SKILL_DIR.name:
        fail(f"frontmatter: name {name_match.group(1)!r} != dir {SKILL_DIR.name!r}")
    desc_match = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE | re.DOTALL)
    if not desc_match or not desc_match.group(1).strip():
        fail("frontmatter: missing/empty description")
    else:
        desc = desc_match.group(1).strip()
        if len(desc) > 1024:
            fail(f"frontmatter: description exceeds 1024 chars ({len(desc)})")
        if "独立调用的叶子 skill" not in desc:
            fail("frontmatter: description lost the leaf-skill declaration")
        if "不由 apply-optimization 结构化流水线分派" not in desc:
            fail("frontmatter: description lost the non-pipeline-dispatch clause")

# ---- 2. output contract ----
REQUIRED_FIELDS = [
    "success", "original_code", "optimized_code",
    "clock_reads_per_iteration", "techniques_used",
    "overflow_bound_seconds", "fallback_arch",
    "modified_file", "error_message",
]
TECHNIQUES_ENUM = [
    "cntvct_el0", "cntfrq_el0", "anchor_time_point",
    "tick_domain_compare", "split_conversion", "isb_serialize",
]
contract = None
for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
    if "generic_timer_optimization_result" in block:
        try:
            contract = json.loads(block)["generic_timer_optimization_result"]
        except (json.JSONDecodeError, KeyError) as e:
            fail(f"output contract block not parseable: {e}")
        break
if contract is None:
    fail("output contract: no json block with generic_timer_optimization_result")
else:
    for field in REQUIRED_FIELDS:
        if field not in contract:
            fail(f"output contract: missing field {field!r}")
    for tech in contract.get("techniques_used", []):
        if tech not in TECHNIQUES_ENUM:
            fail(f"output contract: techniques_used value {tech!r} outside documented enum")
    if contract.get("overflow_bound_seconds") != 184:
        fail("output contract: overflow_bound_seconds != 184 (inconsistent with numeric section)")
    if not isinstance(contract.get("fallback_arch"), str) or "aarch64" not in contract["fallback_arch"]:
        fail("output contract: fallback_arch must describe the non-aarch64 fallback")
enum_decl = "、".join(f"`{t}`" for t in TECHNIQUES_ENUM)
if enum_decl not in text:
    fail(f"techniques_used enum declaration missing or drifted (expected: {enum_decl})")

# ---- 3. numeric invariants recomputed ----
MAX_TICKS = (1 << 64) // 10**9
if MAX_TICKS != 18446744073:
    fail(f"overflow bound recompute: 2^64//1e9 = {MAX_TICKS}, expected 18446744073")
numeric_checks["max_ticks_diff"] = MAX_TICKS

span_100mhz = MAX_TICKS / 100_000_000
if round(span_100mhz) != 184:
    fail(f"safe span @100MHz recompute: {span_100mhz:.3f}s does not round to 184")
if "约 184 秒" not in text:
    fail("numeric: documented '约 184 秒' safe span missing")
numeric_checks["safe_span_seconds_100mhz"] = round(span_100mhz, 3)

if abs(MAX_TICKS - 1.8e10) / 1.8e10 > 0.05:
    fail("numeric: documented '≈ 1.8e10' ticks bound drifted beyond 5%")
if "1.8e10" not in text:
    fail("numeric: documented '1.8e10' ticks bound missing")

wrap_years = (1 << 64) / 100_000_000 / (365.25 * 86400)
if wrap_years <= 1000:
    fail(f"wraparound horizon recompute: {wrap_years:.0f} years, '几千年' claim untruthful")
numeric_checks["wraparound_years_100mhz"] = round(wrap_years)

for freq in (24_000_000, 100_000_000, 400_000_000, 1_000_000_000, 4_000_000_000):
    if freq * 10**9 >= 1 << 64:
        fail(f"split conversion: second term bound freq*1e9 >= 2^64 at freq={freq}")
boundary = ((1 << 64) // 10**9 + 1) * 10**9
if boundary <= 1 << 64:
    fail("split conversion: bound boundary check broken (freq*1e9<=2^64 should saturate above ~18.4GHz)")
numeric_checks["split_bound_max_freq_hz"] = (1 << 64) // 10**9

limit_ns_max = (1 << 64) // 100_000_000
if round(limit_ns_max / 1e9) != 184:
    fail(f"tick-domain limit bound @100MHz: {limit_ns_max/1e9:.2f}s does not round to 184")
numeric_checks["tick_domain_limit_seconds_100mhz"] = round(limit_ns_max / 1e9, 2)

# ---- 4. template invariants stay documented ----
ANCHORS = [
    "#if defined(__aarch64__)",     # platform gating
    "#else",                         # non-aarch64 fallback branch
    "asm volatile(\"mrs %0, cntvct_el0\"",  # counter read template
    "asm volatile(\"mrs %0, cntfrq_el0\"",  # frequency read template
    "禁止硬编码",                     # frequency must come from cntfrq_el0
    "无符号减法",                     # wraparound-safe arithmetic
    "防 CSE",                        # volatile rationale
    "分步换算",                       # overflow-safe split conversion variant
    "锚点保留",                       # anchor Clock::now() keeps time_point domain
    "独立调用的叶子 skill",           # leaf-skill declaration (review fix)
    "源码替换由调用方自行负责",        # caller-owned source replacement (review fix)
]
for anchor in ANCHORS:
    if anchor in text:
        anchors_found += 1
    else:
        fail(f"template anchor missing from SKILL.md: {anchor!r}")
if text.count("#if defined(__aarch64__)") < 3:
    fail("template: expected platform gating in helper block + inline reads (>=3 occurrences)")

# ---- 5. review-fix negative checks ----
RETIRED = [
    "适用于 apply-optimization 调用",
    "由上游 `apply-optimization` 统一执行",
    "统一执行",
]
for phrase in RETIRED:
    if phrase in text:
        fail(f"retired wording still present (review regression): {phrase!r}")

# ---- 6. no sensitive content ----
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "personal home path"),
    (re.compile(r"[Cc]:\\Users\\[a-z][a-z0-9_.-]+\\"), "personal home path"),
]
sensitive_findings = []
for pattern, kind in SENSITIVE:
    for m in pattern.finditer(text):
        sensitive_findings.append(f"SKILL.md: {kind}: {m.group(0)!r}")
if sensitive_findings:
    fail(f"sensitive content found ({len(sensitive_findings)} hits)")

# ---- report ----
RESULTS.parent.mkdir(parents=True, exist_ok=True)
report = {
    "skill": SKILL_DIR.name,
    "checks": {
        "frontmatter": "ok" if not any("frontmatter" in f for f in failures) else "fail",
        "output_contract": {
            "fields": len(REQUIRED_FIELDS),
            "techniques_enum": len(TECHNIQUES_ENUM),
            "status": "ok" if not any("output contract" in f or "enum" in f for f in failures) else "fail",
        },
        "numeric_invariants": numeric_checks,
        "template_anchors": {"found": anchors_found, "total": len(ANCHORS)},
        "review_fix_regression": "ok" if not any("retired" in f for f in failures) else "fail",
        "sensitive_content": {"hits": len(sensitive_findings)},
    },
    "failures": failures,
    "sensitive_findings": sensitive_findings,
    "passed": not failures,
}
RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
if failures:
    print(f"FAILED: {len(failures)} issue(s)", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
print("skill-integrity: all checks passed")
sys.exit(0)
