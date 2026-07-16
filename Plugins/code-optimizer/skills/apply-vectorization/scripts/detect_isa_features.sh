#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
用法:
  detect_isa_features.sh --list
  detect_isa_features.sh --json
  detect_isa_features.sh --require <item>

支持的能力项:
  neon
  sve
  sme
  dotprod
  fp16
  bf16
  i8mm

退出码:
  0  成功，且支持当前能力项
  1  参数错误或探测失败
  3  本机不支持当前能力项
EOF
}

mode="list"
format="text"
required_item=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      mode="list"
      ;;
    --json)
      mode="list"
      format="json"
      ;;
    --require)
      mode="require"
      shift
      if [[ $# -eq 0 ]]; then
        echo "[错误] --require 需要能力项名称" >&2
        usage >&2
        exit 1
      fi
      required_item="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
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

os="$(uname -s 2>/dev/null || echo unknown)"
arch="$(uname -m 2>/dev/null || echo unknown)"
source_name="unknown"

neon=0
sve=0
sme=0
dotprod=0
fp16=0
bf16=0
i8mm=0

bool_json() {
  if [[ "$1" -eq 1 ]]; then
    echo true
  else
    echo false
  fi
}

sysctl_flag() {
  local key="$1"
  local value
  value="$(sysctl -n "$key" 2>/dev/null || true)"
  case "$value" in
    1|yes|Yes|true|TRUE)
      echo 1
      ;;
    *)
      echo 0
      ;;
  esac
}

detect_macos() {
  source_name="sysctl"
  neon="$(sysctl_flag hw.optional.neon)"
  if [[ "$neon" -eq 0 ]]; then
    neon="$(sysctl_flag hw.optional.AdvSIMD)"
  fi
  sve="$(sysctl_flag hw.optional.arm.FEAT_SVE)"
  sme="$(sysctl_flag hw.optional.arm.FEAT_SME)"
  dotprod="$(sysctl_flag hw.optional.arm.FEAT_DotProd)"
  fp16="$(sysctl_flag hw.optional.arm.FEAT_FP16)"
  if [[ "$fp16" -eq 0 ]]; then
    fp16="$(sysctl_flag hw.optional.neon_fp16)"
  fi
  bf16="$(sysctl_flag hw.optional.arm.FEAT_BF16)"
  i8mm="$(sysctl_flag hw.optional.arm.FEAT_I8MM)"
}

detect_linux() {
  source_name="lscpu/procfs"
  local data=""
  local lower=""

  if command -v lscpu >/dev/null 2>&1; then
    data+="$(lscpu 2>/dev/null || true)"$'\n'
  fi
  if [[ -r /proc/cpuinfo ]]; then
    data+="$(cat /proc/cpuinfo)"$'\n'
  fi

  lower="$(printf '%s' "$data" | tr '[:upper:]' '[:lower:]')"

  if grep -Eq '(^|[[:space:]:,])(asimd|neon|advsimd)($|[[:space:]:,])' <<<"$lower"; then
    neon=1
  fi
  if grep -Eq '(^|[[:space:]:,])sve($|[[:space:]:,])' <<<"$lower"; then
    sve=1
  fi
  if grep -Eq '(^|[[:space:]:,])sme($|[[:space:]:,])' <<<"$lower"; then
    sme=1
  fi
  if grep -Eq '(^|[[:space:]:,])dotprod($|[[:space:]:,])' <<<"$lower"; then
    dotprod=1
  fi
  if grep -Eq '(^|[[:space:]:,])(fp16|fphp|asimdhp)($|[[:space:]:,])' <<<"$lower"; then
    fp16=1
  fi
  if grep -Eq '(^|[[:space:]:,])bf16($|[[:space:]:,])' <<<"$lower"; then
    bf16=1
  fi
  if grep -Eq '(^|[[:space:]:,])i8mm($|[[:space:]:,])' <<<"$lower"; then
    i8mm=1
  fi
}

detect_fallback() {
  if [[ "$arch" =~ ^(arm64|aarch64)$ ]]; then
    source_name="arch-default"
    neon=1
  fi
}

detect_native_arm64() {
  local arm64_flag=""
  arm64_flag="$(sysctl -n hw.optional.arm64 2>/dev/null || true)"
  if [[ "$arm64_flag" == "1" ]]; then
    return 0
  fi
  if [[ "$arch" =~ ^(arm64|aarch64)$ ]]; then
    return 0
  fi
  return 1
}

case "$os" in
  Darwin)
    detect_macos
    ;;
  Linux)
    detect_linux
    ;;
  *)
    detect_fallback
    ;;
esac

if [[ "$source_name" == "unknown" ]]; then
  detect_fallback
fi

available=()
for capability in neon sve sme dotprod fp16 bf16 i8mm; do
  if [[ "${!capability}" -eq 1 ]]; then
    available+=("$capability")
  fi
done

print_text() {
  cat <<EOF
os=$os
arch=$arch
source=$source_name
native_arm64=$(detect_native_arm64 && echo true || echo false)
available=$(IFS=,; echo "${available[*]}")
neon=$(bool_json "$neon")
sve=$(bool_json "$sve")
sme=$(bool_json "$sme")
dotprod=$(bool_json "$dotprod")
fp16=$(bool_json "$fp16")
bf16=$(bool_json "$bf16")
i8mm=$(bool_json "$i8mm")
EOF
}

print_json() {
  local native_arm64_json
  if detect_native_arm64; then
    native_arm64_json=true
  else
    native_arm64_json=false
  fi
  cat <<EOF
{"os":"$os","arch":"$arch","source":"$source_name","native_arm64":$native_arm64_json,"available":[$(for item in "${available[@]}"; do printf '"%s",' "$item"; done | sed 's/,$//')],"capabilities":{"neon":$(bool_json "$neon"),"sve":$(bool_json "$sve"),"sme":$(bool_json "$sme"),"dotprod":$(bool_json "$dotprod"),"fp16":$(bool_json "$fp16"),"bf16":$(bool_json "$bf16"),"i8mm":$(bool_json "$i8mm")}}
EOF
}

supports_item() {
  case "$1" in
    neon)
      [[ "$neon" -eq 1 ]]
      ;;
    sve)
      [[ "$sve" -eq 1 ]]
      ;;
    sme)
      [[ "$sme" -eq 1 ]]
      ;;
    dotprod)
      [[ "$dotprod" -eq 1 ]]
      ;;
    fp16)
      [[ "$fp16" -eq 1 ]]
      ;;
    bf16)
      [[ "$bf16" -eq 1 ]]
      ;;
    i8mm)
      [[ "$i8mm" -eq 1 ]]
      ;;
    *)
      echo "[错误] 不支持的能力项: $1" >&2
      exit 1
      ;;
  esac
}

if [[ "$mode" == "require" ]]; then
  if supports_item "$required_item"; then
    echo "[完成] 本机支持当前能力项: $required_item"
    exit 0
  fi
  echo "[退出] 本机不支持当前能力项: $required_item"
  print_text
  exit 3
fi

if [[ "$format" == "json" ]]; then
  print_json
else
  print_text
fi
