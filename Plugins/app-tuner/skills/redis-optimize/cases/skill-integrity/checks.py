#!/usr/bin/env python3
"""Tier-A integrity checks for the redis-optimize knowledge skill.

Guards the parts that are easy to break during later edits:
  1. SKILL.md frontmatter: name matches the directory, description present,
     within the 1024-character limit and free of angle brackets
  2. every references/*.md and scripts/* path mentioned in the docs exists
  3. critical safety/discipline anchors are still present (IRQ re-read after
     `ethtool -L`, ntuple action starts at 0, instance-queue-IRQ mapping,
     NUMA-unknown handling, safe ntuple rollback, RPS/RFS snapshot, etc.)
  4. evals/evals.json is well formed (skill name, unique ids, non-empty
     prompts/expectations)
  5. no sensitive content (private IPs, credentials, personal home paths)

Writes results/result.json; exits non-zero on any failure.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
EVALS = SKILL_DIR / "evals" / "evals.json"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
links_checked = 0
scripts_checked = 0
anchors_found = 0
evals_checked = 0
sensitive_findings = []


def fail(message):
    failures.append(message)


def read(path):
    return path.read_text(encoding="utf-8-sig")


# ---- 0. required files ----
for path in (SKILL_MD, REFS_DIR, EVALS):
    if not path.exists():
        fail("required path missing: {}".format(path.relative_to(SKILL_DIR)))

if not SKILL_MD.is_file():
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "skill": SKILL_DIR.name,
                "success": False,
                "failures": len(failures),
                "failure_details": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)

text = read(SKILL_MD)

# ---- 1. frontmatter ----
fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
if not fm_match:
    fail("SKILL.md has no frontmatter block")
else:
    frontmatter = fm_match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", frontmatter, re.MULTILINE)
    if not name_match:
        fail("frontmatter: missing name")
    elif name_match.group(1) != SKILL_DIR.name:
        fail(
            "frontmatter: name {!r} != directory {!r}".format(
                name_match.group(1), SKILL_DIR.name
            )
        )
    desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
    if not desc_match or not desc_match.group(1).strip():
        fail("frontmatter: missing/empty description")
    else:
        description = desc_match.group(1).strip()
        if len(description) > 1024:
            fail("frontmatter: description is {} chars (max 1024)".format(len(description)))
        if "<" in description or ">" in description:
            fail("frontmatter: description must not contain angle brackets")

# ---- 2. referenced documents and scripts ----
docs = [SKILL_MD] + sorted(REFS_DIR.glob("*.md"))
corpus_parts = []
for path in docs:
    if not path.is_file():
        continue
    body = read(path)
    corpus_parts.append(body)
    for target in sorted(set(re.findall(r"references/[A-Za-z0-9_.-]+\.md", body))):
        links_checked += 1
        if not (SKILL_DIR / target).is_file():
            fail("broken reference link in {}: {}".format(path.name, target))
    for target in sorted(
        set(re.findall(r"scripts/[A-Za-z0-9_.-]+\.(?:py|sh)", body))
    ):
        scripts_checked += 1
        if not (SKILL_DIR / target).is_file():
            fail("script referenced in {} but missing: {}".format(path.name, target))

if links_checked < 1:
    fail("expected at least 1 references/*.md mention, found 0")
if scripts_checked < 1:
    fail("expected at least 1 scripts/* mention, found 0")

# The rollback helper must keep its snapshot/restore contract.
rollback_script = SKILL_DIR / "scripts" / "ntuple_safe_rollback.py"
if not rollback_script.is_file():
    fail("missing safe rollback script: scripts/ntuple_safe_rollback.py")
else:
    rollback_text = read(rollback_script)
    for marker in ("def snapshot(", "def restore(", '"delete"'):
        if marker not in rollback_text:
            fail("rollback script lost contract marker: {!r}".format(marker))

corpus = "\n".join(corpus_parts)

# ---- 3. methodology / safety anchors ----
# Patterns are regexes so inline-code formatting can evolve without making the
# guard brittle, while the underlying discipline stays mandatory.
ANCHORS = [
    (r"改队列后必须重读 /proc/interrupts", "IRQ table must be re-read after -L"),
    (r"action.*?从 0 开始", "ntuple action starts at 0"),
    (r"实例-队列-中断", "instance-queue-IRQ one-to-one mapping"),
    (r"适用性门禁", "applicability gate (virtual NIC / loopback)"),
    (r"单变量", "single-variable re-measurement discipline"),
    (r"禁止无条件删除网卡全部 ntuple 规则", "safe ntuple rollback"),
    (r"统一快照", "unified snapshot before changes"),
    (r"numa_node\s*=\s*-1", "NUMA-unknown handling"),
    (r"仅删本次新增", "rollback deletes only newly added filters"),
    (r"rps_sock_flow_entries", "RPS/RFS snapshot and restore"),
    (r"REDIS_CONF 未设置或文件不存在", "visible redis.conf backup warning"),
    (r"loopback", "loopback traffic is not steered by the NIC"),
]
for pattern, label in ANCHORS:
    if re.search(pattern, corpus):
        anchors_found += 1
    else:
        fail("safety anchor missing: {} (pattern {!r})".format(label, pattern))

# ---- 4. evals.json format ----
try:
    evals_doc = json.loads(read(EVALS))
except (OSError, ValueError) as exc:
    fail("evals/evals.json is not valid JSON: {}".format(exc))
    evals_doc = {}

if not isinstance(evals_doc, dict):
    fail("evals/evals.json root must be an object")
else:
    if evals_doc.get("skill_name") != SKILL_DIR.name:
        fail(
            "evals/evals.json skill_name {!r} != directory {!r}".format(
                evals_doc.get("skill_name"), SKILL_DIR.name
            )
        )
    evals = evals_doc.get("evals")
    if not isinstance(evals, list) or not evals:
        fail("evals/evals.json must contain a non-empty 'evals' list")
        evals = []
    seen_ids = set()
    for index, item in enumerate(evals):
        prefix = "evals[{}]".format(index)
        if not isinstance(item, dict):
            fail("{} must be an object".format(prefix))
            continue
        eval_id = item.get("id")
        if eval_id is None:
            fail("{} missing id".format(prefix))
        elif eval_id in seen_ids:
            fail("{} duplicate id {!r}".format(prefix, eval_id))
        else:
            seen_ids.add(eval_id)
        for field in ("prompt", "expected_output"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                fail("{}.{} must be a non-empty string".format(prefix, field))
        expectations = item.get("expectations")
        if not isinstance(expectations, list) or not expectations:
            fail("{}.expectations must be a non-empty list".format(prefix))
        elif not all(isinstance(x, str) and x.strip() for x in expectations):
            fail("{}.expectations must contain non-empty strings".format(prefix))
        if "files" in item and not isinstance(item["files"], list):
            fail("{}.files must be a list when present".format(prefix))
    evals_checked = len(evals)
    if evals_checked < 3:
        fail("expected at least 3 eval cases, found {}".format(evals_checked))

# ---- 5. sensitive content ----
SENSITIVE = [
    (
        re.compile(
            r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)"
            r"\.\d{1,3}\.\d{1,3}\b"
        ),
        "private IP address",
    ),
    (re.compile(r"(?i)password\s*[:=]\s*\S+"), "credential-like literal"),
    (re.compile(r"/home/[A-Za-z0-9_.-]+/"), "personal home path"),
    (re.compile(r"[Cc]:\\Users\\[A-Za-z0-9_.-]+\\"), "personal home path"),
]
scan_files = [SKILL_MD] + sorted(REFS_DIR.glob("*.md")) + [EVALS]
for path in scan_files:
    if not path.is_file():
        continue
    body = read(path)
    for pattern, kind in SENSITIVE:
        for hit in pattern.finditer(body):
            line_no = body[: hit.start()].count("\n") + 1
            sensitive_findings.append(
                "{}:{}: {}: {!r}".format(path.name, line_no, kind, hit.group(0))
            )
if sensitive_findings:
    fail("sensitive content found: {} hit(s)".format(len(sensitive_findings)))

# ---- report ----
RESULT = {
    "skill": SKILL_DIR.name,
    "success": not failures,
    "failures": len(failures),
    "failure_details": failures,
    "links_checked": links_checked,
    "scripts_checked": scripts_checked,
    "anchors_found": anchors_found,
    "anchors_expected": len(ANCHORS),
    "evals_checked": evals_checked,
    "sensitive_findings": len(sensitive_findings),
    "sensitive_details": sensitive_findings,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(
    json.dumps(RESULT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(RESULT, ensure_ascii=False, indent=2))

if failures:
    print("FAILED: {} issue(s)".format(len(failures)), file=sys.stderr)
    for item in failures:
        print("  - {}".format(item), file=sys.stderr)
    sys.exit(1)
print("skill-integrity: all checks passed")
sys.exit(0)
