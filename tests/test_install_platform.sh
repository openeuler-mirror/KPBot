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

check_vector_install() {
    local config_root="$1"
    local skill_root="$config_root/skills"
    local role skill
    for skill in diskann-optimize diskann-source-analysis diskann-benchmark-baseline; do
        check "installs $skill" '[ -f "$skill_root/$skill/SKILL.md" ]'
    done
    check "installs DiskANN native performance case" '[ -f "$skill_root/diskann-benchmark-baseline/cases/rust-memory-search/run.py" ]'
    check "installs common vector workflow" '[ -f "$skill_root/vector-search-optimize/SKILL.md" ] && [ -f "$skill_root/vector-search-optimize/references/cp0-cp7.md" ]'
    check "routes vector tasks through the common workflow" 'grep -q "向量检索接管.*vector-search-optimize" "$skill_root/kpbot-code-optimizer/SKILL.md"'
    check "preserves the OpenZL route alongside vector search" 'grep -Fq openzl-compression-tuning "$skill_root/kpbot-code-optimizer/SKILL.md" && [ -f "$skill_root/openzl-compression-tuning/SKILL.md" ]'
    check "routes to installed Faiss and DiskANN workflows" 'grep -Fq faiss-optimize "$skill_root/vector-search-optimize/SKILL.md" && grep -Fq diskann-optimize "$skill_root/vector-search-optimize/SKILL.md" && [ -f "$skill_root/faiss-optimize/SKILL.md" ]'
    for role in analyzer profiler candidate-planner implementer reviewer; do
        check "registers DiskANN $role as subagent" 'grep -q "^mode: subagent$" "$config_root/agents/diskann-$role.md"'
    done
    check "keeps vector and DiskANN workflows out of the agent registry" '[ ! -e "$config_root/agents/vector-search-optimize.md" ] && [ ! -e "$config_root/agents/diskann-optimize.md" ]'
}

check_agentenv_package() {
    local skills="$1"
    local name source_skill
    local found=0
    check "installs AgentENV entry" '[ -f "$skills/kptools-agentenv-tuning/SKILL.md" ]'
    check "AgentENV entry name matches directory" \
        'grep -qx "name: kptools-agentenv-tuning" "$skills/kptools-agentenv-tuning/SKILL.md"'
    check "app-tuner handoff resolves" \
        '[ -r "$skills/kpbot-app-tuner/../kptools-agentenv-tuning/SKILL.md" ]'
    for source_skill in "$ROOT"/Plugins/app-tuner/skills/kptools-agentenv-tuning/skills/agentenv-*/SKILL.md; do
        [ -f "$source_skill" ] || continue
        name="$(basename "$(dirname "$source_skill")")"
        found=$((found + 1))
        check "discovers $name" '[ -f "$skills/$name/SKILL.md" ]'
        check "$name shared references resolve" \
            '[ -r "$skills/$name/references/validation.md" ]'
        check "$name shared collector works" \
            'bash "$skills/$name/scripts/collect_environment.sh" --help >/dev/null'
        check "$name functional validation guide resolves" \
            '[ -r "$skills/$name/references/functional-validation.md" ]'
        check "$name benchmark checker works" \
            'python3 "$skills/$name/scripts/check_benchmark_output.py" --help >/dev/null'
        check "$name case supervisor resolves" \
            '[ -r "$skills/$name/scripts/agent_case_supervisor.py" ]'
        if [ "$name" = "agentenv-cluster-tlbi" ]; then
            check "$name benchmark guide resolves" \
                '[ -r "$skills/$name/references/tlbi-benchmark.md" ]'
        fi
    done
    check "at least one AgentENV specialization is present" '[ "$found" -gt 0 ]'
    check "invalid PID rejected without creating output" \
        '! bash "$skills/kptools-agentenv-tuning/scripts/collect_environment.sh" --output "$TMPROOT/invalid-pid" --pid ../self >/dev/null 2>&1 && [ ! -e "$TMPROOT/invalid-pid" ]'
    check "existing output is not overwritten" \
        '! bash "$skills/kptools-agentenv-tuning/scripts/collect_environment.sh" --output "$TMPROOT" >/dev/null 2>&1'
}

