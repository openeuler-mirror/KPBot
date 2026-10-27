#!/usr/bin/env bash
#
# Regression tests for platform-aware KPBot installation.
#
# Verifies that install.sh:
#   - claude   mode: installs code-optimizer + app-tuner, keeps claude-only
#                   skills
#   - opencode mode: installs both plugins, skips claude-only skills
#                   declared via plugin.json `opencode.exclude`
#
# Usage:
#   bash tests/test_install_platform.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/install.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/kpbot-install-test.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

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
    bash "$INSTALLER" project "$tool" "$dir" >/dev/null 2>&1
}

echo "== claude mode =="
run_install claude
CLAUDE_SKILLS="$TMPROOT/claude/.claude/skills"
check "installs code-optimizer skills"        '[ -d "$CLAUDE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$CLAUDE_SKILLS/kpbot-app-tuner" ]'
check "keeps claude-only drive skill"         '[ -d "$CLAUDE_SKILLS/drive-claude-optimize-pipeline" ]'
check "keeps claude-only batch skill"         '[ -d "$CLAUDE_SKILLS/batch-drive-optimize-pipeline" ]'
check "installs Faiss domain entry"           '[ -f "$CLAUDE_SKILLS/faiss-optimize/SKILL.md" ]'
check "includes Faiss workflow initializer"   '[ -f "$CLAUDE_SKILLS/faiss-optimize/workflows/scripts/init_run.py" ]'
check "registers Faiss reviewer agent"        '[ -f "$TMPROOT/claude/.claude/agents/faiss-reviewer.md" ]'

echo "== opencode mode =="
run_install opencode
OPENCODE_SKILLS="$TMPROOT/opencode/.opencode/skills"
check "installs code-optimizer skills"        '[ -d "$OPENCODE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$OPENCODE_SKILLS/kpbot-app-tuner" ]'
check "excludes claude-only drive skill"      '[ ! -e "$OPENCODE_SKILLS/drive-claude-optimize-pipeline" ]'
check "excludes claude-only batch skill"      '[ ! -e "$OPENCODE_SKILLS/batch-drive-optimize-pipeline" ]'
check "installs Faiss domain entry"           '[ -f "$OPENCODE_SKILLS/faiss-optimize/SKILL.md" ]'
check "preserves Faiss benchmark contract"    '[ -f "$OPENCODE_SKILLS/faiss-ivfpq-optimization/references/benchmark-contract.json" ]'
for role in analyzer profiler candidate-planner implementer reviewer; do
    check "registers Faiss $role agent" '[ -f "$TMPROOT/opencode/.opencode/agents/faiss-'"$role"'.md" ]'
done
check "OpenCode overlay includes Faiss route" 'grep -q "Faiss 路由优先级" "$OPENCODE_SKILLS/kpbot-code-optimizer/SKILL.md"'

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
