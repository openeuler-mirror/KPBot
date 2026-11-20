#!/usr/bin/env bash
# Check whether this host can build/run the RocksDB SVE2 bloom-filter patch.
#
# Read-only: compiles tiny probes in a temp dir; never patches or builds RocksDB.
# Run this on the TARGET deployment machine; build and target must share the same
# CPU capability (SVE2 + 256-bit), because the patch applies global SVE2 flags.
#
# Usage:
#   ./check_arch_sve2.sh [--rocksdb PATH] [--patch PATH] [--json]
#
# Exit codes:
#   0  all prerequisites pass (or the patch is already applied)
#   1  hard error (missing RocksDB/patch, patch does not apply)
#   2  prerequisites not met (skip the optimization)
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROCKSDB="${ROCKSDB_PATH:-$PWD}"
PATCH="$SKILL_DIR/references/patches/bloomfilter_sve2.diff"
JSON=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rocksdb) ROCKSDB="${2:?}"; shift 2 ;;
        --patch) PATCH="${2:?}"; shift 2 ;;
        --json) JSON=1; shift ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

LINES=()
RESULT="PASS"

record() { LINES+=("$1|$2|$3"); }
finish() {
    local hard_fail=0 skip=0
    for line in "${LINES[@]}"; do
        IFS='|' read -r _ status _ <<< "$line"
        case "$status" in
            FAIL) hard_fail=1 ;;
            SKIP) skip=1 ;;
        esac
    done
    if [[ "$hard_fail" -eq 1 ]]; then
        RESULT="FAIL"
    elif [[ "$skip" -eq 1 ]]; then
        RESULT="SKIP"
    else
        RESULT="PASS"
    fi

    if [[ "$JSON" -eq 1 ]]; then
        python3 - "$RESULT" "${LINES[@]}" <<'PY'
import json
import sys

result = sys.argv[1]
checks = []
for line in sys.argv[2:]:
    name, status, detail = line.split("|", 2)
    checks.append({"name": name, "status": status, "detail": detail})
print(json.dumps({"result": result, "checks": checks}, ensure_ascii=False, indent=2))
PY
    else
        printf '%-26s %-6s %s\n' "CHECK" "STATUS" "DETAIL"
        for line in "${LINES[@]}"; do
            IFS='|' read -r name status detail <<< "$line"
            printf '%-26s %-6s %s\n' "$name" "$status" "$detail"
        done
        echo
        echo "RESULT: $RESULT"
    fi

    case "$RESULT" in
        PASS) exit 0 ;;
        SKIP) exit 2 ;;
        *) exit 1 ;;
    esac
}

# 1. architecture
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) record "arch" PASS "$ARCH" ;;
    *)
        record "arch" SKIP "not aarch64: $ARCH"
        finish
        ;;
esac

# 2. CPU features
if command -v grep >/dev/null 2>&1 && [ -r /proc/cpuinfo ] &&
        grep -m1 -qw sve2 /proc/cpuinfo; then
    record "cpu_sve2" PASS "sve2 present in /proc/cpuinfo"
else
    record "cpu_sve2" SKIP "sve2 not found in /proc/cpuinfo"
fi
if command -v grep >/dev/null 2>&1 && [ -r /proc/cpuinfo ] &&
        grep -m1 -qw sve /proc/cpuinfo; then
    record "cpu_sve" PASS "sve present in /proc/cpuinfo"
else
    record "cpu_sve" SKIP "sve not found in /proc/cpuinfo"
fi

# 3. SVE vector length: the patch only enables its runtime path with 256-bit SVE.
if [ -r /proc/sys/abi/sve_default_vector_length ]; then
    VL_BYTES="$(cat /proc/sys/abi/sve_default_vector_length 2>/dev/null || true)"
    if [ "${VL_BYTES:-}" = "32" ]; then
        record "sve_vector_length" PASS "default_vector_length=32 bytes (256-bit)"
    else
        record "sve_vector_length" SKIP "default_vector_length=${VL_BYTES:-unknown} bytes; need 32 (256-bit)"
    fi
else
    record "sve_vector_length" SKIP "no /proc/sys/abi/sve_default_vector_length; use the compiler probe"
fi

# 4. compiler + runtime probe (svcntw()==8 and HWCAP2_SVE2)
CXX="${CXX:-g++}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! command -v "$CXX" >/dev/null 2>&1; then
    record "compiler" SKIP "$CXX not found"
else
    cat > "$TMP/sve2_probe.cc" <<'EOF'
#include <arm_sve.h>
#include <sys/auxv.h>
#include <asm/hwcap.h>
#include <cstdio>

int main() {
    if (svcntw() != 8) {
        std::printf("svcntw=%lu\n", static_cast<unsigned long>(svcntw()));
        return 2;
    }
    if ((getauxval(AT_HWCAP2) & HWCAP2_SVE2) == 0) {
        return 3;
    }
    return 0;
}
EOF
    FLAGS="-march=armv8-a+crc+crypto+sve2 -msve-vector-bits=256"
    if ! "$CXX" $FLAGS "$TMP/sve2_probe.cc" -o "$TMP/sve2_probe" >"$TMP/build.log" 2>&1; then
        record "compiler" SKIP "$CXX does not accept $FLAGS"
    elif "$TMP/sve2_probe" >"$TMP/run.log" 2>&1; then
        record "compiler" PASS "$CXX builds; svcntw()==8 and HWCAP2_SVE2 set"
    else
        rc=$?
        detail="$(cat "$TMP/run.log" 2>/dev/null || true)"
        case "$rc" in
            2) record "runtime" SKIP "SVE vector length is not 256-bit (${detail:-svcntw!=8})" ;;
            3) record "runtime" SKIP "HWCAP2_SVE2 not reported by getauxval" ;;
            *) record "runtime" SKIP "probe failed (rc=$rc); SVE2/256-bit unavailable at runtime" ;;
        esac
    fi
fi

# 5. RocksDB version
if [ -f "$ROCKSDB/include/rocksdb/version.h" ]; then
    MAJOR="$(sed -n 's/^#define ROCKSDB_MAJOR //p' "$ROCKSDB/include/rocksdb/version.h" | head -1)"
    MINOR="$(sed -n 's/^#define ROCKSDB_MINOR //p' "$ROCKSDB/include/rocksdb/version.h" | head -1)"
    PATCHV="$(sed -n 's/^#define ROCKSDB_PATCH //p' "$ROCKSDB/include/rocksdb/version.h" | head -1)"
    record "rocksdb_version" PASS "${MAJOR}.${MINOR}.${PATCHV} (patch targets 6.26.x)"
else
    record "rocksdb_version" FAIL "missing $ROCKSDB/include/rocksdb/version.h"
fi

# 6. patch applicability (read-only)
if [ ! -f "$PATCH" ]; then
    record "patch" FAIL "missing $(basename "$PATCH")"
elif [ -d "$ROCKSDB/.git" ]; then
    if git -C "$ROCKSDB" apply --check "$PATCH" >/dev/null 2>&1; then
        record "patch" PASS "git apply --check OK"
    elif git -C "$ROCKSDB" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
        record "patch" PASS "already applied"
    else
        record "patch" FAIL "does not apply to $ROCKSDB (version/context mismatch)"
    fi
else
    if command -v patch >/dev/null 2>&1 && (cd "$ROCKSDB" && patch -p1 --dry-run < "$PATCH" >/dev/null 2>&1); then
        record "patch" PASS "patch -p1 --dry-run OK"
    else
        record "patch" FAIL "does not apply to $ROCKSDB (or patch tool unavailable)"
    fi
fi

finish
