#!/usr/bin/env python3
"""Tier-A integrity checks for the binary-compile-forensics skill.

Layers:
  1. SKILL.md frontmatter: name matches directory, description present
  2. referenced assets resolve on disk
  3. methodology anchors stay documented (three evidence chains, exit-code
     contract, tri-state semantics, sibling boundaries)
  4. no sensitive content (private IPs, credentials, home/drive paths)
  5. script contract: bash -n syntax check + no-arg usage exits 2

Writes results/result.json; exits non-zero on any failure.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
SCRIPT = SKILL_DIR / "scripts" / "inspect_build_flags.sh"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
checks = 0


def ok():
    global checks
    checks += 1


def fail(msg):
    failures.append(msg)


def bash_exe():
    exe = shutil.which("bash")
    if exe:
        return exe
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    return None


# ---- 1. frontmatter ----
text = SKILL_MD.read_text(encoding="utf-8")
fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not fm:
    fail("SKILL.md has no frontmatter block")
else:
    name = re.search(r"^name:\s*(\S+)", fm.group(1), re.M)
    if not name or name.group(1) != SKILL_DIR.name:
        fail("frontmatter: name mismatch")
    else:
        ok()
    if re.search(r"^description:\s*\S", fm.group(1), re.M | re.DOTALL):
        ok()
    else:
        fail("frontmatter: missing description")

# ---- 2. assets resolve ----
links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text)
if len(links) < 2:
    fail(f"expected >=2 asset links, found {len(links)}")
for t in links:
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"broken link: {t}")
for t in ("references/compile-forensics-guide.md", "scripts/inspect_build_flags.sh"):
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"expected asset missing: {t}")

# ---- 3. methodology anchors ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "DW_AT_producer",          # chain 2: authoritative flags fingerprint
    ".comment",                # chain 1: compiler version section
    "SIMD 兜底",                # chain 3: sampling fallback
    "退出码",                   # exit-code contract documented
    "blocked",                 # tri-state semantics
    "hot-library-version-gap", # sibling boundary declared
    "flamegraph-libopt",       # sibling boundary declared
    "产物",                     # product-side (not build-system) forensics
]
for a in ANCHORS:
    if a in corpus:
        ok()
    else:
        fail(f"anchor missing: {a!r}")

# ---- 4. sensitive content ----
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "home path"),
    (re.compile(r"\b[A-Za-z]:[/\\]Users\b"), "windows user path"),
]
sensitive = []
for path in [SKILL_MD] + sorted(REFS_DIR.glob("*.md")) + [SKILL_DIR / "scripts" / "README.md"]:
    body = path.read_text(encoding="utf-8")
    for pat, label in SENSITIVE:
        for m in pat.finditer(body):
            sensitive.append(f"{label}: {path.name}: {m.group(0)!r}")
if sensitive:
    failures.extend(sensitive)
else:
    ok()

# ---- 5. script contract ----
bash = bash_exe()
if bash is None:
    fail("bash not found for script contract checks")
else:
    r = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
    if r.returncode == 0:
        ok()
    else:
        fail(f"bash -n syntax check failed: {r.stderr.strip()[:200]}")
    r2 = subprocess.run([bash, str(SCRIPT)], capture_output=True, text=True)
    if r2.returncode == 2 and "用法" in (r2.stdout + r2.stderr):
        ok()
    else:
        fail(f"no-arg usage: expected exit 2 with usage, got rc={r2.returncode}")

# ---- report ----
result = {
    "skill": SKILL_DIR.name,
    "case": "skill-integrity",
    "pass": not failures,
    "checks": checks,
    "sensitive_findings": sensitive,
    "failures": failures,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if not failures else 1)
