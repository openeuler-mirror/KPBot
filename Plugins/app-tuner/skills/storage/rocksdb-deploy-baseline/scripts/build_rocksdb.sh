#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
apply_deps_env "$RESULT_DIR/env/deps.env"
WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
MAVEN_REPO=${MAVEN_REPO:-$WORK_ROOT/m2/repository}
ROCKSDB_VERSION=${ROCKSDB_VERSION:-6.26.1}
ROCKSDB_SRC=${ROCKSDB_SRC:-$WORK_ROOT/rocksdb-$ROCKSDB_VERSION-base}
MAVEN_DIR="$MAVEN_REPO/org/rocksdb/rocksdbjni/$ROCKSDB_VERSION"

mkdir -p "$RESULT_DIR/env" "$WORK_ROOT"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  if [[ ! -d "$ROCKSDB_SRC" ]]; then
    run git clone --branch "v$ROCKSDB_VERSION" --depth 1 \
      https://github.com/facebook/rocksdb.git "$ROCKSDB_SRC"
  fi
  run make rocksdbjava -j"$(nproc)" PORTABLE=1 DEBUG_LEVEL=0 \
    DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1
  run mkdir -p "$MAVEN_DIR"
  run cp "$ROCKSDB_SRC/java/target/rocksdbjni-$ROCKSDB_VERSION-linux64.jar" "$MAVEN_DIR/rocksdbjni-$ROCKSDB_VERSION.jar"
  log_info "dry-run: RocksDB build commands printed"
  exit 0
fi

require_cmd git
require_cmd make
if [[ ! -d "$ROCKSDB_SRC" ]]; then
  if [[ -n "${ROCKSDB_TARBALL:-}" ]]; then
    mkdir -p "$ROCKSDB_SRC"
    run tar -xf "$ROCKSDB_TARBALL" -C "$(dirname "$ROCKSDB_SRC")"
  else
    run git clone --branch "v$ROCKSDB_VERSION" --depth 1 \
      https://github.com/facebook/rocksdb.git "$ROCKSDB_SRC"
  fi
fi

version_h="$ROCKSDB_SRC/include/rocksdb/version.h"
if [[ -f "$version_h" ]]; then
  actual_version=$(awk '/#define ROCKSDB_MAJOR/{major=$3} /#define ROCKSDB_MINOR/{minor=$3} /#define ROCKSDB_PATCH/{patch=$3} END{print major "." minor "." patch}' "$version_h")
  [[ "$actual_version" == "$ROCKSDB_VERSION" ]] || die "RocksDB source version $actual_version does not match requested $ROCKSDB_VERSION" 2
fi

(cd "$ROCKSDB_SRC" && make rocksdbjava -j"$(nproc)" PORTABLE=1 DEBUG_LEVEL=0 \
  DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1)
ROCKSDB_JAR="$ROCKSDB_SRC/java/target/rocksdbjni-$ROCKSDB_VERSION-linux64.jar"
[[ -f "$ROCKSDB_JAR" ]] || die "expected jar not found: $ROCKSDB_JAR" 2

mkdir -p "$MAVEN_DIR"
cp "$ROCKSDB_JAR" "$MAVEN_DIR/rocksdbjni-$ROCKSDB_VERSION.jar"
if [[ -f "$ROCKSDB_JAR.sha1" ]]; then
  cp "$ROCKSDB_JAR.sha1" "$MAVEN_DIR/rocksdbjni-$ROCKSDB_VERSION.jar.sha1"
fi

{
  printf 'ROCKSDB_SRC=%s\n' "$ROCKSDB_SRC"
  printf 'ROCKSDB_JAR=%s\n' "$MAVEN_DIR/rocksdbjni-$ROCKSDB_VERSION.jar"
  printf 'ROCKSDB_VERSION=%s\n' "$ROCKSDB_VERSION"
  printf 'ROCKSDB_FLAGS=%s\n' "PORTABLE=1 DEBUG_LEVEL=0 DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1"
  printf 'ROCKSDB_JAR_SHA256=%s\n' "$(sha256sum "$MAVEN_DIR/rocksdbjni-$ROCKSDB_VERSION.jar" | awk '{print $1}')"
  printf 'MAVEN_REPO=%s\n' "$MAVEN_REPO"
  printf 'ROCKSDB_BUILD=%s\n' "$(gcc --version 2>/dev/null | head -1)"
} > "$RESULT_DIR/env/rocksdb.env"

log_info "RocksDB build OK: $ROCKSDB_JAR"
