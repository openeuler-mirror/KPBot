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
  4. 运行时分派: parse -a <impl> 必须退出码 0 且输出可识别的基准结果行
失败任一即判定 "实现未生效", 退出非零; 静默失败/崩溃/无输出一律不放行。
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
  if [[ -z "$JSON" ]]; then
    echo "提示: 未找到语料，运行时分派检查将使用空输入；若因此失败请用 -j 指定有效语料。" >&2
  fi
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

# 运行时确认：用 parse -a <impl> 跑最小输入。白名单式判定——
# 只有「退出码为 0 且输出含可识别的基准结果行」才 PASS，
# 崩溃、无输出、未知实现措辞、回退 fallback、无法识别的输出一律 FAIL。
echo "--- 运行时分派 (parse -a ${IMPL})"
RC=0
if [[ -n "$JSON" ]]; then
  OUT="$(SIMDJSON_FORCE_IMPLEMENTATION=${IMPL} "${BIN}" -a "${IMPL}" -n 1 -t "${JSON}" 2>&1)" || RC=$?
else
  OUT="$(SIMDJSON_FORCE_IMPLEMENTATION=${IMPL} "${BIN}" -a "${IMPL}" -n 1 -t /dev/null 2>&1)" || RC=$?
fi

print_out_head() { printf '%s\n' "$OUT" | sed -n '1,5p' | sed 's/^/  | /' >&2; }

if [[ $RC -ne 0 ]]; then
  echo "FAIL (运行时退出码非 0: ${RC})"
  print_out_head
  PASS=0
elif [[ -z "${OUT//[[:space:]]/}" ]]; then
  echo "FAIL (运行时无输出)"
  PASS=0
elif printf '%s' "$OUT" | grep -qiE 'unknown implementation|unknown simd|unsupported|not (a )?supported|not recognized|does not exist|fallback'; then
  echo "FAIL (运行时报实现错误/回退)"
  print_out_head
  PASS=0
elif printf '%s' "$OUT" | grep -Eq '^"[^"]*"[[:space:]]+([-+0-9.]|(inf|nan))'; then
  echo "PASS (分派成功: 输出含可识别的基准结果行)"
else
  echo "FAIL (输出无法识别为基准结果，拒绝放行)"
  print_out_head
  PASS=0
fi

if [[ $PASS -eq 1 ]]; then
  echo ""
  echo "=== 核验通过: ${IMPL} 实现已生效 ==="
else
  echo ""
  echo "=== 核验失败: ${IMPL} 实现未生效，先修构建/分派再测性能 ===" >&2
  exit 1
fi
