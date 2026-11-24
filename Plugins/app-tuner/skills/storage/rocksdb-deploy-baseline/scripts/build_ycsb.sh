#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-deploy-result}
WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
YCSB_COMMIT=${YCSB_COMMIT:-19e885f7cb780fdded0547853f7810a150554caf}
ROCKSDB_VERSION=${ROCKSDB_VERSION:-6.26.1}
YCSB_SRC=${YCSB_SRC:-}
YCSB_RUN_ID=${YCSB_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}
YCSB_WORK=${YCSB_WORK:-$WORK_ROOT/src/YCSB-$YCSB_RUN_ID}
ARTIFACT_DIR=${ARTIFACT_DIR:-$WORK_ROOT/artifacts}
YCSB_DIST=${YCSB_DIST:-$ARTIFACT_DIR/YCSB_DIST}
YCSB_OPTIONS="$SKILL_ROOT/assets/config/rocksdb-default.ini"
MAVEN_SETTINGS=${MAVEN_SETTINGS:-}
MAVEN_REPO=${MAVEN_REPO:-$WORK_ROOT/m2/repository}

apply_deps_env "$RESULT_DIR/env/deps.env"
mkdir -p "$RESULT_DIR/env" "$ARTIFACT_DIR" "$MAVEN_REPO"

mvn_args=(-Dmaven.repo.local="$MAVEN_REPO" -Drocksdb.version="$ROCKSDB_VERSION"
  -pl site.ycsb:rocksdb-binding -am clean package -DskipTests)
[[ -n "$MAVEN_SETTINGS" ]] && mvn_args=(-s "$MAVEN_SETTINGS" "${mvn_args[@]}")

ensure_ycsb_work() {
  if [[ ! -d "$YCSB_WORK/.git" ]]; then
    if [[ -e "$YCSB_WORK" && -n "$(ls -A "$YCSB_WORK" 2>/dev/null || true)" ]]; then
      die "existing YCSB_WORK is not a git checkout; refusing to modify it: $YCSB_WORK" 2
    fi
    if [[ -n "$YCSB_SRC" ]]; then
      run git clone "$YCSB_SRC" "$YCSB_WORK"
    else
      run git clone https://github.com/brianfrankcooper/YCSB.git "$YCSB_WORK"
    fi
  fi
  if [[ -n "$(git -C "$YCSB_WORK" status --porcelain 2>/dev/null || true)" ]]; then
    die "existing YCSB_WORK has local changes; refusing to reset or clean it: $YCSB_WORK" 2
  fi
  run git -C "$YCSB_WORK" checkout "$YCSB_COMMIT"
}

extract_dist() {
  local tar_file=$1
  local target=$2
  local stage="${target}.staging.$(date +%Y%m%d-%H%M%S)-$$"
  local backup
  mkdir -p "$stage"
  tar -xzf "$tar_file" --strip-components=1 -C "$stage"
  [[ -x "$stage/bin/ycsb" ]] || die "staged distribution missing bin/ycsb: $stage" 2
  [[ -f "$stage/lib/rocksdbjni-$ROCKSDB_VERSION.jar" ]] || \
    die "staged distribution missing rocksdbjni-$ROCKSDB_VERSION.jar" 2
  [[ -f "$stage/workloads/workloada" ]] || die "staged distribution missing workloads" 2

  local expected_jar
  expected_jar=$(awk -F= '$1 == "ROCKSDB_JAR" {print $2; exit}' "$RESULT_DIR/env/rocksdb.env" 2>/dev/null || true)
  if [[ -n "$expected_jar" && -f "$expected_jar" ]]; then
    local expected_sha actual_sha
    expected_sha=$(sha256sum "$expected_jar" | awk '{print $1}')
    actual_sha=$(sha256sum "$stage/lib/rocksdbjni-$ROCKSDB_VERSION.jar" | awk '{print $1}')
    [[ "$expected_sha" == "$actual_sha" ]] || die "YCSB distribution rocksdbjni SHA-256 mismatch" 2
  fi

  if [[ -e "$target" ]]; then
    backup="${target}.backup.$(date +%Y%m%d-%H%M%S)-$$"
    mv "$target" "$backup"
    log_info "existing distribution backed up to $backup; remove it manually after review"
    if ! mv "$stage" "$target"; then
      mv "$backup" "$target"
      die "failed to switch YCSB distribution; restored previous directory" 2
    fi
  else
    mv "$stage" "$target"
  fi
}

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  if [[ ! -d "$YCSB_WORK/.git" ]]; then
    if [[ -n "$YCSB_SRC" ]]; then
      run git clone "$YCSB_SRC" "$YCSB_WORK"
    else
      run git clone https://github.com/brianfrankcooper/YCSB.git "$YCSB_WORK"
    fi
  fi
  run git -C "$YCSB_WORK" checkout "$YCSB_COMMIT"
  run mvn "${mvn_args[@]}"
  printf '[dry-run] extract_dist %s/rocksdb/target/ycsb-rocksdb-binding-*.tar.gz %s\n' "$YCSB_WORK" "$YCSB_DIST"
  log_info "dry-run: YCSB build commands printed"
  exit 0
fi

require_cmd git
require_cmd mvn
require_cmd tar
require_cmd sha256sum
[[ -f "$YCSB_OPTIONS" ]] || die "default options file missing: $YCSB_OPTIONS" 2
[[ -f "$RESULT_DIR/env/rocksdb.env" ]] || die "rocksdb.env is missing; run build_rocksdb.sh first" 2

ensure_ycsb_work
(cd "$YCSB_WORK" && mvn "${mvn_args[@]}")

tar_file=$(find "$YCSB_WORK/rocksdb/target" -maxdepth 1 -name 'ycsb-rocksdb-binding-*.tar.gz' \
  -printf '%T@ %p\n' | sort -nr | awk 'NR == 1 {print $2}')
[[ -n "$tar_file" ]] || die "YCSB distribution tar not found under $YCSB_WORK/rocksdb/target" 2
extract_dist "$tar_file" "$YCSB_DIST"

{
  if [[ -f "$RESULT_DIR/env/deps.env" ]]; then
    while IFS= read -r line; do printf '%s\n' "$line"; done < "$RESULT_DIR/env/deps.env"
  else
    printf 'JAVA_VERSION=%s\n' "$(java -version 2>&1 | head -1)"
    printf 'GCC_VERSION=%s\n' "$(gcc --version 2>/dev/null | head -1)"
    printf 'MAVEN_VERSION=%s\n' "$(mvn -version 2>&1 | head -1)"
  fi
  [[ -f "$RESULT_DIR/env/rocksdb.env" ]] && cat "$RESULT_DIR/env/rocksdb.env"
  printf 'DEPLOY_STATUS=ok\n'
  printf 'YCSB_COMMIT=%s\n' "$YCSB_COMMIT"
  printf 'YCSB_RUN_ID=%s\n' "$YCSB_RUN_ID"
  printf 'YCSB_WORK=%s\n' "$YCSB_WORK"
  printf 'YCSB_DIST=%s\n' "$YCSB_DIST"
  printf 'YCSB_BIN=%s\n' "$YCSB_DIST/bin/ycsb"
  printf 'YCSB_LIB=%s\n' "$YCSB_DIST/lib"
  printf 'YCSB_WORKLOADS=%s\n' "$YCSB_DIST/workloads"
  printf 'YCSB_OPTIONS=%s\n' "$YCSB_OPTIONS"
  printf 'MAVEN_REPO=%s\n' "$MAVEN_REPO"
} > "$RESULT_DIR/env/deploy.env"

log_info "YCSB distribution: $YCSB_DIST"
