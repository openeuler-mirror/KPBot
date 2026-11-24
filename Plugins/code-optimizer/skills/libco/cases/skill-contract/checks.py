#!/usr/bin/env python3
"""Contract checks for the libco domain and its flat member skills."""

import json
import re
import sys
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[2]
DOMAIN_MD = DOMAIN / "DOMAIN.md"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"
REQUIRED_DOMAIN = ["## 成员", "## 目录约定", "## 成员登记纪律", "## Review"]
SUB_REQUIRED = ["## 适用条件", "## 验证", "## 反例", "## 证据"]
TOKENS = ["d8-d15", "v8-v15", "save/swap", "stnp", "ldnp", "128B", "384B", "q0-q7", "q16-q31", "coctx_swap", "co_memcpy"]
failures = []

text = DOMAIN_MD.read_text(encoding="utf-8") if DOMAIN_MD.is_file() else ""
if not text:
    failures.append("missing DOMAIN.md")
for section in REQUIRED_DOMAIN:
    if section not in text:
        failures.append(f"DOMAIN.md missing {section!r}")

skills = sorted(
    p for p in DOMAIN.iterdir()
    if p.is_dir() and (p / "SKILL.md").is_file() and p.name not in ("cases", "references")
)
for skill in skills:
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---\n", body, re.DOTALL)
    if not fm:
        failures.append(f"{skill.name}: missing frontmatter")
    else:
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        if not name or name.group(1) != skill.name:
            failures.append(f"{skill.name}: frontmatter name mismatch")
        desc = re.search(r"^description:\s*(.*?)(?=\n\w+:|\Z)", fm.group(1), re.DOTALL | re.MULTILINE)
        desc_text = desc.group(1) if desc else ""
        if "libco" not in desc_text:
            failures.append(f"{skill.name}: description must name libco")
        for token in TOKENS:
            if token in desc_text:
                failures.append(f"{skill.name}: description contains implementation token {token!r}")
    for section in SUB_REQUIRED:
        if section not in body:
            failures.append(f"{skill.name}: missing section {section!r}")
    if skill.name not in text:
        failures.append(f"{skill.name}: not registered in DOMAIN.md")
    if f"]({skill.name}/SKILL.md)" not in text:
        failures.append(f"{skill.name}: missing domain link")
    eval_path = skill / "evals" / "evals.json"
    if not eval_path.is_file():
        failures.append(f"{skill.name}: missing evals/evals.json")
    else:
        data = json.loads(eval_path.read_text(encoding="utf-8"))
        if data.get("skill_name") != skill.name:
            failures.append(f"{skill.name}: eval skill_name mismatch")
        if not data.get("evals"):
            failures.append(f"{skill.name}: empty eval list")

corpus = text + "\n" + "\n".join(p.read_text(encoding="utf-8") for p in DOMAIN.rglob("*.md"))
for pattern in [r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", r"(?:/home/|C:\\Users\\|/Users/)[A-Za-z0-9._-]+"]:
    for m in re.finditer(pattern, corpus):
        failures.append(f"sensitive content: {m.group(0)}")

RESULTS.parent.mkdir(parents=True, exist_ok=True)
result = {"success": not failures, "failures": len(failures), "failure_details": failures, "skills_checked": len(skills)}
RESULTS.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if not failures else 1)
