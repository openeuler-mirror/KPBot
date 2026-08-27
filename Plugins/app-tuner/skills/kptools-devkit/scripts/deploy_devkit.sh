#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/binaries.yaml"

if [ "$(id -u)" -eq 0 ]; then
    DEFAULT_INSTALL_DIR="/opt/devkit"
else
    DEFAULT_INSTALL_DIR="$HOME/.local/share/devkit"
fi

INSTALL_DIR="$DEFAULT_INSTALL_DIR"
ALL_PACKAGES=false
EXTRA_PACKAGES=()

print_help() {
    cat <<'EOF'
devkit deploy — Download and install devkit RPMs

Usage:
  deploy_devkit.sh [options]

Options:
  --install-dir <path>   Custom install directory (default: /opt/devkit or ~/.local/share/devkit)
  --packages <name>      Install specific package(s), comma-separated
  --all                  Install all packages (not just required ones)
  --help                 Show this help message

Examples:
  deploy_devkit.sh                              Install required packages (devkit + devkit-tuner)
  deploy_devkit.sh --all                        Install all packages defined in binaries.yaml
  deploy_devkit.sh --install-dir /custom/path   Install to custom directory
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --packages)    IFS=',' read -ra EXTRA_PACKAGES <<< "$2"; shift 2 ;;
        --all)         ALL_PACKAGES=true; shift ;;
        --help)        print_help; exit 0 ;;
        *)             echo "Unknown option: $1" >&2; print_help; exit 1 ;;
    esac
done

if ! command -v rpm2cpio &>/dev/null; then
    echo "ERROR: rpm2cpio not found. Install: yum install rpm2cpio" >&2
    exit 1
fi
if ! command -v cpio &>/dev/null; then
    echo "ERROR: cpio not found. Install: yum install cpio" >&2
    exit 1
fi

VERSION=$(grep '^  version:' "$CONFIG_FILE" | head -1 | sed 's/.*: *"\(.*\)".*/\1/')
DISPLAY_VERSION=$(grep '^  display_version:' "$CONFIG_FILE" | head -1 | sed 's/.*: *"\(.*\)".*/\1/')
BASE_URL=$(grep '^  base_url:' "$CONFIG_FILE" | head -1 | sed 's/.*: *"\(.*\)".*/\1/' | sed "s/{DISPLAY_VERSION}/$DISPLAY_VERSION/")

parse_packages() {
    local want_all="$1"
    shift
    local extra=()
    while [ $# -gt 0 ]; do extra+=("$1"); shift; done
    local pkg_name pkg_required pkg_sha256
    local in_packages=false

    while IFS= read -r line; do
        case "$line" in
            "packages:"*)
                in_packages=true
                ;;
            [^[:space:]]*)
                in_packages=false
                ;;
        esac
        [ "$in_packages" = false ] && continue
        case "$line" in
            "  - name:"*)
                pkg_name=$(echo "$line" | sed 's/.*: *//')
                ;;
            "    required:"*)
                pkg_required=$(echo "$line" | sed 's/.*: *//')
                ;;
            "    sha256:"*)
                pkg_sha256=$(echo "$line" | sed 's/.*: *"\(.*\)".*/\1/')
                if [ "$want_all" = true ] || [ "$pkg_required" = true ]; then
                    echo "$pkg_name|$pkg_sha256"
                elif [ "$want_all" = false ]; then
                    for ep in "${extra[@]:-}"; do
                        [ -z "$ep" ] && continue
                        if [ "$pkg_name" = "$ep" ]; then
                            echo "$pkg_name|$pkg_sha256"
                        fi
                    done
                fi
                ;;
        esac
    done < "$CONFIG_FILE"
}

PACKAGES_TO_INSTALL=$(parse_packages "$ALL_PACKAGES" "${EXTRA_PACKAGES[@]:-}")

if [ "${#EXTRA_PACKAGES[@]}" -gt 0 ] && [ -n "${EXTRA_PACKAGES[0]}" ]; then
    ALL_PKG_NAMES=$(awk '/^packages:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$CONFIG_FILE")
    for ep in "${EXTRA_PACKAGES[@]}"; do
        [ -z "$ep" ] && continue
        if ! echo "$ALL_PKG_NAMES" | grep -qx "$ep"; then
            echo "WARNING: package '$ep' not found in binaries.yaml, skipping" >&2
        fi
    done
fi

if [ -z "$PACKAGES_TO_INSTALL" ]; then
    echo "ERROR: No packages to install" >&2
    exit 1
fi

mkdir -p "$INSTALL_DIR"
TMP_DIR=$(mktemp -d /tmp/devkit-install.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

OK_COUNT=0
FAIL_COUNT=0
TOTAL=0

while IFS='|' read -r pkg_name pkg_sha256; do
    [ -z "$pkg_name" ] && continue
    TOTAL=$((TOTAL + 1))

    rpm_file="${pkg_name}-${VERSION}-1.aarch64.rpm"
    url="${BASE_URL}/${rpm_file}"
    tmp_rpm="$TMP_DIR/${rpm_file}"

    echo -n "  $pkg_name: "

    download_ok=false
    for attempt in 1 2 3; do
        if curl -fSL -o "$tmp_rpm" "$url" 2>/dev/null; then
            actual_sha256=$(sha256sum "$tmp_rpm" | awk '{print $1}')
            if [ "$actual_sha256" = "$pkg_sha256" ]; then
                download_ok=true
                break
            else
                echo -n "checksum mismatch (attempt $attempt), "
                rm -f "$tmp_rpm"
            fi
        else
            echo -n "download failed (attempt $attempt), "
        fi
        if [ "$attempt" -lt 3 ]; then
            sleep $((2 ** attempt))
        fi
    done

    if [ "$download_ok" = true ]; then
        if (cd "$INSTALL_DIR" && rpm2cpio "$tmp_rpm" | cpio -idm --quiet); then
            echo "OK (${VERSION}, sha256 verified, ${INSTALL_DIR})"
            OK_COUNT=$((OK_COUNT + 1))
        else
            echo "FAILED (cpio extraction failed)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "FAILED (3 attempts, url=${url})"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done <<< "$PACKAGES_TO_INSTALL"

echo ""
echo "Summary: ${OK_COUNT}/${TOTAL} OK, ${FAIL_COUNT} failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
