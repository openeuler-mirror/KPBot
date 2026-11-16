#!/usr/bin/env bash
set -euo pipefail
umask 077
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CASE_DIR/docker_security.sh"
BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
IMAGE="${1:-kpbot/prompt-capture-agent:1.0-openeuler24.03}"
SAVE="${2:?Provide a new output directory as the second argument}"
[ -f "$BIN" ] && [ -x "$BIN" ] || { echo 'Invalid opencode binary' >&2; exit 1; }
[ -f "$AUTH" ] || { echo 'Missing auth.json' >&2; exit 1; }
BIN="$(realpath "$BIN")"
AUTH="$(realpath "$AUTH")"
HOME_REAL="$(realpath "$HOME")"
case "$BIN" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|"$HOME_REAL"/.local/bin/*|"$HOME_REAL"/.local/share/opencode/*) ;;
    *) echo 'Binary path is outside the allowlist' >&2; exit 1 ;;
esac
case "$AUTH" in
    "$HOME_REAL"/.local/share/opencode/auth.json|"$HOME_REAL"/.config/opencode/auth.json|/etc/opencode/auth.json) ;;
    *) echo 'Auth path is outside the allowlist' >&2; exit 1 ;;
esac
case "$BIN$AUTH" in *','*|*$'\n'*|*$'\r'*) echo 'Invalid mount path' >&2; exit 1 ;; esac
MAGIC="$(head -c 4 "$BIN" | od -An -tx1 | tr -d ' \n')"
[ "$MAGIC" = 7f454c46 ] || { echo 'opencode must be a Linux ELF binary' >&2; exit 1; }
HASH="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$BIN")"
if [ -n "${OPENCODE_SHA256:-}" ] && [ "$HASH" != "$OPENCODE_SHA256" ]; then
    echo 'opencode SHA-256 mismatch' >&2; exit 1
fi
EXTRA=()
for arg in ${DOCKER_RUN_ARGS:-}; do
    case "$arg" in
        --network=bridge|--network=none) EXTRA+=("$arg") ;;
        --cpus=*|--memory=*|--pids-limit=*)
            if ! printf '%s\n' "$arg" | LC_ALL=C grep -Eq '^--(cpus=[0-9]+([.][0-9]+)?|memory=[1-9][0-9]*[kKmMgG]?|pids-limit=[1-9][0-9]*)$'; then
                echo 'Invalid resource limit' >&2; exit 1
            fi
            EXTRA+=("$arg") ;;
        --rm) ;; # Export results before removing the container.
        *) echo 'DOCKER_RUN_ARGS contains a forbidden option' >&2; exit 1 ;;
    esac
done
[ ! -e "$SAVE" ] || { echo 'Output directory already exists' >&2; exit 1; }
mkdir -m 700 -p "$SAVE"
SAVE="$(cd "$SAVE" && pwd)"
name=""
cleanup() {
    if [ -n "$name" ]; then docker rm -f "$name" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
failed=0
for mode in with without; do
    name="kpbot-prompt-case-$$-$RANDOM-$mode"
    docker create --name "$name" "${CASE_DOCKER_SECURITY_ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} \
        -e SKILL_MODE="$mode" -e MODEL="${MODEL:-opencode/glm-5.3-flash-free}" \
        --mount "type=bind,src=$BIN,dst=/usr/local/bin/opencode,readonly" \
        --mount "type=bind,src=$AUTH,dst=/run/secrets/opencode-auth.json,readonly" \
        "$IMAGE" >/dev/null
    docker start "$name" >/dev/null
    if ! python3 -c 'import subprocess,sys
try:
    r=subprocess.run(["docker","wait",sys.argv[1]],capture_output=True,text=True,timeout=660)
    sys.exit(0 if r.returncode == 0 and r.stdout.strip() == "0" else 1)
except subprocess.TimeoutExpired:
    sys.exit(1)' "$name"; then failed=1; fi
    mkdir -m 700 "$SAVE/$mode"
    if ! docker cp "$name:/workspace/results/." "$SAVE/$mode/"; then failed=1; fi
    docker rm -f "$name" >/dev/null
    name=""
    if [ -f "$SAVE/$mode/result.json" ]; then
        cat "$SAVE/$mode/result.json"
    else
        echo "Missing result for $mode" >&2
        failed=1
    fi
done
exit "$failed"
