#!/usr/bin/env bash
# simdjson 功能验证：默认依次跑 dom / ondemand / per_implementation，任一失败退出非零。
# 用法: func_test.sh [-b build_dir] [-i IMPL] [-L label] [-R regex] [--no-force-env]
set -euo pipefail

BUILD_DIR=""
IMPL="arm64"
LABELS=()
REGEX=""
FORCE_ENV=1

usage() {
  cat <<EOF
用法: $0 [选项]

  -b DIR     构建目录 (默认: <repo>/build-Release)
  -i IMPL    强制 SIMD 实现 (默认: arm64)
  -L LABEL   只跑指定标签 (可多次; 默认: dom ondemand per_implementation)
  -R REGEX   ctest -R 名称过滤
  --no-force-env  不设置 SIMDJSON_FORCE_IMPLEMENTATION
  -h         帮助
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -b) BUILD_DIR="$2"; shift 2;;
    -i) IMPL="$2"; shift 2;;
    -L) LABELS+=("$2"); shift 2;;
    -R) REGEX="$2"; shift 2;;
    --no-force-env) FORCE_ENV=0; shift;;
    -h) usage;;
    *) echo "未知参数: $1" >&2; usage;;
  esac
done

REPO_ROOT="${SIMDJSON_ROOT:-$(pwd)}"
REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)"
BUILD_DIR="${BUILD_DIR:-${REPO_ROOT}/build-Release}"
if [[ ! -d "${BUILD_DIR}" ]]; then
  echo "错误: 构建目录不存在: ${BUILD_DIR}" >&2
  exit 1
fi

if [[ ${#LABELS[@]} -eq 0 ]]; then
  LABELS=(dom ondemand per_implementation)
fi

FORCE_PREFIX=()
if [[ $FORCE_ENV -eq 1 ]]; then
  FORCE_PREFIX=(env "SIMDJSON_FORCE_IMPLEMENTATION=${IMPL}")
fi

FAILED=0
for label in "${LABELS[@]}"; do
  echo ""
  echo "=== ctest -L ${label} (impl=${IMPL}) ==="
  if [[ -n "$REGEX" ]]; then
    "${FORCE_PREFIX[@]}" ctest --test-dir "${BUILD_DIR}" -L "${label}" -R "${REGEX}"
  else
    "${FORCE_PREFIX[@]}" ctest --test-dir "${BUILD_DIR}" -L "${label}"
  fi || { echo "!!! 失败: label=${label}" >&2; FAILED=1; }
done

echo ""
if [[ $FAILED -eq 0 ]]; then
  echo "=== 功能验证全部通过 ==="
else
  echo "=== 功能验证存在失败，不得进入性能 A/B ===" >&2
  exit 1
fi
