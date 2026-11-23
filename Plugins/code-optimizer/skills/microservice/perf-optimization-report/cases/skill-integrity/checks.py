#!/usr/bin/env python3
"""Tier-A integrity checks for the perf-optimization-report skill.

Validates that the skill stays well-formed, self-consistent, and honest
as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present
  2. every references/*.md and scripts/* link in SKILL.md resolves on disk
  2b. backticked skill names in SKILL.md resolve to real skills/* directories
      (phantom-reference guard: e.g. naming an unmerged/nonexistent skill)
  3. key methodology invariants stay documented (five-chapter skeleton,
     evidence inventory, authoritative-caliber precedence, discarded-protocol
     disclosure, cost model + contention amplification, fault-injection
     barrier, repo-relative paths, -traces fold source, Total samples weight
     gate, experiment_summary ledger)
  4. no sensitive content (private IPs, credentials, personal home paths,
     Windows drive paths)
  5. fold_traces.py functional contract on a realistic -traces fixture:
     stack reversal, unit normalization, inline stripping, block merging,
     exact collapsed output; plus the three failure paths (usage / refuse
     overwrite / empty-parse no-silent-success)

Writes results/result.json; exits non-zero on any failure.
"""

import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
SCRIPTS_DIR = SKILL_DIR / "scripts"
FOLD = SCRIPTS_DIR / "fold_traces.py"
FIXTURE = CASE_DIR / "fixtures" / "traces_sample.txt"
RESULTS = CASE_DIR / "results" / "result.json"

failures = []
check_count = 0


def fail(msg):
    failures.append(msg)


def ok():
    global check_count
    check_count += 1


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
    else:
        ok()
    if re.search(r"^description:\s*\S", fm, re.MULTILINE | re.DOTALL):
        ok()
    else:
        fail("frontmatter: missing/empty description")

# ---- 2. reference / script links resolve ----
links_checked = 0
for target in re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text):
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken link in SKILL.md: {target}")
if links_checked < 3:
    fail(f"expected at least 3 links (2 references + 1 script), found {links_checked}")
else:
    for t in ("references/report-template.md", "references/flamegraph-pipeline.md", "scripts/fold_traces.py"):
        if (SKILL_DIR / t).is_file():
            ok()
        else:
            fail(f"expected asset missing: {t}")

# ---- 2b. backticked skill names must exist in this repo (phantom guard) ----
# A kebab-case name in backticks (not this skill itself) must resolve to a
# skills/<name>/ or skills/<domain>/<name>/ directory with a SKILL.md --
# otherwise the skill names something the repo does not have (e.g. an
# unmerged MR), and its only named handoff contract is dead on arrival.
skills_root = SKILL_DIR.parent.parent
existing_skills = set()
if skills_root.is_dir():
    for d in skills_root.iterdir():
        if d.is_dir() and (d / "SKILL.md").is_file():
            existing_skills.add(d.name)
        if d.is_dir():
            for sub in d.iterdir():
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    existing_skills.add(sub.name)
candidates = set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", text))
phantom_refs = sorted(candidates - {SKILL_DIR.name} - existing_skills)
if phantom_refs:
    fail("backticked skill name(s) not present in this repo: "
         + ", ".join(phantom_refs)
         + " (reference only real, already-merged skills)")
else:
    if candidates - {SKILL_DIR.name}:
        ok()  # at least one real cross-skill reference verified

# ---- 3. methodology invariants stay documented ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "五章",                    # five-chapter skeleton
    "证据清单",                # evidence inventory before writing
    "权威口径",                # authoritative-caliber precedence
    "废协议",                  # discarded-protocol disclosure
    "成本模型",                # cost model is mandatory
    "争用放大",                # contention-amplification attribution
    "故障注入",                # fault-injection verification barrier
    "仓内相对路径",            # repo-relative paths only
    "-traces",                 # fold source iron law
    "Total samples",           # post-fold weight gate
    "experiment_summary",      # the experiment ledger contract
]
anchors_found = 0
for anchor in ANCHORS:
    if anchor in corpus:
        anchors_found += 1
    else:
        fail(f"methodology anchor missing from corpus: {anchor!r}")
if anchors_found == len(ANCHORS):
    ok()

# ---- 4. no sensitive content ----
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "personal home path"),
    (re.compile(r"\b[A-Za-z]:[/\\]Users\b"), "windows user path"),
]
sensitive_findings = []
for path in [SKILL_MD] + sorted(REFS_DIR.glob("*.md")) + [SCRIPTS_DIR / "README.md"]:
    body = path.read_text(encoding="utf-8")
    for pat, label in SENSITIVE:
        for m in pat.finditer(body):
            sensitive_findings.append(f"{label}: {path.name}: {m.group(0)!r}")
if sensitive_findings:
    failures.extend(sensitive_findings)
else:
    ok()

