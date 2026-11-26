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
  --clean   清理后重建 (只允许清理源码目录内的构建目录; 拒绝 /、家目录、源码根与含 .. 的路径)
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
REPO_ROOT="$(realpath "${REPO_ROOT}")"

if [[ -z "$BUILD_DIR" ]]; then
  BUILD_DIR="${REPO_ROOT}/build-${BUILD_TYPE}"
fi
RAW_BUILD_DIR="$BUILD_DIR"
BUILD_DIR="$(realpath -m -- "${BUILD_DIR}")"

if [[ $CLEAN -eq 1 ]]; then
  clean_refuse() {
    echo "错误: 拒绝清理构建目录 '${RAW_BUILD_DIR}' (解析为 '${BUILD_DIR}'): $1" >&2
    exit 2
  }
  case "$RAW_BUILD_DIR" in
    ..|../*|*/..|*/../*) clean_refuse "路径包含 .. 分量";;
  esac
  [[ -n "$BUILD_DIR" ]] || clean_refuse "路径为空"
  [[ "$BUILD_DIR" != "/" ]] || clean_refuse "路径解析为文件系统根 /"
  if [[ -n "${HOME:-}" ]]; then
    [[ "$BUILD_DIR" != "$HOME" ]] || clean_refuse "路径解析为家目录"
  fi
  [[ "$BUILD_DIR" != "$REPO_ROOT" ]] || clean_refuse "路径解析为源码根目录本身"
  case "$BUILD_DIR" in
    "$REPO_ROOT"/*) ;;
    *) clean_refuse "规范化后必须位于源码目录 ${REPO_ROOT} 之内";;
  esac
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
