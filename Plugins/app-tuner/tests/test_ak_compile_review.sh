#!/usr/bin/env bash
# Regression cases for target selection, CSS PGO and collection checkpoints.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
skill="$dir/../skills/kpbot-app-tuner/subskills/compiler-optimization"
script="$skill/scripts/ak_compile_optimize.sh"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

bash "$script" --pid 12 --duration 90 --page-size 4096 --help >/dev/null
for args in '--pid 0' '--pid abc' '--duration -1' '--page-size abc'; do
  if bash "$script" $args --help >/dev/null 2>&1; then exit 1; fi
done
echo 'PASS numeric arguments'

AK_TEST_SOURCE_ONLY=1 source "$script"
(
  # A child captures the exact argv, without invoking Docker or a compiler.
  CHAIN_SELF="$scratch/child.sh" CHAIN_DIR="$scratch" CHAIN_START=$(date +%s)
  printf '%s\n' 'printf "%s\n" "$@" > "$ARGS_FILE"' > "$CHAIN_SELF"
  export ARGS_FILE="$scratch/child.args"
  CONTAINER=target DRY_RUN=true
  run_chain_stage sample --component python
  grep -A1 -x -- '--container' "$ARGS_FILE" | grep -qx target
  grep -qx -- '--dry-run' "$ARGS_FILE"
)
echo 'PASS chain child arguments'

(
  # Use the real run_chain state machine with fake build stages and a profile reader.
  CHAIN_DIR="$scratch/checkpoints" CHAIN_BISHENG_PREFIX="$scratch/toolchain"
  BISHENG_PREFIX_OPT="$CHAIN_BISHENG_PREFIX" CHAIN=pytorch
  PROFILE_DIR="$scratch/profile root"
  mkdir -p "$PROFILE_DIR" "$CHAIN_BISHENG_PREFIX/bin"
  printf keep > "$PROFILE_DIR/previous.profraw"
  printf '%s\n' '#!/bin/sh' 'test "$1" = show && grep -qx valid "$2"' > "$CHAIN_BISHENG_PREFIX/bin/llvm-profdata"
  chmod +x "$CHAIN_BISHENG_PREFIX/bin/llvm-profdata"
  setup_chain_ccache() { :; }
  resolve_chain_python() { CHAIN_PYTHON_EXE=python3; }
  run_chain_stage() { printf '%s\n' "$*" >> "$scratch/stages"; }
  CHAIN_WORKLOAD=false
  if (run_chain); then exit 1; fi
  [[ $(chain_state read collect_done) == failed ]]
  CHAIN_RESUME=true
  CHAIN_WORKLOAD='printf corrupt > "${LLVM_PROFILE_FILE%/*}/bad.profraw"'
  if (run_chain); then exit 1; fi
  [[ $(chain_state read collect_done) == failed ]]
  CHAIN_WORKLOAD='printf valid > "${LLVM_PROFILE_FILE%/*}/new.profraw"'
  run_chain
  current=$(chain_state read profile_run_dir)
  [[ $(chain_state read collect_done) == done && -s "$current/new.profraw" ]]
  [[ $(cat "$scratch/profile root/previous.profraw") == keep ]]
  grep -F -- "--profile-dir $current" "$scratch/stages" | grep -q profile_use
  before=$(wc -l < "$scratch/stages")
  CHAIN_WORKLOAD=false
  run_chain
  [[ $(wc -l < "$scratch/stages") == "$before" ]]
)
echo 'PASS isolated profiles, invalid data and resume'

(
  SRC="$scratch/npu"
  mkdir -p "$SRC"
  printf "%s\n" "if True:" "    cmake_args.append('-DBUILD_TENSORPIPE=on')" \
    "define_macros=[('_GLIBCXX_USE_CXX11_ABI', '0'), ('GLIBCXX_USE_CXX11_ABI', '0')]" > "$SRC/setup.py"
  patch="$skill/references/patches/torch_npu-v2.4.0-6.0.0-abi-env.sh"
  bash "$patch" "$SRC" torch_npu 2.4.0-6.0.0
  cp "$SRC/setup.py" "$scratch/first"
  bash "$patch" "$SRC" torch_npu 2.4.0-6.0.0
  cmp "$scratch/first" "$SRC/setup.py"
  [[ $(grep -Fc "cmake_args.append('-DGLIBCXX_USE_CXX11_ABI=" "$SRC/setup.py") == 1 ]]
  grep -Fq "('_GLIBCXX_USE_CXX11_ABI', os.environ.get(" "$SRC/setup.py"
)
echo 'PASS ABI patch idempotence'

