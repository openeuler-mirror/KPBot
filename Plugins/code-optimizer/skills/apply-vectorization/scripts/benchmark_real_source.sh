#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd "$script_dir/.." && pwd)"
cases_dir="$skill_dir/assets/real-source-cases"
default_generate_dir="$cases_dir/generate"
preflight_script="$script_dir/preflight_benchmark_env.py"
request_script="$script_dir/generate_vectorization_request.py"
materialize_script="$script_dir/materialize_vectorization_result.py"

usage() {
  cat <<'EOF'
用法:
  benchmark_real_source.sh --case <case-name> --arch <neon|sve|sme>
  benchmark_real_source.sh --case <case-name> --arch <neon|sve|sme> --response-json <path>
  benchmark_real_source.sh --arch <neon|sve|sme> \
    --source-file <path> --driver-file <path> --target-function <name> \
    --request-json <path> --response-json <path> --output-dir <dir>

说明:
  - --case 模式使用内部 BLAS regression fixtures。
  - 显式源码模式用于外部项目；若不提供 --request-json，则还需 --start-line/--end-line/--data-type 生成 request。
  - 物化后的主 C 源码默认写入 generate/<case>_<arch>_generated.c。
  - 如果 response 带 artifacts，会同时编译同目录下的 .S/.s/.asm 汇编产物。
EOF
}

case_name=""
arch=""
source_file=""
driver_file=""
target_function_arg=""
output_dir=""
request_json=""
response_json=""
materialized_output=""
start_line=""
end_line=""
data_types=()
extra_request_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --case)
      shift
      case_name="${1:-}"
      ;;
    --arch)
      shift
      arch="${1:-}"
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
      target_function_arg="${1:-}"
      ;;
    --output-dir)
      shift
      output_dir="${1:-}"
      ;;
    --request-json)
      shift
      request_json="${1:-}"
      ;;
    --response-json)
      shift
      response_json="${1:-}"
      ;;
    --materialized-output)
      shift
      materialized_output="${1:-}"
      ;;
    --start-line)
      shift
      start_line="${1:-}"
      ;;
    --end-line)
      shift
      end_line="${1:-}"
      ;;
    --data-type)
      shift
      data_types+=("${1:-}")
      ;;
    --loop-variable|--iteration-count|--body-operation|--dependency|--semantic-contract-json|--aliasing|--index-property|--math-mode)
      key="$1"
      shift
      extra_request_args+=("$key" "${1:-}")
      ;;
    --requires-bit-exact|--allows-reassociation)
      extra_request_args+=("$1")
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

if [[ -z "$arch" ]]; then
  echo "[错误] 必须提供 --arch" >&2
  usage >&2
  exit 1
fi

if [[ -n "$case_name" ]]; then
  if [[ -n "$source_file" || -n "$driver_file" || -n "$target_function_arg" ]]; then
    echo "[错误] --case 模式不能同时提供 --source-file/--driver-file/--target-function" >&2
    exit 1
  fi
  source_file="$cases_dir/${case_name}_scalar.c"
  driver_file="$cases_dir/${case_name}_driver.c"
fi

if [[ -z "$case_name" ]]; then
  if [[ -z "$source_file" || -z "$driver_file" || -z "$target_function_arg" ]]; then
    echo "[错误] 显式源码模式必须提供 --source-file、--driver-file 和 --target-function" >&2
    usage >&2
    exit 1
  fi
fi

if [[ ! -f "$source_file" ]]; then
  echo "[错误] 找不到标量源码: $source_file" >&2
  exit 1
fi
if [[ ! -f "$driver_file" ]]; then
  echo "[错误] 找不到 benchmark driver: $driver_file" >&2
  exit 1
fi

if [[ -z "$output_dir" ]]; then
  if [[ -n "$case_name" ]]; then
    output_dir="$default_generate_dir"
  else
    output_dir="$(dirname "$source_file")/generate"
  fi
fi
generate_dir="$output_dir"
mkdir -p "$generate_dir"

case_label="${case_name:-$target_function_arg}"
if [[ -z "$request_json" ]]; then
  request_json="$generate_dir/${case_label}_${arch}_request.json"
fi
if [[ -z "$response_json" ]]; then
  response_json="$generate_dir/${case_label}_${arch}_response.json"
fi
if [[ -z "$materialized_output" ]]; then
  materialized_output="$generate_dir/${case_label}_${arch}_generated.c"
fi
materialize_summary="$generate_dir/${case_label}_${arch}_materialize_summary.json"

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

preflight_env="$tmp_dir/preflight.env"
python3 "$preflight_script" --arch "$arch" --env > "$preflight_env"
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

if [[ -n "$case_name" ]]; then
  python3 "$request_script" \
    --case "$case_name" \
    --arch "$arch" \
    --output "$request_json" >/dev/null
elif [[ ! -f "$request_json" ]]; then
  if [[ -z "$start_line" || -z "$end_line" || "${#data_types[@]}" -eq 0 ]]; then
    echo "[错误] 显式源码模式生成 request 时必须提供 --start-line、--end-line 和至少一个 --data-type" >&2
    exit 1
  fi
  request_cmd=(
    python3 "$request_script"
    --source-file "$source_file"
    --target-function "$target_function_arg"
    --start-line "$start_line"
    --end-line "$end_line"
    --arch "$arch"
    --output "$request_json"
  )
  for data_type in "${data_types[@]}"; do
    request_cmd+=(--data-type "$data_type")
  done
  if [[ "${#extra_request_args[@]}" -gt 0 ]]; then
    request_cmd+=("${extra_request_args[@]}")
  fi
  "${request_cmd[@]}" >/dev/null
fi

if [[ ! -f "$response_json" ]]; then
  echo "[错误] 缺少 response JSON: $response_json" >&2
  exit 1