# ---- 5. fold_traces.py functional contract ----
def run_fold(args, expect_dst=None):
    return subprocess.run(
        [sys.executable, str(FOLD)] + args,
        capture_output=True, text=True, timeout=60)

workdir = CASE_DIR / "results" / "fold_work"
if workdir.exists():
    shutil.rmtree(workdir)  # keep the case idempotent across re-runs
workdir.mkdir(parents=True, exist_ok=True)

# 5.1 happy path: fold the fixture
dst1 = workdir / "out.collapsed"
r = run_fold([str(FIXTURE), str(dst1)])
if r.returncode != 0:
    fail(f"5.1 fold fixture: exit {r.returncode}, stderr: {r.stderr.strip()[:200]}")
else:
    ok()
    summary = r.stdout
    if "collapsed stacks: 2" not in summary:
        fail(f"5.1 expected 2 collapsed stacks, stdout: {summary.strip()[:200]}")
    else:
        ok()
    if "total weight: 1730 ms" not in summary:
        fail(f"5.1 expected total weight 1730 ms (460+40 merged, 1.23s->1230), stdout: {summary.strip()[:200]}")
    else:
        ok()
    if "(inline)" in dst1.read_text(encoding="utf-8"):
        fail("5.1 inline suffix not stripped in collapsed output")
    else:
        ok()
    lines = sorted(dst1.read_text(encoding="utf-8").strip().splitlines())
    expected_lines = sorted([
        "main.decodeFrame;main.processPayload;main.handleRequest 1230",
        "runtime.schedule;runtime.findRunnable;runtime.stealWork 500",
    ])
    if lines != expected_lines:
        fail(f"5.1 collapsed output mismatch: {lines!r} != {expected_lines!r}")
    else:
        ok()
    # root-first order: first frame of each stack is the shallowest caller
    for line in lines:
        root = line.split(" ")[0].split(";")[0]
        if root not in ("main.decodeFrame", "runtime.schedule"):
            fail(f"5.1 root-first order violated: {root!r}")
    else:
        ok()

# 5.2 usage error: wrong argument count -> exit 2
r = run_fold([])
if r.returncode != 2:
    fail(f"5.2 usage error: expected exit 2, got {r.returncode}")
else:
    ok()

# 5.3 empty parse: input with no trace blocks -> exit 1, no dst written
empty_src = workdir / "empty.txt"
empty_src.write_text("not a pprof -traces file\n", encoding="utf-8")
dst3 = workdir / "empty.collapsed"
r = run_fold([str(empty_src), str(dst3)])
if r.returncode != 1:
    fail(f"5.3 empty parse: expected exit 1 (no silent success), got {r.returncode}")
else:
    ok()
if dst3.exists():
    fail("5.3 empty parse: dst file must not be written on failure")
else:
    ok()

# 5.4 refuse overwrite: existing dst -> exit 2
dst4 = workdir / "exists.collapsed"
dst4.write_text("precious data\n", encoding="utf-8")
r = run_fold([str(FIXTURE), str(dst4)])
if r.returncode != 2:
    fail(f"5.4 refuse overwrite: expected exit 2, got {r.returncode}")
else:
    ok()
if dst4.read_text(encoding="utf-8") != "precious data\n":
    fail("5.4 refuse overwrite: existing dst was modified")
else:
    ok()


# 5.4b dst-is-dir: script should exit 2 with "destination is a directory"
try:
    import tempfile as _tf
    _d = _tf.mkdtemp()
    _r = subprocess.run([sys.executable, str(FOLD), str(FIXTURE), _d],
                       capture_output=True, text=True, timeout=30)
    if _r.returncode == 2 and "directory" in (_r.stderr or "").lower():
        ok()
    else:
        fail(f"5.4b dst-is-dir: rc={_r.returncode}, stderr={(_r.stderr or '')[:60]}")
    import shutil as _sh
    _sh.rmtree(_d, ignore_errors=True)
except Exception as exc:
    fail(f"5.4b dst-is-dir raised: {exc}")

# 5.4c dst-parent-missing: script should exit 2 with "does not exist"
try:
    _r = subprocess.run([sys.executable, str(FOLD), str(FIXTURE), os.path.join(os.sep, "nonexistent", "dir", "out.txt")],
                       capture_output=True, text=True, timeout=30)
    if _r.returncode == 2 and ("not exist" in (_r.stderr or "").lower() or "not a directory" in (_r.stderr or "").lower()):
        ok()
    else:
        fail(f"5.4c dst-parent-missing: rc={_r.returncode}, stderr={(_r.stderr or '')[:60]}")
except Exception as exc:
    fail(f"5.4c dst-parent-missing raised: {exc}")

# ---- report ----
result = {
    "skill": SKILL_DIR.name,
    "case": CASE_DIR.name,
    "pass": not failures,
    "checks": check_count,
    "anchors": anchors_found,
    "links": links_checked,
    "sensitive_findings": sensitive_findings,
    "failures": failures,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if not failures else 1)
