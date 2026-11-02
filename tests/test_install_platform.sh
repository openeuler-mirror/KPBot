#!/usr/bin/env bash
#
# Regression tests for platform-aware KPBot installation.
#
# Verifies that install.sh:
#   - claude   mode: installs the plugin skills, keeps claude-only skills
#   - opencode mode: installs supported plugins, skips claude-only skills
#   - both modes: memory modules, bundled guides, manifest and entry points
#                   declared via plugin.json `opencode.exclude`
#
# Usage:
#   bash tests/test_install_platform.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/install.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/kpbot install test.XXXXXX")"
# Only remove the directory allocated by this test, never an unchecked path.
case "$TMPROOT" in "${TMPDIR:-/tmp}"/kpbot\ install\ test.*) ;; *) exit 1 ;; esac
trap 'rm -rf "${TMPROOT:?}"' EXIT

pass=0
fail=0

check() {
    local desc="$1"
    local cond="$2"
    if eval "$cond"; then
        echo "  PASS  $desc"
        pass=$((pass + 1))
    else
        echo "  FAIL  $desc"
        fail=$((fail + 1))
    fi
}

run_install() {
    local tool="$1"
    local dir="$TMPROOT/$tool"
    mkdir -p "$dir"
    check "$tool installer completes" 'bash "$INSTALLER" project "$tool" "$dir" >"$TMPROOT/$tool-install.log" 2>&1'
}

memory_modules_exist() {
    local root="$1/scripts/kpbot_memory"
    local name
    for name in cli/__init__.py cli/configure.py cli/backend.py backend_config.py northbound.py \
                backends/openviking_http.py backends/mem0_http.py backends/tencentdb_http.py; do
        [ -f "$root/$name" ] || return 1
    done
}

memory_manifest_contains() {
    python3 -c 'import json,sys; assert "kpbot-memory" in json.load(open(sys.argv[1]))["installed_skills"]' "$1"
}

memory_links_resolve() {
    python3 - "$1" <<'PY'
from pathlib import Path
import re
import sys
root = Path(sys.argv[1]).resolve()
for page in [root / "SKILL.md", *root.glob("references/*.md")]:
    for link in re.findall(r"\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
        link = link.strip("<>")
        if link.startswith(("https://", "http://", "#")):
            continue
        target = (page.parent / link.split("#", 1)[0]).resolve()
        assert target.is_relative_to(root) and target.exists(), (page.name, link)
PY
}

check_memory() {
    local memory="$1/kpbot-memory"
    local manifest="$(dirname "$1")/kpbot-manifest.json"
    check "installs memory skill" '[ -f "$memory/SKILL.md" ]'
    check "copies all memory modules" 'memory_modules_exist "$memory"'
    check "excludes source-only development helper" '[ ! -e "$memory/scripts/setup-openviking.py" ]'
    check "bundles onboarding guides" '[ -f "$memory/references/getting-started.md" ] && [ -f "$memory/references/deployment.md" ]'
    check "installed memory links resolve locally" 'memory_links_resolve "$memory"'
    check "manifest contains memory" 'memory_manifest_contains "$manifest"'
    check "copied backend help runs" 'python3 "$memory/scripts/kpbot-memory.py" backend --help >/dev/null'
    check "copied configure help runs" 'python3 "$memory/scripts/kpbot-memory.py" configure --help >/dev/null'
}

echo "== claude mode =="
run_install claude
CLAUDE_SKILLS="$TMPROOT/claude/.claude/skills"
check "installs code-optimizer skills"        '[ -d "$CLAUDE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$CLAUDE_SKILLS/kpbot-app-tuner" ]'
check "keeps claude-only drive skill"         '[ -d "$CLAUDE_SKILLS/drive-claude-optimize-pipeline" ]'
check "keeps claude-only batch skill"         '[ -d "$CLAUDE_SKILLS/batch-drive-optimize-pipeline" ]'
check_memory "$CLAUDE_SKILLS"

echo "== opencode mode =="
run_install opencode
OPENCODE_SKILLS="$TMPROOT/opencode/.opencode/skills"
check "installs code-optimizer skills"        '[ -d "$OPENCODE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$OPENCODE_SKILLS/kpbot-app-tuner" ]'
check "excludes claude-only drive skill"      '[ ! -e "$OPENCODE_SKILLS/drive-claude-optimize-pipeline" ]'
check "excludes claude-only batch skill"      '[ ! -e "$OPENCODE_SKILLS/batch-drive-optimize-pipeline" ]'
check_memory "$OPENCODE_SKILLS"

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
