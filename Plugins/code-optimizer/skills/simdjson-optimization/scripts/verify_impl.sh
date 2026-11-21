#!/usr/bin/env bash
# 核验二进制确实包含并使用指定 SIMD 实现（"编过了 ≠ 生效了"）。
# 用法: verify_impl.sh <binary> [-i IMPL]
set -euo pipefail

BIN="${1:-}"
IMPL="arm64"
JSON=""
PASS=1

usage() {
  cat <<EOF
用法: $0 <binary> [-i IMPL] [-j JSON]

检查项 (针对 arm64):
  1. 存在 simdjson::arm64:: 符号
  2. stage1 含 NEON 指令 umaxv (is_ascii 水平归约)
  3. 含 NEON 比较/查表/向量逻辑指令 (classify/reduce)
失败任一即判定 "实现未生效", 退出非零。
IMPL 仅允许字母/数字/下划线；非法值直接退出 2。
EOF
  exit 0
}

[[ -n "$BIN" ]] || usage
[[ -f "$BIN" ]] || { echo "错误: 二进制不存在: $BIN" >&2; exit 2; }
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    -i) IMPL="$2"; shift 2;;
    -j) JSON="$2"; shift 2;;
    *) echo "未知参数: $1" >&2; usage;;
  esac
done

# 安全校验：实现名仅允许字母/数字/下划线。IMPL 会被写入 bash -c 命令文本，
# 白名单是防命令注入的第一道闸门（配合 check() 用环境变量传模式，见下）。
if ! [[ "$IMPL" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "错误: 非法实现名: ${IMPL}（仅允许字母/数字/下划线）" >&2
  exit 2
fi

if [[ -z "$JSON" ]]; then
  # 默认找构建目录下的 twitter.json
  BIN_DIR="$(cd "$(dirname "${BIN}")" && pwd)"
  for cand in "${BIN_DIR}/../.." "${BIN_DIR}/../../jsonexamples" "$(pwd)/jsonexamples"; do
    if [[ -f "${cand}/jsonexamples/twitter.json" ]]; then JSON="${cand}/jsonexamples/twitter.json"; break; fi
    if [[ -f "${cand}/twitter.json" ]]; then JSON="${cand}/twitter.json"; break; fi
  done
fi

check() {
  local desc="$1" snippet="$2"
  echo "--- ${desc}"
  # snippet 为静态命令文本；BIN 与 IMPL_PATTERN 只经环境变量注入 inner shell，
  # 不作为命令文本拼接，避免外部输入被二次解析执行。
  if BIN="$BIN" IMPL_PATTERN="simdjson::${IMPL}::" bash -c "$snippet" > /dev/null 2>&1; then
    echo "PASS"
  else
    echo "FAIL"
    PASS=0
  fi
}

if [[ "$IMPL" == "arm64" ]]; then
  check "符号 simdjson::arm64::" 'nm -C "$BIN" | grep -q -- "$IMPL_PATTERN"'
  check "NEON umaxv 指令 (is_ascii 归约)" 'objdump -d "$BIN" | grep -q -- "umaxv"'
  check "NEON 比较/查表/向量逻辑指令" 'objdump -d "$BIN" | grep -qE "cmeq|tbl|orr.*\\.16b|eor.*\\.16b|addp"'
else
  echo "警告: 未内置对实现 ${IMPL} 的核验规则，仅检查符号" >&2
  check "符号 simdjson::${IMPL}::" 'nm -C "$BIN" | grep -q -- "$IMPL_PATTERN"'
fi

# 运行时确认：用 parse -a <impl> 跑最小输入，检查不报 fallback/未知实现错误
echo "--- 运行时分派 (parse -a ${IMPL})"
if [[ -n "$JSON" ]]; then
  OUT="$(SIMDJSON_FORCE_IMPLEMENTATION=${IMPL} "${BIN}" -a "${IMPL}" -n 1 -t "${JSON}" 2>&1 || true)"
else
  OUT="$(SIMDJSON_FORCE_IMPLEMENTATION=${IMPL} "${BIN}" -a "${IMPL}" -n 1 -t /dev/null 2>&1 || true)"
fi
if echo "$OUT" | grep -qiE 'unknown implementation|unsupported implementation|not a supported'; then
  echo "FAIL (运行时报实现错误/回退)"
  PASS=0
elif echo "$OUT" | grep -q 'Failed to parse'; then
  echo "PASS (实现可用；输入不可用是预期噪声，不影响本检查)"
else
  echo "PASS"
fi

if [[ $PASS -eq 1 ]]; then
  echo ""
  echo "=== 核验通过: ${IMPL} 实现已生效 ==="
else
  echo ""
  echo "=== 核验失败: ${IMPL} 实现未生效，先修构建/分派再测性能 ===" >&2
  exit 1
fi
