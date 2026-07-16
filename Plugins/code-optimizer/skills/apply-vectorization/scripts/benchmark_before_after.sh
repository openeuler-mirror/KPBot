#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd "$script_dir/.." && pwd)"
worker_script="$script_dir/benchmark_real_source.sh"
generate_dir="$skill_dir/assets/real-source-cases/generate"

usage() {
  cat <<'EOF'
用法:
  benchmark_before_after.sh
  benchmark_before_after.sh --arch <neon|sve|sme>
  benchmark_before_after.sh --arch <neon|sve|sme> --cases <case1,case2>
  benchmark_before_after.sh --arch <neon|sve|sme> --generate-dir <dir>

说明:
  - 默认扫描 generate/ 中现有的 response JSON 并按架构汇总。
  - --generate-dir 可指向外部源码同级 generate 目录。
  - --cases 只指定 case 名，不带 _<arch>_response.json 后缀。
EOF
}

arch_filter=""
cases_filter=""
source_file=""
driver_file=""
target_function=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch)
      shift
      arch_filter="${1:-}"
      ;;
    --cases)
      shift
      cases_filter="${1:-}"
      ;;
    --generate-dir)
      shift
      generate_dir="${1:-}"
      ;;
    --source-file)
      shift
      source_file="${1:-}"
      ;;
    --driver-file)
      shift
      driver_file="${1:-}"
      ;;
    --target-function)
      shift
      target_function="${1:-}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[错误] 不支持的参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

explicit_source_mode=false
if [[ -n "$source_file" || -n "$driver_file" || -n "$target_function" ]]; then
  explicit_source_mode=true
  if [[ -z "$source_file" || -z "$driver_file" || -z "$target_function" ]]; then
    echo "[错误] 显式源码汇总模式必须同时提供 --source-file、--driver-file 和 --target-function" >&2
    exit 1
  fi
fi

print_header() {
  printf '%-8s | %-14s | %-6s | %-12s | %-12s | %-8s | %-10s | %s\n' \
    "架构" "Case" "状态" "使用前(秒)" "使用后(秒)" "加速比" "性能提升" "正确性/备注"
  printf '%s\n' "---------|----------------|--------|--------------|--------------|----------|------------|------------------------------"
}

parse_field() {
  local key="$1"
  local output="$2"
  printf '%s\n' "$output" | awk -F= -v key="$key" '$1 == key {print $2; exit}'
}

render_result_row() {
  local arch_label="$1"
  local case_label="$2"
  local status_text="$3"
  local output="$4"
  local before after speedup improvement correctness remark note

  before="$(parse_field "使用前(秒)" "$output")"
  after="$(parse_field "使用后(秒)" "$output")"
  speedup="$(parse_field "加速比" "$output")"
  improvement="$(parse_field "性能提升" "$output")"
  correctness="$(parse_field "正确性" "$output")"
  remark="$(parse_field "备注" "$output")"
  note="$correctness"

  if [[ -z "$before" ]]; then
    before="-"
  fi
  if [[ -z "$after" ]]; then
    after="-"
  fi
  if [[ -z "$speedup" ]]; then
    speedup="-"
  fi
  if [[ -z "$improvement" ]]; then
    improvement="-"
  fi
  if [[ -n "$remark" && "$remark" != "checksum ok" && "$remark" != "dot ok" ]]; then
    if [[ -n "$note" ]]; then
      note="${note}/${remark}"
    else
      note="$remark"
    fi
  fi
  if [[ -z "$note" ]]; then
    note="$(printf '%s\n' "$output" | sed -n '1p')"
  fi

  printf '%-8s | %-14s | %-6s | %-12s | %-12s | %-8s | %-10s | %s\n' \
    "$arch_label" "$case_label" "$status_text" "$before" "$after" "$speedup" "$improvement" "$note"
}

run_one() {
  local arch="$1"
  local case_name="$2"
  local output
  local status
  local command_args

  set +e
  command_args=(--arch "$arch" --case "$case_name" --output-dir "$generate_dir")
  if [[ "$explicit_source_mode" == true ]]; then
    command_args=(--arch "$arch" --source-file "$source_file" --driver-file "$driver_file" --target-function "$target_function" --output-dir "$generate_dir")
    command_args+=(--request-json "$generate_dir/${case_name}_${arch}_request.json")
    command_args+=(--response-json "$generate_dir/${case_name}_${arch}_response.json")
  fi
  output="$("$worker_script" "${command_args[@]}" 2>&1)"
  status=$?
  set -e

  if [[ "$status" -eq 0 ]]; then
    render_result_row "$(printf '%s' "$arch" | tr '[:lower:]' '[:upper:]')" "$case_name" "完成" "$output"
    success_count=$((success_count + 1))
    total_count=$((total_count + 1))
    return 0
  fi

  if [[ "$status" -eq 2 || "$status" -eq 3 ]]; then
    render_result_row "$(printf '%s' "$arch" | tr '[:lower:]' '[:upper:]')" "$case_name" "跳过" "$output"
    skip_count=$((skip_count + 1))
    total_count=$((total_count + 1))
    return 0
  fi

  render_result_row "$(printf '%s' "$arch" | tr '[:lower:]' '[:upper:]')" "$case_name" "失败" "$output"
  failure_count=$((failure_count + 1))
  total_count=$((total_count + 1))
  return 0
}

discover_cases() {
  local arch="$1"
  if [[ -n "$cases_filter" ]]; then
    printf '%s\n' "$cases_filter" | tr ',' '\n'
    return 0
  fi
  if [[ ! -d "$generate_dir" ]]; then
    return 0
  fi

  find "$generate_dir" -maxdepth 1 -type f -name "*_${arch}_response.json" \
    | sed -E "s#.*/(.*)_${arch}_response\\.json#\\1#" \
    | sort
}

success_count=0
skip_count=0
failure_count=0
total_count=0

archs=("neon" "sve" "sme")
if [[ -n "$arch_filter" ]]; then
  archs=("$arch_filter")
fi

print_header
for arch in "${archs[@]}"; do
  while IFS= read -r case_name; do
    [[ -z "$case_name" ]] && continue
    run_one "$arch" "$case_name"
  done < <(discover_cases "$arch")
done

printf '\n'
printf '%s\n' "说明:"
printf '%s\n' "- 使用前: 对应 case 的标量源码，编译期禁用自动向量化。"
printf '%s\n' "- 使用后: generate/ 下 response JSON 物化出的候选源码。"
printf '%s\n' "- 若 generate/ 中没有匹配的 response JSON，本脚本不会凭空生成候选结果。"

if [[ "$total_count" -eq 0 ]]; then
  echo "[错误] generate 目录中没有可运行的 case response JSON" >&2
  exit 1
fi
if [[ "$failure_count" -gt 0 ]]; then
  exit 1
fi
if [[ "$success_count" -gt 0 ]]; then
  exit 0
fi
if [[ "$skip_count" -gt 0 ]]; then
  exit 3
fi
exit 1
