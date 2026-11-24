#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
DATA_DIR=${DATA_DIR:-/data/workload_data}

mkdir -p "$RESULT_DIR/env" "$WORK_ROOT"

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  if [[ "${SKIP_ROOT_CHECK:-0}" != "1" && "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "install_deps requires root" 2
  fi
  require_cmd yum
fi

packages=(
  git make gcc-c++ maven numactl wget tar python3
  snappy snappy-devel zlib zlib-devel bzip2 bzip2-devel
  lz4 lz4-devel zstd zstd-devel gflags gflags-devel flex
  java-1.8.0-openjdk-devel
)

run yum install -y "${packages[@]}"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log_info "dry-run: dependencies command printed"
  exit 0
fi

if [[ -z "${JAVA_HOME:-}" ]]; then
  for candidate in /usr/lib/jvm/java-1.8.0 /usr/lib/jvm/java-1.8.0-openjdk /usr/lib/jvm/java-1.8.0-openjdk-*; do
    if [[ -x "$candidate/bin/javac" ]]; then
      JAVA_HOME="$candidate"
      break
    fi
  done
fi
[[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/javac" ]] || die "Java 8 not found; set JAVA_HOME to a JDK 8" 2
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"
JAVA_VERSION=$("$JAVA_HOME/bin/java" -version 2>&1 | head -1)
[[ "$JAVA_VERSION" == *"1.8."* ]] || die "JAVA_HOME must be Java 8, got: $JAVA_VERSION" 2

GCC_VERSION=$(gcc --version 2>/dev/null | head -1 || true)
if [[ "$GCC_VERSION" != *"12.3.1"* ]]; then
  if [[ -n "${GCC_ROOT:-}" && -x "$GCC_ROOT/bin/gcc" ]]; then
    export PATH="$GCC_ROOT/bin:$PATH"
    export LD_LIBRARY_PATH="$GCC_ROOT/lib64:${LD_LIBRARY_PATH:-}"
    export INCLUDE="$GCC_ROOT/include:${INCLUDE:-}"
    GCC_VERSION=$(gcc --version | head -1)
  else
    GCC_ROOT="$WORK_ROOT/toolchain/gcc-12.3.1-2025.06-aarch64-linux"
    GCC_ARCHIVE_URL=${GCC_ARCHIVE_URL:-https://mirrors.huaweicloud.com/kunpeng/archive/compiler/kunpeng_gcc/gcc-12.3.1-2025.06-aarch64-linux.tar.gz}
    GCC_ARCHIVE_SHA256=${GCC_ARCHIVE_SHA256:-}
    mkdir -p "$WORK_ROOT/toolchain"
    if [[ ! -x "$GCC_ROOT/bin/gcc" ]]; then
      [[ -n "$GCC_ARCHIVE_SHA256" ]] || die "GCC_ARCHIVE_SHA256 is required before downloading the gcc toolchain" 2
      run wget -O "$WORK_ROOT/toolchain/gcc-12.3.1.tar.gz" "$GCC_ARCHIVE_URL"
      actual_sha256=$(sha256sum "$WORK_ROOT/toolchain/gcc-12.3.1.tar.gz" | awk '{print $1}')
      [[ "$actual_sha256" == "$GCC_ARCHIVE_SHA256" ]] || die "gcc archive checksum mismatch: expected $GCC_ARCHIVE_SHA256, got $actual_sha256" 2
      run tar -xf "$WORK_ROOT/toolchain/gcc-12.3.1.tar.gz" -C "$WORK_ROOT/toolchain"
    fi
    export PATH="$GCC_ROOT/bin:$PATH"
    export LD_LIBRARY_PATH="$GCC_ROOT/lib64:${LD_LIBRARY_PATH:-}"
    export INCLUDE="$GCC_ROOT/include:${INCLUDE:-}"
    GCC_VERSION=$(gcc --version | head -1)
  fi
fi
[[ "$GCC_VERSION" == *"12.3.1"* ]] || die "gcc 12.3.1 is required, got: $GCC_VERSION" 2

MAVEN_VERSION=$(mvn -version 2>&1 | head -1)
{
  printf 'JAVA_HOME=%s\n' "$JAVA_HOME"
  printf 'JAVA_VERSION=%s\n' "$JAVA_VERSION"
  printf 'GCC_VERSION=%s\n' "$GCC_VERSION"
  printf 'MAVEN_VERSION=%s\n' "$MAVEN_VERSION"
  printf 'GCC_ROOT=%s\n' "${GCC_ROOT:-system}"
} > "$RESULT_DIR/env/deps.env"

log_info "dependencies OK: $JAVA_VERSION; $GCC_VERSION; $MAVEN_VERSION"
