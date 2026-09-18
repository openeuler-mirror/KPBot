#!/bin/bash
set -euo pipefail

# network-collector deploy — Check/install tools required by collect.sh modes
#   required: ip+ss+nsstat(iproute2) / sar(sysstat) / ethtool
#   optional (fallbacks): netstat(net-tools)

print_help() {
    cat <<'EOF'
network-collector deploy — Check/install tools required by collect.sh

Usage:
  deploy.sh [options]

Options:
  --help    Show this help

Behavior:
  - all tools present: report and exit 0
  - root: try yum install -y <pkgs>, fallback apt-get install -y <pkgs>,
          re-verify; still missing -> print manual hints and exit 1
  - non-root: print the install commands and exit 1 (no install attempt)

Tool -> package mapping:
  ip/ss/nstat -> iproute2   (--iface/--sock/--retrans)
  sar         -> sysstat    (--bandwidth)
  ethtool     -> ethtool    (--nic)
  netstat     -> net-tools  (--drop fallback view, optional)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help) print_help; exit 0 ;;
        *) echo "Unknown option: $1" >&2; print_help >&2; exit 1 ;;
    esac
done

check() {
    command -v "$1" &>/dev/null
}

declare -A HINT=(
    [ip]="yum install iproute2 or apt install iproute2"
    [ss]="yum install iproute2 or apt install iproute2"
    [nstat]="yum install iproute2 or apt install iproute2"
    [sar]="yum install sysstat or apt install sysstat"
    [ethtool]="yum install ethtool or apt install ethtool"
    [netstat]="yum install net-tools or apt install net-tools (optional, --drop fallback view)"
)

declare -A PKG=(
    [ip]="iproute2"
    [ss]="iproute2"
    [nstat]="iproute2"
    [sar]="sysstat"
    [ethtool]="ethtool"
    [netstat]="net-tools"
)

TOOLS=(ip ss nstat sar ethtool netstat)

missing=()
for t in "${TOOLS[@]}"; do
    if ! check "$t"; then
        missing+=("$t")
    fi
done

if [ ${#missing[@]} -eq 0 ]; then
    echo "network-collector tools: all installed"
    exit 0
fi

printf 'network-collector tools: MISSING: %s\n' "${missing[*]}"

pkgs=""
seen=""
for t in "${missing[@]}"; do
    p="${PKG[$t]}"
    case ",$seen," in
        *",$p,"*) ;;
        *) pkgs="$pkgs $p"; seen="$seen,$p" ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    if command -v yum &>/dev/null; then
        # shellcheck disable=SC2086
        yum install -y $pkgs || true
    fi
    if command -v apt-get &>/dev/null; then
        # shellcheck disable=SC2086
        apt-get install -y $pkgs || true
    fi

    still_missing=()
    for t in "${missing[@]}"; do
        check "$t" || still_missing+=("$t")
    done

    if [ ${#still_missing[@]} -eq 0 ]; then
        echo "network-collector tools: installed via package manager, all present now"
        exit 0
    fi

    echo "ERROR: package-manager install finished but some tools are still missing:" >&2
    for t in "${still_missing[@]}"; do
        echo "  $t: ${HINT[$t]}" >&2
    done
    exit 1
fi

echo "NOT_INSTALLED and running as non-root. Install manually with:" >&2
echo "  yum install $pkgs    # RHEL/openEuler" >&2
echo "  apt-get install $pkgs    # Ubuntu/Debian" >&2
exit 1
