#!/usr/bin/env bash
set -euo pipefail
# Prebuilt image and runtime-mounted model credentials; no host installation.
: "${OPENCODE_BIN:?Set the absolute path of a Linux opencode binary}"
: "${OPENCODE_CONFIG_DIR:?Set the existing opencode config/auth directory}"
IMAGE="${1:-kpbot/prompt-capture-agent:1.0-openeuler24.03}"
SAVE="${2:?Provide a new output directory as the second argument}"
if [ -e "$SAVE" ]; then printf '%s\n' 'Output directory already exists' >&2; exit 2; fi
mkdir -p "$SAVE"
SAVE="$(cd "$SAVE" && pwd)"
for mode in with without; do
    mkdir -p "$SAVE/$mode"
    docker run --rm -e SKILL_MODE="$mode" -e MODEL="${MODEL:-opencode/glm-5.3-flash-free}" \
        -v "$OPENCODE_BIN:/usr/local/bin/opencode:ro" \
        -v "$OPENCODE_CONFIG_DIR:/root/.config/opencode:ro" \
        -v "$SAVE/$mode:/workspace/results" "$IMAGE" || true
    if [ -f "$SAVE/$mode/result.json" ]; then
        printf '\n%s\n' "$mode"
        cat "$SAVE/$mode/result.json"
    else
        printf 'Missing result for %s\n' "$mode" >&2
        exit 1
    fi
done
