#!/usr/bin/env python3
"""Tier-A integrity checks for the vectordbbench-deploy skill.

Guards:
  1. SKILL.md frontmatter (name/description, no XML-like tags)
  2. references/scripts/assets links exist
  3. default version is pinned (with commit verification), root-private build
     root, Python 3.11+/venv/pip/PIP_INDEX_URL[_FILE]/extras markers stay
  4. critical doc anchors (pinned version, Python 3.11, venv, extras, wheelhouse,
     commit/package version, current/CLI link, deploy.env, 0700, path ..)
  5. evals/evals.json structure and at least 5 cases
  6. no sensitive content (private IPs, credentials, personal home paths)
  7. no forced recursive delete in references, no /dev/tcp probe, no
     curl-pipe-shell pattern in docs/scripts

Writes results/result.json; exits non-zero on failure.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
ASSETS_DIR = SKILL_DIR / "assets"
SCRIPTS_DIR = SKILL_DIR / "scripts"
EVALS = SKILL_DIR / "evals" / "evals.json"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
links_checked = 0
scripts_checked = 0
assets_checked = 0
anchors_found = 0
evals_checked = 0
sensitive_findings = []


def fail(message):
    failures.append(message)


def read(path):
    return path.read_text(encoding="utf-8-sig")


required = [
    SKILL_MD,
    REFS_DIR,
    ASSETS_DIR,
    SCRIPTS_DIR,
    EVALS,
    ASSETS_DIR / "smoke_check.sh",
    SCRIPTS_DIR / "preflight_check.sh",
    SCRIPTS_DIR / "install_deps.sh",
    SCRIPTS_DIR / "install_vectordbbench.sh",
    SCRIPTS_DIR / "lib" / "common.sh",
]
for path in required:
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
    if re.search(r"<[A-Za-z/?][^>]*>", frontmatter):
        fail("frontmatter: XML-like tag found; avoid angle-bracket placeholders")

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
        set(re.findall(r"scripts/[A-Za-z0-9_./-]+\.(?:py|sh)", body))
    ):
        scripts_checked += 1
        if not (SKILL_DIR / target).is_file():
            fail("script referenced in {} but missing: {}".format(path.name, target))
    for target in sorted(set(re.findall(r"assets/[A-Za-z0-9_.-]+", body))):
        assets_checked += 1
        if not (SKILL_DIR / target).is_file():
            fail("asset referenced in {} but missing: {}".format(path.name, target))

if links_checked < 1:
    fail("expected at least 1 references/*.md mention, found 0")
if scripts_checked < 1:
    fail("expected at least 1 scripts/* mention, found 0")
if assets_checked < 1:
    fail("expected at least 1 assets/* mention, found 0")

corpus = "\n".join(corpus_parts)

install_text = read(SCRIPTS_DIR / "install_vectordbbench.sh")
preflight_text = read(SCRIPTS_DIR / "preflight_check.sh")
common_text = read(SCRIPTS_DIR / "lib" / "common.sh")
deps_text = read(SCRIPTS_DIR / "install_deps.sh")

if not re.search(r"^VDB_PINNED_VERSION=v[0-9][0-9A-Za-z._-]*$", install_text, re.MULTILINE):
    fail("install_vectordbbench.sh must pin VDB_PINNED_VERSION")
if not re.search(r"^VDB_PINNED_COMMIT=[0-9a-f]{40}$", install_text, re.MULTILINE):
    fail("install_vectordbbench.sh must hardcode VDB_PINNED_COMMIT")
if not re.search(r"^VDB_VERSION=\$VDB_PINNED_VERSION$", install_text, re.MULTILINE):
    fail("install_vectordbbench.sh default must be VDB_PINNED_VERSION")
if not re.search(r"^BUILD_ROOT=/var/cache/vectordbbench-deploy$", install_text, re.MULTILINE):
    fail("build root default must be /var/cache/vectordbbench-deploy")

for marker in (
    "find_python",
    "prepare_private_dir",
    "check_build_parents",
    "mktemp -d",
    "PIP_INDEX_URL",
    "--no-build-isolation",
    "--extras",
    "deploy.env",
    "vectordbbench",
    "--allow-moving-branch",
    "--pip-index-url-file",
    "VDB_PINNED_COMMIT",
    "stat -L",
):
    if marker not in install_text:
        fail("install_vectordbbench.sh lost marker: {!r}".format(marker))
for marker in (
    "find_python",
    "valid_abs_path",
    "normalize_path",
    "check_system_path",
    "is_reserved_root",
    "is_system_install_tree",
    "is_system_bin_tree",
    "unsafe_parent_of",
    "mask_url",
    "url_has_userinfo",
    "stat -L",
):
    if marker not in common_text:
        fail("common.sh lost marker: {!r}".format(marker))
for marker in ("--pip-index-url-file", "VDB_PINNED_VERSION", "unsafe_parent_of", "stat -L"):
    if marker not in preflight_text:
        fail("preflight_check.sh lost marker: {!r}".format(marker))
if "python3 -m venv --help" not in deps_text:
    fail("install_deps.sh should warn on missing venv")

ref_text = "\n".join(read(p) for p in sorted(REFS_DIR.glob("*.md")))
if re.search(r"\brm\s+-rf\b", ref_text, re.IGNORECASE):
    fail("references must not use forced recursive delete")
if re.search(r"/dev/[t]cp", preflight_text):
    fail("preflight_check.sh must not use the bash TCP pseudo-device")
scan_all = "\n".join([text, ref_text, install_text, preflight_text, deps_text, common_text])
if re.search(r"curl\s*\|\s*(ba)?sh", scan_all):
    fail("docs/scripts must not pipe remote content to a shell")

ANCHORS = [
    (r"固定版本|VDB_PINNED_VERSION|--allow-moving-branch", "pinned version / moving branch opt-in"),
    (r"Python\s*3\.11\+|Python >= 3\.11|3\.11\+", "Python 3.11+ requirement"),
    (r"venv", "venv usage"),
    (r"extras", "database extras"),
    (r"wheelhouse|离线", "offline wheelhouse"),
    (r"COMMIT|commit", "commit recording"),
    (r"PACKAGE_VERSION|包版本", "package version recording"),
    (r"current", "current symlink"),
    (r"deploy\.env", "deploy.env"),
    (r"0700", "build root hardening"),
    (r"pip-index-url-file|pip 索引文件", "credential-safe pip index channel"),
    (r"PIP_INDEX_URL", "pip index credential handling"),
    (r"危险操作警告", "destructive cleanup warning"),
    (r"\.\.", "path traversal rejection"),
]
for pattern, label in ANCHORS:
    if re.search(pattern, corpus):
        anchors_found += 1
    else:
        fail("safety anchor missing: {} (pattern {!r})".format(label, pattern))

try:
    evals_doc = json.loads(read(EVALS))
except (OSError, ValueError) as exc:
    fail("evals/evals.json is not valid JSON: {}".format(exc))
    evals_doc = {}

if not isinstance(evals_doc, dict):
    fail("evals/evals.json root must be an object")
else:
    if evals_doc.get("skill_name") != SKILL_DIR.name:
        fail("evals skill_name mismatch")
    evals = evals_doc.get("evals")
    if not isinstance(evals, list) or not evals:
        fail("evals must be a non-empty list")
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
    if evals_checked < 5:
        fail("expected at least 5 eval cases, found {}".format(evals_checked))

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

RESULT = {
    "skill": SKILL_DIR.name,
    "success": not failures,
    "failures": len(failures),
    "failure_details": failures,
    "links_checked": links_checked,
    "scripts_checked": scripts_checked,
    "assets_checked": assets_checked,
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
