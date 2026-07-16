#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd "$script_dir/.." && pwd)"
preflight_script="$script_dir/preflight_benchmark_env.py"
target_arch="${1:-${APPLY_VECTORIZATION_TARGET_ARCH:-neon}}"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

case "$target_arch" in
  neon)
    arch_label="NEON"
    src="$skill_dir/assets/fixtures/benchmark_add.c"
    ;;
  sve)
    arch_label="SVE"
    src="$skill_dir/assets/fixtures/benchmark_add_sve.c"
    ;;
  sme)
    arch_label="SME"
    src="$skill_dir/assets/fixtures/benchmark_add_sme.c"
    ;;
  *)
    echo "[错误] 不支持的目标架构: $target_arch"
    exit 1
    ;;
esac

preflight_env="$tmp_dir/preflight.env"
python3 "$preflight_script" --arch "$target_arch" --env > "$preflight_env"
set -a
source "$preflight_env"
set +a

if [[ "$status" == "unsupported" ]]; then
  echo "跳过：$skip_reason"
  exit 3
fi
if [[ "$status" != "ready" ]]; then
  echo "跳过：$skip_reason"
  exit 2
fi

common_flags_arr=()
arch_flags_arr=()
baseline_disable_flags_arr=()

if [[ -n "$common_flags" ]]; then
  IFS='|' read -r -a common_flags_arr <<<"$common_flags"
fi
if [[ -n "$arch_flags" ]]; then
  IFS='|' read -r -a arch_flags_arr <<<"$arch_flags"
fi
if [[ -n "$baseline_disable_flags" ]]; then
  IFS='|' read -r -a baseline_disable_flags_arr <<<"$baseline_disable_flags"
fi

bin="$tmp_dir/benchmark_${target_arch}"
log="$tmp_dir/benchmark_${target_arch}.log"
compile_cmd=("$compiler_path")
if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
  compile_cmd+=("${common_flags_arr[@]}")
fi
if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
  compile_cmd+=("${arch_flags_arr[@]}")
fi
if [[ "${#baseline_disable_flags_arr[@]}" -gt 0 ]]; then
  compile_cmd+=("${baseline_disable_flags_arr[@]}")
fi
compile_cmd+=("$src" -o "$bin")

if ! "${compile_cmd[@]}" >"$log" 2>&1; then
  echo "[错误] $arch_label 基准编译失败"
  cat "$log"
  exit 1
fi

echo "对比口径=使用前(标量基线, 编译期禁用自动向量化) vs 使用后(apply-vectorization 夹具中的 ${arch_label} 显式向量化实现)"
"$bin"
