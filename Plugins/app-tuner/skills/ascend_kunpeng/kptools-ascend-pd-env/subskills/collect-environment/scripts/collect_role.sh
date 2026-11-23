#!/bin/bash
# Collect one role-tagged snapshot through a verified local Docker endpoint.
set -euo pipefail

if [ "$#" -lt 5 ] || [ "$#" -gt 6 ]; then
    printf 'Usage: collect_role.sh SOCKET DOCKER_NODE CONTAINER P|D OUT [PYTHON]\n' >&2
    exit 1
fi
socket=$1 node=$2 container=$3 role=$4 out=$5
case "$role" in P|D) ;; *) printf 'Role must be P or D.\n' >&2; exit 1 ;; esac
if [[ "$socket" != /* ]] || [ ! -S "$socket" ]; then
    printf 'Expected an explicit local Docker socket: %s\n' "$socket" >&2
    exit 2
fi

unset DOCKER_CONTEXT
export DOCKER_HOST="unix://$socket"
actual_node=$(docker info --format '{{.Name}}')
if [ -z "$node" ] || [ "$actual_node" != "$node" ]; then
    printf 'Docker node mismatch: expected %s, got %s\n' "$node" "$actual_node" >&2
    exit 2
fi

scripts="$(cd "$(dirname "$0")/../../../scripts" && pwd)"
bash "$scripts/discover.sh"
record=$(docker container inspect --format '{{.Id}} {{.Name}}' -- "$container")
read -r id name <<< "$record"
if [[ ! "$id" =~ ^[0-9a-f]{64}$ ]] || [ -z "$name" ]; then
    printf 'Could not verify a unique container ID for %s\n' "$container" >&2
    exit 1
fi

# collect.sh also accepts paths. Resolve the output before changing directory.
case "$out" in /*) ;; '') exit 1 ;; *) out="$PWD/$out" ;; esac
work=$(mktemp -d)
trap 'rmdir "$work"' EXIT
cd "$work"
printf 'Docker: %s (%s), container: %s (%s)\n' "$actual_node" "$DOCKER_HOST" "${name#/}" "$id"
args=("$id" --role "$role" --out "$out")
if [ "$#" -eq 6 ]; then
    args+=(--python "$6")
fi
bash "$scripts/collect.sh" "${args[@]}"
