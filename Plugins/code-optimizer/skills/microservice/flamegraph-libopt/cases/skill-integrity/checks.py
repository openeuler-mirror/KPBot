#!/usr/bin/env python3
"""Tier-A integrity checks for the flamegraph-libopt skill.

Knowledge-card integrity (the parser functional tests moved to the
flamegraph-parsing skill together with parse_flamegraph_svg.py):
  frontmatter, reference links (local references/ and cross-skill
  ../<skill>/references/), methodology anchors, sensitive-content guard.

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
# 解析器与解析参考已拆分至 flamegraph-parsing skill：本 skill 的参考文档
# 链接包含本地 references/ 与跨 skill ../<skill>/references/ 两种形态，
# 全部必须可解析（源码树与安装平铺布局下两个 skill 为同级目录）。
for target in re.findall(r"\]\((references/[^)]+)\)", text):
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken reference link: {target}")
for target in re.findall(r"\]\((\.\./[^)]+)\)", text):
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken cross-skill link: {target}")
if links_checked < 1:
    fail(f"expected at least 1 reference link, found {links_checked}")

# ---- 3. methodology invariants stay documented ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "Σ(self)",          # integrity self-check before trusting numbers
    "去嵌套",           # de-nest discipline for recursive frames
    "链接指纹",         # link-fingerprint verification before attribution
    "上游",             # upstream-diff-first rule
    "形态前提",         # shape-premise check before porting
    "self 口径",        # self-share definition and its limits
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
    (re.compile(r"[A-Z]:[/\\]Users[/\\]"), "personal windows path"),
]
sensitive_findings = []
for pattern, label in SENSITIVE:
    for m in pattern.finditer(corpus):
        sensitive_findings.append(f"{label}: {m.group(0)[:40]}")
if sensitive_findings:
    fail(f"sensitive content found: {sensitive_findings}")

# ---- report ----
result = {
    "success": not failures,
    "failures": failures,
    "links_checked": links_checked,
    "anchors_found": anchors_found,
    "anchors_total": len(ANCHORS),
    "sensitive_findings": sensitive_findings,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if not failures else 1)
