#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  build_faiss.sh \
    --repo DIR \
    --algorithm rabitq|turboquant|ivfpq|ivfpqfs|hnsw \
    --mode baseline|current-head-profile|candidate \
    --run-dir DIR \
    [--krl on|off] \
    [--build-dir DIR] \
    [--jobs N] \
    [--blas PATH] \
    [--candidate-macro NAME] \
    [--clean] \
    [--cmake-arg ARG]

The script builds the shared target `faiss` and writes auditable build evidence
to --run-dir. In candidate mode --candidate-macro names the definition to verify;
enable its CMake option with --cmake-arg when needed. The definition must already
be integrated into the build targets. BLAS: when --blas is not given, no
MKL_LIBRARIES is passed and the build system's BLAS discovery is used; pass
--blas only when the current machine requires an explicit library path.
--krl is a compatibility option for source trees that support that backend.
When omitted, no KRL CMake option is injected and the manifest records null.
EOF
}

repo=""
algorithm=""
mode=""
run_dir=""
build_dir=""
jobs="$(nproc)"
blas=""
krl=""
candidate_macro=""
clean="false"
declare -a extra_cmake_args=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --algorithm) algorithm="$2"; shift 2 ;;
    --mode) mode="$2"; shift 2 ;;
    --run-dir) run_dir="$2"; shift 2 ;;
    --build-dir) build_dir="$2"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --blas) blas="$2"; shift 2 ;;
    --krl) krl="$2"; shift 2 ;;
    --candidate-macro) candidate_macro="$2"; shift 2 ;;
    --clean) clean="true"; shift ;;
    --cmake-arg) extra_cmake_args+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$repo" ] || [ -z "$algorithm" ] || [ -z "$mode" ] || [ -z "$run_dir" ]; then
  echo "--repo, --algorithm, --mode and --run-dir are required" >&2
  usage >&2
  exit 2
fi

case "$algorithm" in
  rabitq|turboquant|ivfpq|ivfpqfs|hnsw) ;;
  *) echo "Unsupported algorithm: $algorithm" >&2; exit 2 ;;
esac

case "$mode" in
  baseline|current-head-profile|candidate) ;;
  *) echo "Unsupported mode: $mode" >&2; exit 2 ;;
esac

case "$krl" in
  ""|on|off) ;;
  *) echo "--krl must be on or off" >&2; exit 2 ;;
esac

if ! [[ "$jobs" =~ ^[1-9][0-9]*$ ]]; then
  echo "--jobs must be a positive integer" >&2
  exit 2
fi

if [ "$mode" = "candidate" ] && [ -z "$candidate_macro" ]; then
  echo "--candidate-macro is required in candidate mode" >&2
  exit 2
fi

if [ "$mode" != "candidate" ] && [ -n "$candidate_macro" ]; then
  echo "--candidate-macro is only valid in candidate mode" >&2
  exit 2
fi

if [ -n "$candidate_macro" ] && ! [[ "$candidate_macro" =~ ^FAISS_[A-Z0-9_]+$ ]]; then
  echo "Invalid candidate macro: $candidate_macro" >&2
  exit 2
fi

if [ ! -d "$repo" ] || [ ! -f "$repo/CMakeLists.txt" ] || [ ! -f "$repo/faiss/CMakeLists.txt" ]; then
  echo "Invalid Faiss repository: $repo" >&2
  exit 2
fi

repo="$(realpath "$repo")"
if [ -z "$build_dir" ]; then
  build_dir="$repo/build"
fi
build_dir="$(realpath -m "$build_dir")"
run_dir="$(realpath -m "$run_dir")"

case "$build_dir" in
  "$repo"/build|"$repo"/build-*) ;;
  *)
    echo "Refusing build directory outside the approved repo build path: $build_dir" >&2
    exit 3
    ;;
esac

if [ "$run_dir" = "/" ] || [ "$run_dir" = "$repo" ]; then
  echo "Unsafe run directory: $run_dir" >&2
  exit 3
fi

mkdir -p "$run_dir"

if [ "$clean" = "true" ] && { [ -e "$build_dir" ] || [ -L "$build_dir" ]; }; then
  rm -rf -- "$build_dir"
fi

source_commit="$(git -C "$repo" rev-parse HEAD)"
dirty="false"
dirty_files=""
if [ -n "$(git -C "$repo" status --porcelain --untracked-files=normal)" ]; then
  dirty="true"
  dirty_files="$(git -C "$repo" status --porcelain --untracked-files=normal)"
fi

if [ "$mode" = "baseline" ] && [ "$dirty" = "true" ]; then
  echo "Baseline mode requires a clean worktree; refusing to build dirty sources" >&2
  exit 4
fi

cmakelists_sha256="$(sha256sum "$repo/faiss/CMakeLists.txt" | awk '{print $1}')"
krl_value="OFF"
if [ "$krl" = "on" ]; then
  krl_value="ON"
fi

