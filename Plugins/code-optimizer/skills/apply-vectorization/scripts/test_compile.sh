#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd "$script_dir/.." && pwd)"
fixtures_dir="$skill_dir/assets/fixtures"
cases_dir="$skill_dir/assets/real-source-cases"
generate_dir="$cases_dir/generate"
preflight_script="$script_dir/preflight_benchmark_env.py"
compiler_support_script="$script_dir/detect_compiler_support.py"
target_arch="${APPLY_VECTORIZATION_TARGET_ARCH:-neon}"
compile_only=false
extra_sources=()
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if ! command -v cc >/dev/null 2>&1; then
  echo "跳过：宿主机缺少可用的 C 编译器"
  exit 2
fi

usage() {
  cat <<'EOF'
用法:
  test_compile.sh [neon|sve|sme]
  test_compile.sh --arch <neon|sve|sme> --compile-only [--source <path> ...]

说明:
  - 默认模式会先做运行时 preflight，只编译当前 generate/ 中可运行架构的产物。
  - --compile-only 只使用编译器支持探测得到的架构 flags 做语法/编译级检查，不要求本机支持运行该 ISA。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch)
      shift
      target_arch="${1:-}"
      ;;
    --compile-only)
      compile_only=true
      ;;
    --source)
      shift
      extra_sources+=("${1:-}")
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    neon|sve|sme)
      target_arch="$1"
      ;;
    *)
      echo "[错误] 不支持的参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if [[ "$target_arch" != "neon" && "$target_arch" != "sve" && "$target_arch" != "sme" ]]; then
  echo "[错误] target arch 必须是 neon|sve|sme: $target_arch" >&2
  exit 1
fi

scalar_sources=(
  "$fixtures_dir/simple_add_scalar.c"
  "$fixtures_dir/nested_bias_scalar.c"
  "$fixtures_dir/not_vectorizable_prefix_sum.c"
  "$fixtures_dir/irregular_scatter_matmul.c"
)

echo "[信息] 正在对标量夹具做烟测编译"
for src in "${scalar_sources[@]}"; do
  out="$tmp_dir/$(basename "${src%.c}").o"
  cc -std=c11 -O2 -c "$src" -o "$out"
done

echo "[信息] 正在对 case 源码的 scalar/driver 做烟测编译"
while IFS= read -r src; do
  out="$tmp_dir/$(basename "${src%.c}").o"
  cc -std=c11 -O2 -c "$src" -o "$out"
done < <(
  find "$cases_dir" -maxdepth 1 -type f \
    \( -name 'cblas_*_scalar.c' -o -name 'cblas_*_driver.c' \) \
    | sort
)

generated_count="$(
  find "$generate_dir" -maxdepth 1 -type f \
    \( -name "*_${target_arch}_generated.c" -o -name "*_${target_arch}_*.S" -o -name "*_${target_arch}_*.s" -o -name "*_${target_arch}_*.asm" \) \
    2>/dev/null | wc -l | tr -d ' '
)"
if [[ "$generated_count" == "0" ]]; then
  if [[ "$compile_only" != true || "${#extra_sources[@]}" -eq 0 ]]; then
    echo "[完成] 标量与 driver 烟测编译通过；generate/ 中暂无 ${target_arch} 产物"
    exit 0
  fi
fi

if [[ "$compile_only" == true ]]; then
  compiler_json="$tmp_dir/compiler_support.json"
  python3 "$compiler_support_script" --json > "$compiler_json"
  compiler_path="$(python3 - "$compiler_json" "$target_arch" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
arch = sys.argv[2]
for compiler in payload.get("compilers", []):
    check = compiler.get("checks", {}).get(arch, {})
    if check.get("supported"):
        print(compiler["path"])
        raise SystemExit(0)
raise SystemExit(1)
PY
)"
  arch_flags="$(python3 - "$compiler_json" "$target_arch" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
arch = sys.argv[2]
for compiler in payload.get("compilers", []):
    check = compiler.get("checks", {}).get(arch, {})
    if check.get("supported"):
        print("|".join(check.get("flags", [])))
        raise SystemExit(0)
raise SystemExit(1)
PY
)"
  common_flags="-std=c11|-O3"
else
  preflight_env="$tmp_dir/preflight.env"
  python3 "$preflight_script" --arch "$target_arch" --env > "$preflight_env"
  set -a
  source "$preflight_env"
  set +a

  if [[ "$status" == "unsupported" ]]; then
    exit 3
  fi
  if [[ "$status" != "ready" ]]; then
    echo "跳过：$skip_reason"
    exit 2
  fi
fi

common_flags_arr=()
arch_flags_arr=()

if [[ -n "$common_flags" ]]; then
  IFS='|' read -r -a common_flags_arr <<<"$common_flags"
fi
if [[ -n "$arch_flags" ]]; then
  IFS='|' read -r -a arch_flags_arr <<<"$arch_flags"
fi

compile_one() {
  local src="$1"
  base="$(basename "$src")"
  out="$tmp_dir/${base%.*}.o"
  log="$tmp_dir/${base%.*}.log"
  compile_cmd=("$compiler_path")
  if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
    compile_cmd+=("${common_flags_arr[@]}")
  fi
  if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
    compile_cmd+=("${arch_flags_arr[@]}")
  fi
  if [[ "$compile_only" == true ]]; then
    compile_cmd+=(-fsyntax-only "$src")
  else
    compile_cmd+=(-c "$src" -o "$out")
  fi
  if ! "${compile_cmd[@]}" >"$log" 2>&1; then
    echo "[错误] 编译失败: $base"
    cat "$log"
    exit 1
  fi
}

if [[ "${#extra_sources[@]}" -gt 0 ]]; then
  echo "[信息] 正在使用 $compiler_path 对指定 ${target_arch} 源码做 compile-only 检查"
  for src in "${extra_sources[@]}"; do
    compile_one "$src"
  done
fi

if [[ "$generated_count" != "0" ]]; then
  echo "[信息] 正在使用 $compiler_path 对 generate/ 中的 ${target_arch} 产物做烟测编译"
  while IFS= read -r src; do
    compile_one "$src"
  done < <(
  find "$generate_dir" -maxdepth 1 -type f \
    \( -name "*_${target_arch}_generated.c" -o -name "*_${target_arch}_*.S" -o -name "*_${target_arch}_*.s" -o -name "*_${target_arch}_*.asm" \) \
    | sort
)
fi

if [[ "$compile_only" == true ]]; then
  echo "[完成] 标量、driver 与 ${target_arch} compile-only 检查通过"
else
  echo "[完成] 标量、driver 与 ${target_arch} 运行时产物烟测编译通过"
fi
