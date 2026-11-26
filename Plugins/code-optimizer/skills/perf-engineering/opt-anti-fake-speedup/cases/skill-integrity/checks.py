#!/usr/bin/env python3
"""Tier-A integrity checks for a KPBot skill.

Validates the skill stays well-formed and de-privatized as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present,
     no license:/metadata: keys (repo convention: frontmatter = name + description only)
  2. every references/*.md reference in SKILL.md resolves on disk
     (markdown-link form `](references/...)` or backtick form `` `references/...` ``)
  3. description carries a trigger + anti-trigger convention:
     leaf skills -> "适用" + "不适用"; hub skills -> "触发"
  4. no sensitive content (private IPs, credentials, personal home paths)
  5. every scripts/*.py reference in SKILL.md resolves on disk

Writes results/result.json; exits non-zero on any failure.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
SCRIPTS_DIR = SKILL_DIR / "scripts"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
links_checked = 0
scripts_checked = 0
sensitive_findings = []


def fail(msg):
    failures.append(msg)


text = SKILL_MD.read_text(encoding="utf-8").replace("\r\n", "\n")
fm = ""

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
    if re.search(r"^license:|^metadata:", fm, re.MULTILINE):
        fail("frontmatter: must not carry license:/metadata: keys")

# reference links resolve (markdown-link or backtick form)
link_targets = re.findall(r"\]\((references/[^)]+\.md)\)", text)
link_targets += re.findall(r"`(references/[A-Za-z0-9_.-]+\.md)`", text)
for target in link_targets:
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken reference link: {target}")
if links_checked < 1:
    fail("expected at least 1 references/*.md link in SKILL.md")

# script references resolve (markdown-link or backtick form)
script_targets = re.findall(r"\]\((scripts/[^)]+\.py)\)", text)
script_targets += re.findall(r"`(scripts/[A-Za-z0-9_.-]+\.py)`", text)
for target in script_targets:
    scripts_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken script reference: {target}")

# trigger + anti-trigger convention
leaf_style = ("适用" in fm) and ("不适用" in fm)
hub_style = "触发" in fm
if not (leaf_style or hub_style):
    fail("description lacks 适用/不适用反例 (leaf) or 触发 (hub) convention")

# sensitive content scan — cover SKILL.md + references/*.md + scripts/** so that
# a future commit dropping credentials into a script (e.g. scripts/deploy.sh) is
# caught. cases/ harness is intentionally excluded (template code, self-referential
# regex literals, no project content).
SENSITIVE = [
    (r"\b(192\.168|10\.\d{1,3}|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", "private IP"),
    (r"(?i)password\s*[:=]\s*['\"]?\S+", "credential-like literal"),
    (r"/home/(?!<|\$|\{)[A-Za-z0-9_.-]+/", "personal home path"),
    (r"\bsshpass\b", "ssh credential tool"),
    (r"(?i)\b(api[_-]?key|api[_-]?token|auth[_-]?token|access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*['\"]?\S+", "api key/token/secret"),
    (r"(?i)\btoken\s*[:=]\s*['\"]?[A-Za-z0-9_.\-]{8,}", "bearer-ish token literal"),
    (r"(?i)\bAuthorization\s*[:=]\s*['\"]?\S+", "authorization header"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "openai-style secret key"),
]
scan_files = [SKILL_MD] + sorted(REFS_DIR.glob("*.md"))
if SCRIPTS_DIR.is_dir():
    for ext in ("*.py", "*.sh", "*.md", "*.yaml", "*.yml", "*.json"):
        scan_files.extend(SCRIPTS_DIR.rglob(ext))
scan_files = sorted(set(scan_files))
for path in scan_files:
    body = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    for pattern, why in SENSITIVE:
        for hit in re.finditer(pattern, body):
            line_no = body[:hit.start()].count("\n") + 1
            rel = path.relative_to(SKILL_DIR).as_posix()
            sensitive_findings.append(f"{rel}:{line_no}: {why}: {hit.group(0)!r}")
if sensitive_findings:
    fail(f"sensitive content found: {len(sensitive_findings)} hit(s)")

RESULTS.parent.mkdir(parents=True, exist_ok=True)
result = {
    "success": not failures,
    "failures": len(failures),
    "failure_details": failures,
    "links_checked": links_checked,
    "scripts_checked": scripts_checked,
    "sensitive_findings": len(sensitive_findings),
    "sensitive_details": sensitive_findings,
}
RESULTS.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if not failures else 1)