(
  CHECK_ONLY=true AK_AUTO_INSTALL_DEPS=1 BISHENG_CLANG=clang TGT_ENV="" PYTHON_EXEC=python3
  run_in_target() { return 1; }
  auto_fix_dependencies() { touch "$scratch/unwanted-install"; }
  check_dependencies
  [[ ! -e "$scratch/unwanted-install" ]]
)
echo 'PASS check-only with missing dependencies'

(
  # Capture configure argv as delivered by the real command builder.
  OPTIMIZE=css_pgo REUSE_BUILD=true DRY_RUN=true MCPU=test JOBS=1
  BISHENG_CLANG=clang BISHENG_CXX=clang++ TGT_ENV=""
  CSS_PROFILE="$scratch/selected.spgo"
  printf '%s\n' 'printf "%s\n" "$@" > "$CONFIG_ARGS"' > "$scratch/configure"
  chmod +x "$scratch/configure"
  printf '#!/bin/sh\nexit 0\n' > "$scratch/make"; chmod +x "$scratch/make"
  export CONFIG_ARGS="$scratch/config.args" PATH="$scratch:$PATH"
  run_in_target() { bash -c "$1"; }
  assert_bisheng_active() { :; }; audit_build_log() { :; }; log_eta_reference() { :; }
  STAGE=gen; compile_python "$scratch" "$scratch/install"
  grep '^CFLAGS=.*-fpseudo-probe-for-profiling.*-g' "$CONFIG_ARGS"
  STAGE=use; compile_python "$scratch" "$scratch/install"
  for var in CFLAGS LDFLAGS; do
    grep "^$var=.*-fpseudo-probe-for-profiling" "$CONFIG_ARGS" | grep -F -- "-fprofile-sample-use=$CSS_PROFILE"
  done
)
echo 'PASS Python CSS gen/use configure flags'

(
  # Emulate docker exec, including stdin streaming of a host-side patch.
  CONTAINER=build-container COMPONENT=torch_npu SOURCE_DIR='/build path' CURRENT_VERSION=2.4
  docker() {
    [[ "$1" == exec && "$2" == -i && "$3" == build-container ]] || return 1
    shift 3; "$@"
  }
  AK_PATCH_SCRIPT="$scratch/host patch"
  printf 'printf "%%s\\n" "$@" > "$PATCH_ARGS"\n' > "$AK_PATCH_SCRIPT"
  export PATCH_ARGS="$scratch/patch.args"
  run_patch_scripts
  printf '%s\n' "$SOURCE_DIR" "$COMPONENT" "$CURRENT_VERSION" > "$scratch/expected"
  cmp "$PATCH_ARGS" "$scratch/expected"
)
echo 'PASS host patch streamed into target'

(
  # Exercise the collect path with filenames that require distinct argv entries.
  PROFILE_DIR="$scratch/css samples" COMPONENT=python
  BINARY_PATH="$scratch/python's gen binary" CSS_PROFILE="$scratch/output profile"
  COLLECT_CMD=true BISHENG_PREFIX="$scratch/tools"
  mkdir -p "$BISHENG_PREFIX/bin"
  touch "$BINARY_PATH"
  export PERF_BINARY="$BINARY_PATH" EXPECT_PROFILE="$CSS_PROFILE"
  cat > "$BISHENG_PREFIX/bin/llvm-profgen" <<'PROF'
#!/bin/bash
set -eu
[[ "$1" == "--binary=$(readlink -f "$PERF_BINARY")" ]]
[[ "$2" == "--perfscript="* && -f "${2#--perfscript=}" ]]
[[ "$3" == "--output=$EXPECT_PROFILE" ]]
printf 'function:20:1\n' > "$EXPECT_PROFILE"
PROF
  cat > "$BISHENG_PREFIX/bin/perf" <<'PERF'
#!/bin/bash
set -eu
if [[ "$1" == script ]]; then
  basename "$PERF_BINARY"
else
  while [[ "$1" != -o ]]; do shift; done
  touch "$2"
fi
PERF
  chmod +x "$BISHENG_PREFIX/bin/"*
  export PATH="$BISHENG_PREFIX/bin:$PATH"
  check_brbe_support() { :; }
  collect_css_profile
  check_css_profile
)
echo 'PASS CSS paths across perf and llvm-profgen'
