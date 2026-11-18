#!/usr/bin/env python3
"""Tier-A integrity checks for the protobuf-optimization knowledge skill.

Validates that the skill stays well-formed and self-consistent as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present
  2. every references/*.md link in SKILL.md resolves on disk
  3. key methodology invariants are still documented (profile-aligned microbench
     acceptance, shape-premise check, 5-round alternating A/B, DebugString
     semantics rule, extrapolation discipline, SPE canonical alignment)
  4. no sensitive content (private IPs, credentials, personal home paths)

Writes results/result.json; exits non-zero on any failure.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
links_checked = 0
anchors_found = 0
sensitive_findings = []

def fail(msg):
    failures.append(msg)

# ---- 1. frontmatter ----
text = SKILL_MD.read_text(encoding="utf-8")
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
    if not re.search(r"^description:\s*\S", fm, re.MULTILINE | re.DOTALL):
        fail("frontmatter: missing/empty description")

# ---- 2. reference links resolve ----
for target in re.findall(r"\]\((references/[^)]+)\)", text):
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken reference link: {target}")
if links_checked < 2:
    fail(f"expected at least 2 reference links, found {links_checked}")

# ---- 3. methodology invariants stay documented ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "画像对齐",                    # microbench acceptance discipline
    "形态前提",                    # shape-premise check before porting
    "交替",                        # 5-round alternating A/B protocol
    "DebugString",                 # semantics comparison rule
    "SerializeAsString",           # the forbidden comparator must stay documented
    "外推",                        # extrapolation discipline
    "arm-spe-analysis",            # SPE canonical alignment (no dual thresholds)
    "period=65536",                # SPE sampling period discipline
]
for anchor in ANCHORS:
    if anchor in corpus:
        anchors_found += 1
    else:
        fail(f"methodology anchor missing from corpus: {anchor!r}")

# ---- 4. no sensitive content ----
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "personal home path"),
    (re.compile(r"[Cc]:\\Users\\[a-z][a-z0-9_.-]+\\"), "personal home path"),
]
for src in [SKILL_MD, *sorted(REFS_DIR.glob("*.md"))]:
    body = src.read_text(encoding="utf-8")
    for pattern, kind in SENSITIVE:
        for m in pattern.finditer(body):
            sensitive_findings.append(f"{src.name}: {kind}: {m.group(0)!r}")
if sensitive_findings:
    fail(f"sensitive content found ({len(sensitive_findings)} hits)")

# ---- 5. referenced scripts exist ----
for target in re.findall(r"`?(scripts/[A-Za-z0-9_.-]+\.(?:sh|py))`?", text):
    if not (SKILL_DIR / target).is_file():
        fail(f"script referenced in SKILL.md but missing on disk: {target}")

# ---- report ----
RESULTS.parent.mkdir(parents=True, exist_ok=True)
report = {
    "skill": SKILL_DIR.name,
    "checks": {
        "frontmatter": "ok" if not any("frontmatter" in f for f in failures) else "fail",
        "reference_links": {"checked": links_checked, "status": "ok" if links_checked >= 2 else "fail"},
        "methodology_anchors": {"found": anchors_found, "total": len(ANCHORS)},
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
