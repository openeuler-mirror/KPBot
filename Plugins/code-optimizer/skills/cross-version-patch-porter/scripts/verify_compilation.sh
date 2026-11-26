#!/usr/bin/env bash
# verify_compilation.sh — Run build and capture errors
# Usage: verify_compilation.sh <target_dir> [build_command]
# If no build_command is provided, looks for local_build.sh or uses default make.

set -euo pipefail

TARGET_DIR="$1"
BUILD_CMD="${2:-}"

cd "$TARGET_DIR"

# Determine build command
if [ -n "$BUILD_CMD" ]; then
    echo "Using provided build command: $BUILD_CMD"
elif [ -f "local_build.sh" ]; then
    echo "Using local_build.sh"
    BUILD_CMD="bash local_build.sh"
else
    echo "Using default: make -j\$(nproc)"
    BUILD_CMD="make -j\$(nproc)"
fi

# Run build and capture output
BUILD_OUTPUT=$(mktemp)
BUILD_ERR=$(mktemp)

echo "Building in: $(pwd)"
echo "Command: $BUILD_CMD"
echo "---"

# Run the build
set +e
eval "$BUILD_CMD" > "$BUILD_OUTPUT" 2> "$BUILD_ERR"
BUILD_EXIT=$?
set -e

# Analyze results
if [ $BUILD_EXIT -eq 0 ]; then
    echo "RESULT: PASS"
    echo "Build completed successfully."
    rm -f "$BUILD_OUTPUT" "$BUILD_ERR"
    exit 0
fi

echo "RESULT: FAIL"
echo "Exit code: $BUILD_EXIT"
echo ""
echo "=== Error Summary ==="

# Extract meaningful error lines
grep -E '(error:|fatal error:|Error:|FAILED|undefined reference|cannot find|No such file)' "$BUILD_ERR" 2>/dev/null | head -50 || true
grep -E '(error:|fatal error:|Error:|FAILED|undefined reference|cannot find|No such file)' "$BUILD_OUTPUT" 2>/dev/null | head -50 || true

echo ""
echo "=== First 20 error lines ==="
grep -n 'error' "$BUILD_ERR" 2>/dev/null | head -20 || true

# Save full output for debugging
FULL_LOG="${TARGET_DIR}/build_error.log"
cat "$BUILD_OUTPUT" "$BUILD_ERR" > "$FULL_LOG" 2>/dev/null || true
echo ""
echo "Full build log saved to: $FULL_LOG"

rm -f "$BUILD_OUTPUT" "$BUILD_ERR"
exit $BUILD_EXIT
