#!/bin/bash
set -euo pipefail

print_help() {
    cat <<'EOF'
kptools-network-collector discover — Check tool availability for each collection mode

Output:
  <tool>: AVAILABLE (<path>) | MISSING (install: <pkg>)
  ip/ss:      iproute2 (required for most modes)
  sar:        sysstat (bandwidth)
  netstat:    net-tools (drop/retrans; nstat is the fallback)
  ethtool:    ethtool (nic mode)
exit 0 always (missing tools only degrade specific modes)
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

check_tool ip "iproute2"
check_tool ss "iproute2"
check_tool sar "sysstat"
check_tool netstat "net-tools"
check_tool nstat "iproute2 (retrans fallback)"
check_tool ethtool "ethtool"
exit 0
