#!/usr/bin/env python3
"""Tier-A integrity and behavior checks for the openviking-deploy skill.

Guards the parts that are easy to break during later edits:
  1. SKILL.md frontmatter: name matches the directory, description present,
     within the 200-character limit, no XML-like tags in frontmatter
  2. every references/*.md, scripts/* and assets/* path mentioned in the docs
     exists
  3. the default version stays pinned to main (master alias), the official repo
     URL and root-private build root stay fixed; security markers remain
  4. critical anchors are present in the docs
  5. evals/evals.json is well formed and has at least 5 cases
  6. no sensitive content (private IPs, credentials, personal home paths)
  7. offline behavior checks for auth, key permissions, config validation,
     untrusted source dirs, and git-url userinfo rejection
  8. every --flag mentioned in SKILL.md / references exists in the scripts

Writes results/result.json; exits non-zero on any failure.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
behavior_results = {}
behavior_failures = []
doc_flags_checked = 0
doc_flags_missing = []


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
    ASSETS_DIR / "ov.conf.template",
    ASSETS_DIR / "openviking.service.template",
    SCRIPTS_DIR / "preflight_check.sh",
    SCRIPTS_DIR / "install_deps.sh",
    SCRIPTS_DIR / "gen_ov_conf.py",
    SCRIPTS_DIR / "install_openviking.sh",
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
        if len(description) > 200:
            fail("frontmatter: description is {} chars (max 200)".format(len(description)))
        if "<" in description or ">" in description:
            fail("frontmatter: description must not contain angle brackets")
    if re.search(r"<[A-Za-z/?][^>]*>", frontmatter):
        fail("frontmatter: XML-like tag found; avoid angle-bracket placeholders")

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

# ---- 3. version pin and security markers ----
install_text = read(SCRIPTS_DIR / "install_openviking.sh")
preflight_text = read(SCRIPTS_DIR / "preflight_check.sh")
deps_text = read(SCRIPTS_DIR / "install_deps.sh")
common_text = read(SCRIPTS_DIR / "lib" / "common.sh")
gen_text = read(SCRIPTS_DIR / "gen_ov_conf.py")

if not re.search(r"^OPENVIKING_VERSION=main$", install_text, re.MULTILINE):
    fail("install_openviking.sh must default OPENVIKING_VERSION=main")
if not re.search(r'OPENVIKING_VERSION" == "master"', install_text):
    fail("install_openviking.sh must keep the historical master alias mapping")
if not re.search(
    r"^GIT_URL=https://github\.com/volcengine/OpenViking\.git$",
    install_text,
    re.MULTILINE,
):
    fail("install_openviking.sh must default to the official OpenViking repository")
if not re.search(
    r"^BUILD_ROOT=/var/cache/openviking-deploy$", install_text, re.MULTILINE
):
    fail("build root default must be the root-private /var/cache/openviking-deploy")

for marker in (
    "secure_path",
    "prepare_private_dir",
    "check_build_parents",
    "mktemp -d",
    "SOURCE_DIR",
    "openviking-server",
    "doctor",
    "/health",
    "/ready",
    "deploy.env",
    "pip wheel",
    "maturin",
    "systemctl",
    "root_api_key",
    "url_has_userinfo",
    "set_deploy_failed",
    "!/^DEPLOY_STATUS=/",
    "chmod -R u=rwX",
    "0640",
):
    if marker not in install_text:
        fail("install_openviking.sh lost security/function marker: {!r}".format(marker))

for name, body in (
    ("install_openviking.sh", install_text),
    ("preflight_check.sh", preflight_text),
):
    if "file://" in body:
        fail("{} must reject file:// URLs; local sources use --source-dir".format(name))

for marker in ("rustup-init", "sha256", ".sha256", "RUST_MIN=1.91.1"):
    if marker not in deps_text:
        fail("install_deps.sh lost Rust installation marker: {!r}".format(marker))
if "curl" in deps_text and re.search(r"curl[^\n]*\|\s*(ba)?sh", deps_text):
    fail("install_deps.sh must not pipe curl output to a shell")
if "sha256sum" not in deps_text:
    fail("install_deps.sh must verify rustup-init sha256")

for marker in (
    "valid_abs_path",
    "normalize_path",
    "check_system_path",
    "prepare_private_dir",
    "check_build_parents",
    "check_secret_file",
    "untrusted_parent_reason",
    "url_has_userinfo",
):
    if marker not in common_text:
        fail("common.sh lost helper: {!r}".format(marker))
for marker in ("root_api_key", "embedding", "vlm", "workspace", "11434/v1"):
    if marker not in gen_text:
        fail("gen_ov_conf.py lost config marker: {!r}".format(marker))

ref_text = "\n".join(read(p) for p in sorted(REFS_DIR.glob("*.md")))
if re.search(r"\brm\s+-rf\b", ref_text, re.IGNORECASE):
    fail("references must not use forced recursive delete")
if re.search(r"/dev/[t]cp", preflight_text):
    fail("preflight_check.sh must not use the bash TCP pseudo-device")
scan_all = "\n".join([text, ref_text, install_text, preflight_text, deps_text])
if re.search(r"curl\s*\|\s*(ba)?sh", scan_all):
    fail("docs/scripts must not pipe remote content to a shell")

# ---- 4. methodology / safety anchors ----
ANCHORS = [
    (r"默认.*main|main.*默认|未指定版本", "default main/master-alias policy is documented"),
    (r"userinfo", "git URL userinfo rejection is documented"),
    (r"deploy\.env", "deploy.env contract is documented"),
    (r"doctor", "doctor verification is documented"),
    (r"/health", "health endpoint is documented"),
    (r"/ready", "readiness endpoint is documented"),
    (r"0640", "config file permission hardening is documented"),
    (r"root_api_key|root API key", "non-loopback auth requirement is documented"),
    (r"1\.91\.1", "Rust minimum version is documented"),
    (r"--source-dir", "offline source directory is documented"),
    (r"releases?/|release 目录|current", "release/current layout is documented"),
    (r"0700", "build root permission hardening is documented"),
    (r"API key", "API key handling is documented"),
    (r"\.\.", "path traversal rejection is documented"),
    (r"systemd", "systemd deployment is documented"),
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
    if evals_checked < 5:
        fail("expected at least 5 eval cases, found {}".format(evals_checked))

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


# ---- 7. offline behavior checks ----
def record_behavior(name, ok, detail=""):
    behavior_results[name] = {"ok": bool(ok), "detail": str(detail)[:600]}
    if not ok:
        behavior_failures.append("{}: {}".format(name, str(detail)[:600]))


def run_process(argv):
    try:
        return subprocess.run(
            argv,
            cwd=str(SKILL_DIR),
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None


bash_bin = shutil.which("bash")
shell_scripts = [
    SCRIPTS_DIR / "lib" / "common.sh",
    SCRIPTS_DIR / "preflight_check.sh",
    SCRIPTS_DIR / "install_deps.sh",
    SCRIPTS_DIR / "install_openviking.sh",
    Path(__file__).resolve().parent / "run.sh",
]
if bash_bin:
    for script in shell_scripts:
        proc = run_process([bash_bin, "-n", str(script)])
        record_behavior(
            "bash_n:{}".format(script.name),
            proc is not None and proc.returncode == 0,
            "" if proc is None else (proc.stderr or proc.stdout),
        )
else:
    record_behavior("bash_available", False, "bash not found")

gen_script = SCRIPTS_DIR / "gen_ov_conf.py"
installer = SCRIPTS_DIR / "install_openviking.sh"
with tempfile.TemporaryDirectory(prefix="openviking-deploy-check-") as tmp:
    tmp_dir = Path(tmp)
    workspace = tmp_dir / "workspace"
    key600 = tmp_dir / "key600"
    key600.write_text("sk-placeholder\n", encoding="utf-8")
    os.chmod(key600, 0o600)
    key644 = tmp_dir / "key644"
    key644.write_text("sk-placeholder\n", encoding="utf-8")
    os.chmod(key644, 0o644)

    def run_gen(extra):
        proc = run_process([sys.executable, str(gen_script)] + extra)
        if proc is None:
            return 1, "gen_ov_conf.py failed to start"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    non_loopback_conf = tmp_dir / "non-loopback.conf"
    rc, out = run_gen(
        [
            "--out",
            str(non_loopback_conf),
            "--workspace",
            str(workspace),
            "--host",
            "0.0.0.0",
            "--provider",
            "openai",
            "--embedding-model",
            "text-embedding-3-small",
            "--vlm-model",
            "gpt-4o",
            "--api-key-file",
            str(key600),
        ]
    )
    record_behavior(
        "non_loopback_requires_root_key",
        rc != 0 and "root-api-key-file" in out and not non_loopback_conf.exists(),
        out,
    )

    rc, out = run_gen(
        [
            "--out",
            str(tmp_dir / "wide-key.conf"),
            "--workspace",
            str(workspace),
            "--provider",
            "openai",
            "--embedding-model",
            "text-embedding-3-small",
            "--vlm-model",
            "gpt-4o",
            "--api-key-file",
            str(key644),
        ]
    )
    record_behavior("key_file_mode_enforced", rc != 0 and "权限过宽" in out, out)

    ollama_conf = tmp_dir / "ollama.conf"
    rc, out = run_gen(
        [
            "--out",
            str(ollama_conf),
            "--workspace",
            str(workspace),
            "--provider",
            "ollama",
            "--embedding-model",
            "nomic-embed-text",
            "--vlm-model",
            "llava",
        ]
    )
    ollama_text = ollama_conf.read_text(encoding="utf-8") if ollama_conf.exists() else ""
    record_behavior(
        "ollama_without_key_and_v1",
        rc == 0 and "api_key" not in ollama_text and "11434/v1" in ollama_text,
        out,
    )

    incomplete = tmp_dir / "incomplete.conf"
    incomplete.write_text(
        json.dumps(
            {
                "embedding": {"dense": {"provider": "openai"}},
                "vlm": {"provider": "openai"},
                "storage": {"workspace": str(workspace)},
            }
        ),
        encoding="utf-8",
    )
    rc, out = run_gen(["--check", str(incomplete)])
    record_behavior(
        "check_rejects_incomplete_config",
        rc != 0 and "model" in out and "api_key" in out,
        out,
    )

    if bash_bin:
        untrusted_src = tmp_dir / "src"
        untrusted_src.mkdir()
        (untrusted_src / "pyproject.toml").write_text("", encoding="utf-8")
        (untrusted_src / "setup.py").write_text("", encoding="utf-8")
        os.chmod(untrusted_src, 0o777)
        result_dir = tmp_dir / "result"
        proc = run_process(
            [
                bash_bin,
                str(installer),
                "--source-dir",
                str(untrusted_src),
                "--provider",
                "ollama",
                "--embedding-model",
                "m",
                "--vlm-model",
                "v",
                "--workspace",
                str(tmp_dir / "ws"),
                "--result-dir",
                str(result_dir),
                "--dry-run",
            ]
        )
        detail = "" if proc is None else (proc.stdout or "") + (proc.stderr or "")
        record_behavior(
            "untrusted_source_dir_rejected",
            proc is not None and proc.returncode != 0 and not result_dir.exists(),
            detail,
        )

        proc = run_process(
            [
                bash_bin,
                str(installer),
                "--git-url",
                "https://user:token@example.com/repo.git",
                "--provider",
                "ollama",
                "--embedding-model",
                "m",
                "--vlm-model",
                "v",
                "--dry-run",
            ]
        )
        detail = "" if proc is None else (proc.stdout or "") + (proc.stderr or "")
        record_behavior(
            "git_url_userinfo_rejected",
            proc is not None
            and proc.returncode != 0
            and ("userinfo" in detail or "用户名/密码" in detail),
            detail,
        )
    else:
        record_behavior("untrusted_source_dir_rejected", False, "bash not found")
        record_behavior("git_url_userinfo_rejected", False, "bash not found")

# ---- 8. documented flags must exist in scripts ----
script_text = "\n".join(read(p) for p in sorted(SCRIPTS_DIR.glob("*.sh"))) + "\n" + read(gen_script)
# Only SKILL.md flags are part of the skill contract; the guide also shows
# external command flags (git/pip/rustup) that must not be checked here.
documented_flags = set(re.findall(r"--[a-z0-9-]+", text))
documented_flags -= {"--config"}  # OpenViking server flag in the acceptance example
missing_flags = sorted(flag for flag in documented_flags if flag not in script_text)
doc_flags_checked = len(documented_flags)
doc_flags_missing = len(missing_flags)
if missing_flags:
    fail("documented flags not found in scripts: {}".format(", ".join(missing_flags)))

# ---- report ----
RESULT = {
    "skill": SKILL_DIR.name,
    "success": not failures and not behavior_failures,
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
    "behavior_checks": len(behavior_results),
    "behavior_failures": len(behavior_failures),
    "behavior_failure_details": behavior_failures,
    "behavior_details": behavior_results,
    "doc_flags_checked": doc_flags_checked,
    "doc_flags_missing": doc_flags_missing,
    "doc_flags_missing_details": missing_flags,
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
if behavior_failures:
    print("FAILED behavior: {} issue(s)".format(len(behavior_failures)), file=sys.stderr)
    for item in behavior_failures:
        print("  - {}".format(item), file=sys.stderr)
    sys.exit(1)
print("skill-integrity: all checks passed")
sys.exit(0)
