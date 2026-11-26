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
WITH_PACKAGES=()

print_help() {
    cat <<'EOF'
devkit deploy — Download and install devkit RPMs

Usage:
  deploy_devkit.sh [options]

Options:
  --install-dir <path>   Custom install directory (default: /opt/devkit or ~/.local/share/devkit)
  --packages <name>      Install specific package(s), comma-separated
  --with <package>       Additionally install optional package(s), repeatable
                         (e.g. --with java-perf; required packages are never duplicated)
  --all                  Install all packages (not just required ones)
  --help                 Show this help message

Examples:
  deploy_devkit.sh                              Install required packages (devkit + devkit-tuner)
  deploy_devkit.sh --with java-perf             Install required packages + java-perf optional plugin
  deploy_devkit.sh --all                        Install all packages defined in binaries.yaml
  deploy_devkit.sh --install-dir /custom/path   Install to custom directory
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --packages)    IFS=',' read -ra EXTRA_PACKAGES <<< "$2"; shift 2 ;;
        --with)        [ -z "${2:-}" ] && { echo "ERROR: --with requires a package name" >&2; exit 1; }
                       WITH_PACKAGES+=("$2"); shift 2 ;;
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
    local pkg_name pkg_required pkg_component pkg_sha256
    local in_packages=false
    local match ep

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
                pkg_component=""
                ;;
            "    required:"*)
                pkg_required=$(echo "$line" | sed 's/.*: *//')
                ;;
            "    component:"*)
                pkg_component=$(echo "$line" | sed 's/.*: *//')
                ;;
            "    sha256:"*)
                pkg_sha256=$(echo "$line" | sed 's/.*: *"\(.*\)".*/\1/')
                if [ "$want_all" = true ] || [ "$pkg_required" = true ]; then
                    echo "$pkg_name|$pkg_sha256|$pkg_required"
                else
                    match=false
                    for ep in "${extra[@]:-}"; do
                        [ -z "$ep" ] && continue
                        if [ "$pkg_name" = "$ep" ] || [ "$pkg_component" = "$ep" ]; then
                            match=true
                            break
                        fi
                    done
                    [ "$match" = true ] && echo "$pkg_name|$pkg_sha256|$pkg_required"
                fi
                ;;
        esac
    done < "$CONFIG_FILE"
}

SELECTED_EXTRAS=()
for ep in "${EXTRA_PACKAGES[@]:-}"; do
    [ -z "$ep" ] && continue
    IFS=',' read -ra parts <<< "$ep"
    for p in "${parts[@]}"; do [ -n "$p" ] && SELECTED_EXTRAS+=("$p"); done
done
for wp in "${WITH_PACKAGES[@]:-}"; do
    [ -z "$wp" ] && continue
    IFS=',' read -ra parts <<< "$wp"
    for p in "${parts[@]}"; do [ -n "$p" ] && SELECTED_EXTRAS+=("$p"); done
done

PACKAGES_TO_INSTALL=$(parse_packages "$ALL_PACKAGES" "${SELECTED_EXTRAS[@]:-}")

if [ "${#SELECTED_EXTRAS[@]}" -gt 0 ]; then
    ALL_PKG_NAMES=$(awk '/^packages:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$CONFIG_FILE")
    ALL_COMPONENTS=$(awk '/^packages:/{f=1;next} f&&/^    component:/{print $2} f&&/^[^[:space:]]/{f=0}' "$CONFIG_FILE")
    for ep in "${SELECTED_EXTRAS[@]}"; do
        if ! echo "$ALL_PKG_NAMES" | grep -qx "$ep" && ! echo "$ALL_COMPONENTS" | grep -qx "$ep"; then
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
REQ_OK=0
REQ_FAIL=0
OPT_OK=0
OPT_FAIL=0

while IFS='|' read -r pkg_name pkg_sha256 pkg_required; do
    [ -z "$pkg_name" ] && continue
    TOTAL=$((TOTAL + 1))
    [ "$pkg_required" = "true" ] && group="required" || group="optional"

    rpm_file="${pkg_name}-${VERSION}-1.aarch64.rpm"
    url="${BASE_URL}/${rpm_file}"
    tmp_rpm="$TMP_DIR/${rpm_file}"

    echo -n "  [$group] $pkg_name: "

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
            if [ "$group" = "required" ]; then REQ_OK=$((REQ_OK + 1)); else OPT_OK=$((OPT_OK + 1)); fi
        else
            echo "FAILED (cpio extraction failed)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            if [ "$group" = "required" ]; then REQ_FAIL=$((REQ_FAIL + 1)); else OPT_FAIL=$((OPT_FAIL + 1)); fi
        fi
    else
        echo "FAILED (3 attempts, url=${url})"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        if [ "$group" = "required" ]; then REQ_FAIL=$((REQ_FAIL + 1)); else OPT_FAIL=$((OPT_FAIL + 1)); fi
    fi
done <<< "$PACKAGES_TO_INSTALL"

echo ""
if [ "$OPT_OK" -gt 0 ] || [ "$OPT_FAIL" -gt 0 ]; then
    echo "Summary: required ${REQ_OK}/$((REQ_OK + REQ_FAIL)) OK, optional ${OPT_OK}/$((OPT_OK + OPT_FAIL)) OK, ${FAIL_COUNT} failed"
else
    echo "Summary: ${OK_COUNT}/${TOTAL} OK, ${FAIL_COUNT} failed"
fi

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