declare -a configure_args=(
  -S "$repo"
  -B "$build_dir"
  -DFAISS_ENABLE_GPU=OFF
  -DBUILD_TESTING=OFF
  -DBUILD_SHARED_LIBS=ON
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
  -DFAISS_OPT_LEVEL=generic
  -DFAISS_ENABLE_PYTHON=OFF
)
if [ -n "$krl" ]; then
  configure_args+=("-DKRL=$krl_value")
fi
configure_args+=("${extra_cmake_args[@]}")

# BLAS：未显式指定时不传 MKL_LIBRARIES，由构建系统（CMake）的发现机制选择本机 BLAS
if [ -n "$blas" ]; then
  configure_args+=("-DMKL_LIBRARIES=$blas")
fi

cmake "${configure_args[@]}" 2>&1 | tee "$run_dir/cmake-configure.log"

# The manifest below describes this helper's fixed build shape. Check the actual
# cache so extra arguments or project FORCE assignments cannot invalidate it.
python3 - "$build_dir/CMakeCache.txt" "$krl" <<'PY'
import sys
from pathlib import Path

cache = {}
try:
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        if line.startswith(("#", "//")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cache[key.split(":", 1)[0]] = value
except OSError as exc:
    raise SystemExit(f"Cannot verify effective CMake settings: {exc}")

expected = {"CMAKE_BUILD_TYPE": "Release", "FAISS_OPT_LEVEL": "generic"}
for key, value in expected.items():
    if cache.get(key, "").lower() != value.lower():
        raise SystemExit(f"Effective {key}={cache.get(key)!r} conflicts with helper manifest {value!r}; use native CMake for a different build shape")
if sys.argv[2]:
    enabled = cache.get("KRL", "").upper() in {"1", "ON", "YES", "TRUE", "Y"}
    if "KRL" not in cache or enabled != (sys.argv[2] == "on"):
        raise SystemExit("Effective KRL setting conflicts with --krl and the helper manifest")
PY

cmake --build "$build_dir" --config Release -j"$jobs" --target faiss 2>&1 | tee "$run_dir/build.log"

library="$build_dir/faiss/libfaiss.so"
compile_commands="$build_dir/compile_commands.json"

if [ ! -f "$library" ]; then
  echo "Missing build artifact: $library" >&2
  exit 5
fi

if [ ! -f "$compile_commands" ]; then
  echo "Missing compile commands: $compile_commands" >&2
  exit 5
fi

python3 - "$compile_commands" "$run_dir/compile-definitions.txt" <<'PY'
import json
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
entries = json.loads(source.read_text(encoding="utf-8"))
definitions = set()
pattern = re.compile(r"-D(?:FAISS_[A-Za-z0-9_]+|KRL)(?:=[^\s\"']+)?")
for entry in entries:
    text = entry.get("command") or " ".join(entry.get("arguments", []))
    definitions.update(pattern.findall(text))
destination.write_text("\n".join(sorted(definitions)) + "\n", encoding="utf-8")
PY

if [ "$mode" = "candidate" ]; then
  if ! grep -Eq -- "^-D${candidate_macro}(=|$)" "$run_dir/compile-definitions.txt"; then
    echo "Candidate macro is absent from real compile commands: $candidate_macro" >&2
    exit 6
  fi
fi

sha256sum "$library" > "$run_dir/libfaiss.sha256"
library_sha256="$(awk '{print $1}' "$run_dir/libfaiss.sha256")"
compiler_c="$(cc --version 2>/dev/null | head -n1 || true)"
compiler_cxx="$(c++ --version 2>/dev/null | head -n1 || true)"

python3 - \
  "$run_dir/BUILD-MANIFEST.json" \
  "$mode" "$algorithm" "$repo" "$source_commit" "$dirty" \
  "$candidate_macro" "$build_dir" "$library" "$library_sha256" \
  "$cmakelists_sha256" "$krl" "$jobs" "$blas" \
  "$compiler_c" "$compiler_cxx" "$dirty_files" <<'PY'
import json
import sys
from pathlib import Path

(
    destination,
    mode,
    algorithm,
    repo,
    source_commit,
    dirty,
    candidate_macro,
    build_dir,
    library,
    library_sha256,
    cmakelists_sha256,
    krl,
    jobs,
    blas,
    compiler_c,
    compiler_cxx,
    dirty_files,
) = sys.argv[1:]

payload = {
    "schema_version": "faiss-build-manifest.v1",
    "build_mode": mode,
    "algorithm": algorithm,
    "repo": repo,
    "source_commit": source_commit,
    "dirty": dirty == "true",
    "dirty_files": dirty_files.splitlines() if dirty_files else [],
    "candidate_macro": candidate_macro or None,
    "build_dir": build_dir,
    "library": library,
    "library_sha256": library_sha256,
    "cmakelists_sha256": cmakelists_sha256,
    "krl": (krl == "on") if krl else None,
    "build_type": "Release",
    "faiss_opt_level": "generic",
    "jobs": int(jobs),
    "blas": blas,
    "compiler_c": compiler_c,
    "compiler_cxx": compiler_cxx,
}
Path(destination).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

echo "Faiss build completed"
echo "Library: $library"
echo "Evidence: $run_dir"
