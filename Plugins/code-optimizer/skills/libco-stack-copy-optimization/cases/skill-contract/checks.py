#!/usr/bin/env python3
"""Package-local contract checks for a self-contained libco method package."""

import json
import re
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
SKILLS = PACKAGE / "skills"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"
REQUIRED = ["## 1. 适用对象", "## 2. libco 文件与结构", "## 3. 使用流程", "## 4. 子 skill", "## 5. 验证与门禁"]
SUB_REQUIRED = ["## 适用条件", "## 验证", "## 反例", "## 证据"]
TOKENS = ["d8-d15", "v8-v15", "save/swap", "stnp", "ldnp", "128B", "384B", "q0-q7", "q16-q31", "coctx_swap", "co_memcpy"]
failures = []
parent_path = PACKAGE / "SKILL.md"
parent = parent_path.read_text(encoding="utf-8")
fm = re.match(r"^---\n(.*?)\n---\n", parent, re.DOTALL)
if not fm:
    failures.append("parent: missing frontmatter")
else:
    name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
    if not name or name.group(1) != PACKAGE.name:
        failures.append("parent: name must match package directory")
    desc = re.search(r"^description:\s*(.*?)(?=\n\w+:|\Z)", fm.group(1), re.DOTALL | re.MULTILINE)
    text = desc.group(1) if desc else ""
    if "libco" not in text:
        failures.append("parent: description must name libco")
    for token in TOKENS:
        if token in text:
            failures.append(f"parent: description contains implementation token {token!r}")
for section in REQUIRED:
    if section not in parent:
        failures.append(f"parent: missing section {section!r}")
for target in re.findall(r"\]\((references/[^)]+)\)", parent):
    if not (PACKAGE / target).is_file():
        failures.append(f"parent: broken reference link {target}")
for token in sorted(set(re.findall(r"`(libco-[a-z0-9-]+)`", parent))):
    if not (SKILLS / token / "SKILL.md").is_file():
        failures.append(f"parent: route token {token!r} has no skill directory")

def check_evals(path, expected):
    if not path.is_file():
        failures.append(f"{expected}: missing evals/evals.json")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("skill_name") != expected:
        failures.append(f"{expected}: eval skill_name mismatch")
    if not data.get("evals"):
        failures.append(f"{expected}: empty eval list")

def check_evals_file(path, expected):
    if not path.is_file():
        failures.append(f"{expected}: missing evals/evals.json")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("skill_name") != expected:
        failures.append(f"{expected}: eval skill_name mismatch")
    if not data.get("evals"):
        failures.append(f"{expected}: empty eval list")

check_evals_file(PACKAGE / "evals/evals.json", PACKAGE.name)
subskills = sorted(d for d in SKILLS.iterdir() if (d / "SKILL.md").is_file()) if SKILLS.is_dir() else []
for sub in subskills:
    text = sub.joinpath("SKILL.md").read_text(encoding="utf-8")
    fm_sub = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not fm_sub:
        failures.append(f"{sub.name}: missing frontmatter")
    else:
        n = re.search(r"^name:\s*(\S+)", fm_sub.group(1), re.MULTILINE)
        if not n or n.group(1) != sub.name:
            failures.append(f"{sub.name}: frontmatter name mismatch")
        d = re.search(r"^description:\s*(.*?)(?=\n\w+:|\Z)", fm_sub.group(1), re.DOTALL | re.MULTILINE)
        for token in TOKENS:
            if token in (d.group(1) if d else ""):
                failures.append(f"{sub.name}: description contains implementation token {token!r}")
    for section in SUB_REQUIRED:
        if section not in text:
            failures.append(f"{sub.name}: missing section {section!r}")
    if f"`{sub.name}`" not in parent:
        failures.append(f"{sub.name}: not registered in parent route")
    check_evals(sub / "evals/evals.json", sub.name)

corpus = "\n".join(p.read_text(encoding="utf-8") for p in PACKAGE.rglob("*.md"))
for pattern in [r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", r"(?:/home/|C:\\Users\\|/Users/)[A-Za-z0-9._-]+"]:
    for m in re.finditer(pattern, corpus):
        failures.append(f"sensitive content: {m.group(0)}")

RESULTS.parent.mkdir(parents=True, exist_ok=True)
result = {"success": not failures, "failures": len(failures), "failure_details": failures, "subskills_checked": len(subskills)}
RESULTS.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if not failures else 1)
