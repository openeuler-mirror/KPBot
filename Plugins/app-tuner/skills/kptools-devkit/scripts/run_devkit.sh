#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEVKIT_BIN="${DEVKIT_BIN:-}"
if [ -z "$DEVKIT_BIN" ]; then
    # 受管安装优先（版本/sha256 有契约），PATH 中杂散版本仅作 fallback
    # （实测环境 PATH 中的 26.0 旧版曾被优先命中，导致版本断言失败）
    MANAGED_SUBPATH=$(grep '^[[:space:]]*binary_subpath:' "$SCRIPT_DIR/binaries.yaml" 2>/dev/null | head -1 | sed 's/.*: *//')
    if [ -n "$MANAGED_SUBPATH" ]; then
        for managed in "/opt/devkit/${MANAGED_SUBPATH}" "${HOME:-}/.local/share/devkit/${MANAGED_SUBPATH}"; do
            # -f 排除目录：MANAGED_SUBPATH 异常时 [ -x dir ] 对目录也为真，exec 目录报错难懂
            if [ -f "$managed" ] && [ -x "$managed" ]; then
                DEVKIT_BIN="$managed"
                break
            fi
        done
    fi
    if [ -z "$DEVKIT_BIN" ]; then
        if command -v devkit &>/dev/null; then
            DEVKIT_BIN=$(command -v devkit)
        else
            DEVKIT_BIN=$("$SCRIPT_DIR/discover_devkit.sh" --path 2>/dev/null || true)
            if [ -z "$DEVKIT_BIN" ]; then
                echo "ERROR: devkit not installed. Run scripts/deploy_devkit.sh to install." >&2
                exit 1
            fi
        fi
    fi
fi

CONFIG_FILE="$SCRIPT_DIR/binaries.yaml"
LIB_SUBPATH=$(grep '^[[:space:]]*lib_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
BINARY_SUBPATH=$(grep '^[[:space:]]*binary_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
DEVKIT_ROOT="${DEVKIT_BIN%/${BINARY_SUBPATH}}"
export LD_LIBRARY_PATH="${DEVKIT_ROOT}/${LIB_SUBPATH}:${LD_LIBRARY_PATH:-}"

validate_java_perf_output() {
    local out_val="$1"
    case "$out_val" in
        /*) ;;
        *)
            echo "ERROR: java-perf -o must be an absolute path (got '$out_val'); relative paths cause ThorCliApp exceptions" >&2
            exit 2 ;;
    esac
    case "$out_val" in
        *.html) ;;
        *)
            echo "ERROR: java-perf -o must end with .html (got '$out_val'); a missing suffix fails silently (no output file, no error)" >&2
            exit 2 ;;
    esac
    local out_dir
    out_dir=$(dirname "$out_val")
    if [ ! -d "$out_dir" ] || [ ! -w "$out_dir" ]; then
        echo "ERROR: java-perf -o target directory does not exist or is not writable: $out_dir (default install-dir output is unwritable for non-root; create the dir first, e.g. mkdir -p ./devkit-evidence/<ts>)" >&2
        exit 2
    fi
}

if [ "${1:-}" = "java-perf" ]; then
    expect_output=false
    for arg in "$@"; do
        if [ "$expect_output" = true ]; then
            validate_java_perf_output "$arg"
            expect_output=false
            continue
        fi
        case "$arg" in
            -o|--output) expect_output=true ;;
            -o=*|--output=*) validate_java_perf_output "${arg#*=}" ;;
        esac
    done
    if [ "$expect_output" = true ]; then
        echo "ERROR: java-perf -o/--output requires a value" >&2
        exit 2
    fi
fi

# java-perf 的 cli-backend 以相对路径写 java-perf/log 等文件，要求 cwd 为 devkit home
# （实测：cwd 不对时 attach 失败，NoSuchFileException: java-perf/log/...）
if [ "${1:-}" = "java-perf" ]; then
    cd "$(dirname "$DEVKIT_BIN")"
fi

exec "$DEVKIT_BIN" "$@"
