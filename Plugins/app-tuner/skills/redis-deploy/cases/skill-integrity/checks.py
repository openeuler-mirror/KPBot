#!/usr/bin/env python3
"""Tier-A integrity checks for the redis-deploy skill.

Guards the parts that are easy to break during later edits:
  1. SKILL.md frontmatter: name matches the directory, description present,
     within the 1024-character limit and free of angle brackets
  2. every references/*.md, scripts/*.sh and assets/* path mentioned in the
     docs exists, and the referenced templates keep their placeholders
  3. the default version stays pinned to 7.0.15 and its sha256 stays in
     assets/checksums.txt
  4. critical safety anchors are still present in the docs (loopback bind,
     protected-mode, password never in argv, sha256 verification, read-only
     preflight, rollback, deploy.env contract)
  5. evals/evals.json is well formed and has at least 4 cases
  6. no sensitive content (private IPs, credentials, personal home paths)

Writes results/result.json; exits non-zero on any failure.
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

DEFAULT_VERSION = "7.0.15"
DEFAULT_SHA256 = "98066f5363504b26c34dd20fbcc3c957990d764cdf42576c836fc021073f4341"

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


# ---- 0. required files ----
required = [
    SKILL_MD,
    REFS_DIR,
    ASSETS_DIR,
    SCRIPTS_DIR,
    EVALS,
    ASSETS_DIR / "checksums.txt",
    ASSETS_DIR / "redis.conf.template",
    ASSETS_DIR / "redis.service.template",
    SCRIPTS_DIR / "preflight_check.sh",
    SCRIPTS_DIR / "install_deps.sh",
    SCRIPTS_DIR / "install_redis.sh",
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
    if re.search(r"<[A-Za-z/?][^>]*>", frontmatter):
        fail("frontmatter: XML-like tag found; use {version} instead")

# ---- 2. referenced documents / scripts / assets ----
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

# ---- 3. version pin and templates ----
install_text = read(SCRIPTS_DIR / "install_redis.sh")
preflight_text = read(SCRIPTS_DIR / "preflight_check.sh")
checksums_text = read(ASSETS_DIR / "checksums.txt")
conf_template = read(ASSETS_DIR / "redis.conf.template")
unit_template = read(ASSETS_DIR / "redis.service.template")

if not re.search(r"^VERSION=7\.0\.15$", install_text, re.MULTILINE):
    fail("install_redis.sh must default VERSION=7.0.15")
if not re.search(r"^VERSION=7\.0\.15$", preflight_text, re.MULTILINE):
    fail("preflight_check.sh must default VERSION=7.0.15")
if DEFAULT_VERSION not in checksums_text or DEFAULT_SHA256 not in checksums_text:
    fail("assets/checksums.txt must pin 7.0.15 to its official sha256")
if "REDISCLI_AUTH" not in install_text:
    fail("install_redis.sh must authenticate redis-cli via REDISCLI_AUTH")
if re.search(r"redis-cli[^\n]*\s-a\s", install_text):
    fail("install_redis.sh must not pass a password with redis-cli -a")

ref_text = "\n".join(read(p) for p in sorted(REFS_DIR.glob("*.md")))
if re.search(r"\brm\s+-rf\b", ref_text, re.IGNORECASE):
    fail("references must not use forced recursive delete; use a confirmation-gated non-forced example")
if re.search(r"/dev/[t]cp", preflight_text):
    fail("preflight_check.sh must not use the bash TCP pseudo-device; use curl/nc/wget probing")

for placeholder in ("@PORT@", "@BIND@", "@DATA_DIR@", "@LOG_FILE@", "@PID_FILE@"):
    if placeholder not in conf_template:
        fail("redis.conf.template lost placeholder {}".format(placeholder))
for placeholder in ("@NAME@", "@PREFIX@", "@CONF@", "@USER@"):
    if placeholder not in unit_template:
        fail("redis.service.template lost placeholder {}".format(placeholder))

# ---- 4. methodology / safety anchors ----
ANCHORS = [
    (r"默认安装 Redis 7\.0\.15|安装 Redis 7\.0\.15", "default version 7.0.15 is documented"),
    (r"sha256", "sha256 verification is documented"),
    (r"checksums\.txt", "checksum manifest is referenced"),
    (r"REDISCLI_AUTH", "client auth uses REDISCLI_AUTH"),
    (r"requirepass", "requirepass handling is documented"),
    (r"127\.0\.0\.1", "loopback bind default is documented"),
    (r"protected-mode", "protected-mode is documented"),
    (r"--password-file", "password file option is documented"),
    (r"--allow-insecure-remote", "insecure remote opt-out is explicit"),
    (r"deploy\.env", "deploy.env contract is documented"),
    (r"只读", "preflight is read-only"),
    (r"回滚", "rollback guidance is documented"),
    (r"preflight_check\.sh", "preflight script is indexed"),
    (r"--tarball", "offline tarball path is documented"),
    (r"危险操作警告", "destructive uninstall warning is present"),
]
for pattern, label in ANCHORS:
    if re.search(pattern, corpus):
        anchors_found += 1
    else:
        fail("safety anchor missing: {} (pattern {!r})".format(label, pattern))

# ---- 5. evals.json format ----
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
    if evals_checked < 4:
        fail("expected at least 4 eval cases, found {}".format(evals_checked))

# ---- 6. sensitive content ----
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