echo "== claude mode =="
run_install claude
CLAUDE_SKILLS="$TMPROOT/claude/.claude/skills"
check "installs code-optimizer skills"        '[ -d "$CLAUDE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$CLAUDE_SKILLS/kpbot-app-tuner" ]'
check "installs prompt capture runtime"       '[ -f "$CLAUDE_SKILLS/kptools-prompt-capture/scripts/capture_runtime.py" ]'
check "prompt capture controller loads"       'python3 "$CLAUDE_SKILLS/kptools-prompt-capture/scripts/capturectl.py" --help >/dev/null'
check "prompt capture report assets load"     'python3 "$CLAUDE_SKILLS/kptools-prompt-capture/scripts/session_view.py" --help >/dev/null'
check "excludes OpenCode-only knowledge skill" '[ ! -e "$CLAUDE_SKILLS/kunpeng-knowledge" ]'
check "keeps claude-only drive skill"         '[ -d "$CLAUDE_SKILLS/drive-claude-optimize-pipeline" ]'
check "keeps claude-only batch skill"         '[ -d "$CLAUDE_SKILLS/batch-drive-optimize-pipeline" ]'
check "installs Faiss domain entry"           '[ -f "$CLAUDE_SKILLS/faiss-optimize/SKILL.md" ]'
check "includes Faiss workflow initializer"   '[ -f "$CLAUDE_SKILLS/faiss-optimize/workflows/scripts/init_run.py" ]'
check "registers Faiss reviewer agent"        '[ -f "$TMPROOT/claude/.claude/agents/faiss-reviewer.md" ]'
check_agentenv_package "$CLAUDE_SKILLS"
check_vector_install "$TMPROOT/claude/.claude"

echo "== opencode mode =="
run_install opencode
OPENCODE_SKILLS="$TMPROOT/opencode/.opencode/skills"
check "installs code-optimizer skills"        '[ -d "$OPENCODE_SKILLS/kpbot-code-optimizer" ]'
check "installs app-tuner skills"             '[ -d "$OPENCODE_SKILLS/kpbot-app-tuner" ]'
check "installs prompt capture entry"         '[ -f "$OPENCODE_SKILLS/kptools-prompt-capture/SKILL.md" ]'
check "prompt capture assets survive install" '[ -f "$OPENCODE_SKILLS/kptools-prompt-capture/assets/session.json" ]'
check "prompt capture analysis loads"         'python3 "$OPENCODE_SKILLS/kptools-prompt-capture/scripts/analyze.py" --help >/dev/null'
check "installs knowledge skill"              '[ -f "$OPENCODE_SKILLS/kunpeng-knowledge/SKILL.md" ]'
check "includes knowledge connection guide"    '[ -f "$OPENCODE_SKILLS/kunpeng-knowledge/references/connection.md" ]'
check "excludes claude-only drive skill"      '[ ! -e "$OPENCODE_SKILLS/drive-claude-optimize-pipeline" ]'
check "excludes claude-only batch skill"      '[ ! -e "$OPENCODE_SKILLS/batch-drive-optimize-pipeline" ]'
check "installs Faiss domain entry"           '[ -f "$OPENCODE_SKILLS/faiss-optimize/SKILL.md" ]'
check "preserves Faiss benchmark contract"    '[ -f "$OPENCODE_SKILLS/faiss-ivfpq-optimization/references/benchmark-contract.json" ]'
for role in analyzer profiler candidate-planner implementer reviewer; do
    check "registers Faiss $role agent" '[ -f "$TMPROOT/opencode/.opencode/agents/faiss-'"$role"'.md" ]'
done
check_agentenv_package "$OPENCODE_SKILLS"
check_vector_install "$TMPROOT/opencode/.opencode"

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
