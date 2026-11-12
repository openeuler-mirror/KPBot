#!/usr/bin/env python3
"""Tier-A integrity checks for the openzl-compression-tuning knowledge skill.

Validates that the skill stays well-formed and self-consistent as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present
  2. every references/*.md link in SKILL.md resolves on disk
  3. key methodology invariants are still documented (constraint formulas,
     enable gates, correctness gate)
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
    r"kUnroll × 最大码长 \+ 7 ≤ 64",   # bit-container width hard constraint
    r"tableLog ≥ 16",                   # root-table gate 1 (large tables only)
    r"85%",                             # root-table gate 2 (identical-block share)
    r"bitsConsumed ≤ 54",               # root-table gate 3 (bit-window expressible)
    r"逐字节",                          # lossless gate: byte-identical output
]
for pattern in ANCHORS:
    if re.search(pattern, corpus):
        anchors_found += 1
    else:
        fail(f"methodology anchor missing from skill docs: {pattern}")
if anchors_found < len(ANCHORS):
    fail(f"only {anchors_found}/{len(ANCHORS)} methodology anchors found")

# ---- 4. sensitive content scan ----
SENSITIVE = [
    (r"\b(192\.168|10\.\d{1,3}|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
     "private IP address"),
    (r"(?i)password\s*[:=]\s*['\"]?\S+", "credential-like literal"),
    (r"/home/(?!<user>|\$|\{)[A-Za-z0-9_.-]+/", "personal home path"),
]
scan_files = [SKILL_MD] + sorted(REFS_DIR.glob("*.md"))
for path in scan_files:
    body = path.read_text(encoding="utf-8")
    for pattern, why in SENSITIVE:
        for hit in re.finditer(pattern, body):
            line_no = body[:hit.start()].count("\n") + 1
            sensitive_findings.append(f"{path.name}:{line_no}: {why}: {hit.group(0)!r}")
if sensitive_findings:
    fail(f"sensitive content found: {len(sensitive_findings)} hit(s)")

# ---- result.json ----
RESULTS.parent.mkdir(parents=True, exist_ok=True)
result = {
    "success": not failures,
    "failures": len(failures),
    "failure_details": failures,
    "links_checked": links_checked,
    "anchors_found": anchors_found,
    "anchors_expected": len(ANCHORS),
    "sensitive_findings": len(sensitive_findings),
    "sensitive_details": sensitive_findings,
}
RESULTS.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if not failures else 1)
