#!/usr/bin/env bash
# simdjson 开发者模式构建脚本。
# 用法: build.sh [-t Release|RelWithDebInfo|Debug] [-b build_dir] [-r repo] [--clean] [--target TARGET]
set -euo pipefail

BUILD_TYPE="Release"
BUILD_DIR=""
REPO_ROOT="${SIMDJSON_ROOT:-}"
CLEAN=0
TARGET=""

usage() {
  cat <<EOF
用法: $0 [选项]

  -t TYPE   构建类型: Release | RelWithDebInfo | Debug (默认: Release)
  -b DIR    构建目录 (默认: <repo>/build-<TYPE>)
  -r DIR    simdjson 源码根目录 (默认: \$SIMDJSON_ROOT 或当前目录)
  --target TARGET  只构建指定目标 (默认全部; 常用: parse / perfdiff / basictests)
  --clean   清理后重建
  -h        帮助
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t) BUILD_TYPE="$2"; shift 2;;
    -b) BUILD_DIR="$2"; shift 2;;
    -r) REPO_ROOT="$2"; shift 2;;
    --target) TARGET="$2"; shift 2;;
    --clean) CLEAN=1; shift;;
    -h) usage;;
    *) echo "未知参数: $1" >&2; usage;;
  esac
done

if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(pwd)"
fi
if [[ ! -f "${REPO_ROOT}/CMakeLists.txt" ]]; then
  echo "错误: ${REPO_ROOT} 不是 simdjson 源码根目录 (无 CMakeLists.txt)" >&2
  exit 1
fi
REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)"

if [[ -z "$BUILD_DIR" ]]; then
  BUILD_DIR="${REPO_ROOT}/build-${BUILD_TYPE}"
fi
BUILD_DIR="$(cd "$(dirname "${BUILD_DIR}")" && pwd)/$(basename "${BUILD_DIR}")"

if [[ $CLEAN -eq 1 ]]; then
  echo "=== 清理构建目录: ${BUILD_DIR} ==="
  rm -rf "${BUILD_DIR}"
fi

echo "=== 构建 (${BUILD_TYPE}) 于 ${BUILD_DIR} ==="
mkdir -p "${BUILD_DIR}"
cmake -S "${REPO_ROOT}" -B "${BUILD_DIR}" \
  -D SIMDJSON_DEVELOPER_MODE=ON \
  -D CMAKE_BUILD_TYPE="${BUILD_TYPE}"

ARGS=(--build "${BUILD_DIR}" --config "${BUILD_TYPE}" -j"$(nproc)")
if [[ -n "$TARGET" ]]; then
  ARGS+=(--target "${TARGET}")
fi
cmake "${ARGS[@]}"

echo "=== 完成: ${BUILD_DIR} ==="
ls "${BUILD_DIR}/benchmark/dom/parse" >/dev/null 2>&1 && echo "parse 基准已就绪: ${BUILD_DIR}/benchmark/dom/parse"
