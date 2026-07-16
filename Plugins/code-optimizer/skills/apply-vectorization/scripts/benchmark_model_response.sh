#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
worker_script="$script_dir/benchmark_real_source.sh"

usage() {
  cat <<'EOF'
用法:
  benchmark_model_response.sh --response-json <path>
  benchmark_model_response.sh --case <case-name> --arch <neon|sve|sme> --response-json <path>

说明:
  - 默认 case 和默认 arch 使用通用占位值。
  - 脚本不做参考性能门槛比较，只运行统一 benchmark 并输出正确性和性能结果。
EOF
}

case_name="default_case"
arch="neon"
response_json=""

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
    --response-json)
      shift
      response_json="${1:-}"
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

if [[ -z "$response_json" ]]; then
  echo "[错误] 必须提供 --response-json" >&2
  usage >&2
  exit 1
fi

echo "候选响应=${response_json}"
"$worker_script" --case "$case_name" --arch "$arch" --response-json "$response_json"
