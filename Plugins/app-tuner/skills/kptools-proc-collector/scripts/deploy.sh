#!/bin/bash
set -euo pipefail

# proc-collector deploy — Check/install tools required by collect.sh modes
#   required: pidstat(sysstat) / perf / taskset(util-linux)
#   optional (modes degrade when missing): numastat(numactl) / readelf+ldd(binutils)

print_help() {
    cat <<'EOF'
proc-collector deploy — Check/install tools required by collect.sh

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
  pidstat   -> sysstat      (--cpu/--ctx)
  perf      -> perf / linux-tools-generic (--migration; kernel-specific
               wrapper may still need a matching linux-tools-<uname -r>)
  taskset   -> util-linux   (--affinity)
  numastat  -> numactl      (--numa, optional)
  readelf   -> binutils     (--binary, optional)
  ldd       -> binutils     (--binary, optional)
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

# tool -> install hint (used when package-manager install does not fix it)
declare -A HINT=(
    [pidstat]="yum install sysstat or apt install sysstat"
    [perf]="yum install perf or apt install linux-tools-generic (plus linux-tools-$(uname -r) for kernel-specific builds)"
    [taskset]="yum install util-linux or apt install util-linux"
    [numastat]="yum install numactl or apt install numactl"
    [readelf]="yum install binutils or apt install binutils"
    [ldd]="yum install glibc or apt install libc6-dev"
)

declare -A PKG=(
    [pidstat]="sysstat"
    [perf]="linux-tools-generic"
    [taskset]="util-linux"
    [numastat]="numactl"
    [readelf]="binutils"
    [ldd]="binutils"
)

TOOLS=(pidstat perf taskset numastat readelf ldd)

missing=()
for t in "${TOOLS[@]}"; do
    if ! check "$t"; then
        missing+=("$t")
    fi
done

if [ ${#missing[@]} -eq 0 ]; then
    echo "proc-collector tools: all installed"
    exit 0
fi

printf 'proc-collector tools: MISSING: %s\n' "${missing[*]}"

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
        echo "proc-collector tools: installed via package manager, all present now"
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
