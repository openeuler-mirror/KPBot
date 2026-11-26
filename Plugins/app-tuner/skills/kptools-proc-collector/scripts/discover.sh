#!/bin/bash
set -euo pipefail

print_help() {
    cat <<'EOF'
kptools-proc-collector discover — Check tool availability for each collection mode

Required: ps (always present on Linux)
Optional: pidstat, perf, taskset, numastat, readelf, ldd

Output:
  <tool>: AVAILABLE (<path>) | MISSING (install: <pkg>)
exit 0 always (missing optional tools only degrade specific modes)
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help) print_help ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

check_tool() {
    local tool="$1" pkg="$2"
    if command -v "$tool" &>/dev/null; then
        echo "$tool: AVAILABLE ($(command -v "$tool"))"
    else
        echo "$tool: MISSING (install: $pkg)"
    fi
}

if command -v ps &>/dev/null; then
    echo "ps: AVAILABLE ($(command -v ps))"
else
    echo "ps: MISSING (procps)"
fi
check_tool pidstat "sysstat"
check_tool perf "perf / linux-tools"
check_tool taskset "util-linux"
check_tool numastat "numactl"
check_tool readelf "binutils"
check_tool ldd "binutils"
exit 0
