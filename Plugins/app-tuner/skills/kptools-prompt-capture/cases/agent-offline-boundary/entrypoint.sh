#!/usr/bin/env bash
set -uo pipefail
cd /workspace
mkdir -p results
export SKILL_MODE="${SKILL_MODE:-with}"
case "$SKILL_MODE" in with|without) ;; *) exit 2 ;; esac
python3 seed.py || exit 1
if [ "$SKILL_MODE" = with ]; then
    mkdir -p .opencode/skills
    cp -R /staging/skill .opencode/skills/kptools-prompt-capture
fi
export AGENT_EXIT_CODE=0
timeout --kill-after=10 600 opencode run --format json -m "${MODEL:-opencode/glm-5.3-flash-free}" \
    "$(cat "prompts/$SKILL_MODE-skill.md")" > results/trace.jsonl 2> results/opencode.log || export AGENT_EXIT_CODE=$?
bash verify.sh