fi

if ! python3 "$materialize_script" \
  --request-json "$request_json" \
  --response-json "$response_json" \
  --output-source "$materialized_output" >"$materialize_summary"; then
  echo "[错误] 响应 JSON 校验或物化失败"
  exit 1
fi

target_function="$(python3 - "$request_json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(payload["target_function"])
PY
)"

common_flags_arr=()
arch_flags_arr=()
baseline_disable_flags_arr=()
driver_flags_arr=()
link_flags_arr=()

if [[ -n "$common_flags" ]]; then
  IFS='|' read -r -a common_flags_arr <<<"$common_flags"
fi
if [[ -n "$arch_flags" ]]; then
  IFS='|' read -r -a arch_flags_arr <<<"$arch_flags"
fi
if [[ -n "$baseline_disable_flags" ]]; then
  IFS='|' read -r -a baseline_disable_flags_arr <<<"$baseline_disable_flags"
fi
if [[ -n "$driver_flags" ]]; then
  IFS='|' read -r -a driver_flags_arr <<<"$driver_flags"
fi
if [[ -n "$link_flags" ]]; then
  IFS='|' read -r -a link_flags_arr <<<"$link_flags"
fi

baseline_symbol="${target_function}_baseline"
optimized_symbol="${target_function}_${arch}_optimized"
baseline_obj="$tmp_dir/baseline.o"
optimized_obj="$tmp_dir/optimized.o"
driver_obj="$tmp_dir/driver.o"
binary="$tmp_dir/benchmark_real_source"
extra_optimized_objs=()

run_compile() {
  local label="$1"
  local log_file="$2"
  shift 2
  if ! "$@" >"$log_file" 2>&1; then
    echo "[错误] ${label}编译失败"
    cat "$log_file"
    exit 1
  fi
}

baseline_cmd=("$compiler_path")
if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
  baseline_cmd+=("${common_flags_arr[@]}")
fi
if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
  baseline_cmd+=("${arch_flags_arr[@]}")
fi
if [[ "${#baseline_disable_flags_arr[@]}" -gt 0 ]]; then
  baseline_cmd+=("${baseline_disable_flags_arr[@]}")
fi
baseline_cmd+=("-D${target_function}=${baseline_symbol}" -c "$source_file" -o "$baseline_obj")

optimized_cmd=("$compiler_path")
if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
  optimized_cmd+=("${common_flags_arr[@]}")
fi
if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
  optimized_cmd+=("${arch_flags_arr[@]}")
fi
optimized_cmd+=("-D${target_function}=${optimized_symbol}" -c "$materialized_output" -o "$optimized_obj")

artifact_list="$tmp_dir/artifacts.list"
python3 - "$materialize_summary" >"$artifact_list" <<'PY'
import json
import pathlib
import sys

summary = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for output in summary.get("outputs", []):
    path = pathlib.Path(output["path"])
    language = output.get("language", "")
    if language in {"asm", "assembly"} or path.suffix.lower() in {".s", ".asm"}:
        print(path)
PY

while IFS= read -r artifact_src; do
  if [[ -z "$artifact_src" ]]; then
    continue
  fi
  artifact_base="$(basename "$artifact_src")"
  artifact_obj="$tmp_dir/${artifact_base%.*}.o"
  artifact_log="$tmp_dir/${artifact_base%.*}.log"
  artifact_cmd=("$compiler_path")
  if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
    artifact_cmd+=("${common_flags_arr[@]}")
  fi
  if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
    artifact_cmd+=("${arch_flags_arr[@]}")
  fi
  artifact_cmd+=(-c "$artifact_src" -o "$artifact_obj")
  run_compile "汇编 artifact ${artifact_base}" "$artifact_log" "${artifact_cmd[@]}"
  extra_optimized_objs+=("$artifact_obj")
done <"$artifact_list"

driver_cmd=("$compiler_path")
if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
  driver_cmd+=("${common_flags_arr[@]}")
fi
if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
  driver_cmd+=("${arch_flags_arr[@]}")
fi
if [[ "${#driver_flags_arr[@]}" -gt 0 ]]; then
  driver_cmd+=("${driver_flags_arr[@]}")
fi
driver_cmd+=(
  "-DAPPLY_VECTORIZATION_BASELINE_FUNCTION=${baseline_symbol}"
  "-DAPPLY_VECTORIZATION_OPTIMIZED_FUNCTION=${optimized_symbol}"
  -c "$driver_file"
  -o "$driver_obj"
)

link_cmd=("$compiler_path")
if [[ "${#common_flags_arr[@]}" -gt 0 ]]; then
  link_cmd+=("${common_flags_arr[@]}")
fi
if [[ "${#arch_flags_arr[@]}" -gt 0 ]]; then
  link_cmd+=("${arch_flags_arr[@]}")
fi
if [[ "${#link_flags_arr[@]}" -gt 0 ]]; then
  link_cmd+=("${link_flags_arr[@]}")
fi
link_cmd+=("$baseline_obj" "$optimized_obj")
if [[ "${#extra_optimized_objs[@]}" -gt 0 ]]; then
  link_cmd+=("${extra_optimized_objs[@]}")
fi
link_cmd+=("$driver_obj" -o "$binary")

run_compile "标量源码" "$tmp_dir/baseline.log" "${baseline_cmd[@]}"
run_compile "优化后源码" "$tmp_dir/optimized.log" "${optimized_cmd[@]}"
run_compile "benchmark driver" "$tmp_dir/driver.log" "${driver_cmd[@]}"
run_compile "benchmark 链接" "$tmp_dir/link.log" "${link_cmd[@]}"

echo "case=${case_label}"
echo "arch=${arch}"
"$binary"
