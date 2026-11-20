#!/usr/bin/env bash
# verify_hw_path.sh — 核验二进制里 checksum 硬件加速路径是否真的生效
#
# 用法: bash verify_hw_path.sh <binary> [symbol_regex] [detect_fn_regex]
#   binary          待核验的 ELF（须未 strip，或至少有符号表）
#   symbol_regex    加速符号匹配式，默认 'crc32_iscsi|Crc32cIsaL|crc32c_hw'
#   detect_fn_regex 能力探测函数匹配式，默认 'supported|_detect|hwcap'
#
# 背景: "构建成功" 不等于 "优化生效"。编译期宏门控的 intrinsic 路径会因
# 全局 -march 缺 +crc、或特性宏从未 #define 而静默编成常量桩
# (mov w0, wzr; ret)。本脚本做三项核验，任一 FAIL 即判定加速路径未生效。
#
# 只对匹配到的符号做定向反汇编（--start-address/--stop-address），
# 不做全二进制反汇编，因此对数百 MB 的带 debug_info 二进制同样可用。

set -uo pipefail

BIN="${1:-}"
SYM_RE="${2:-crc32_iscsi|Crc32cIsaL|crc32c_hw}"
DET_RE="${3:-supported|_detect|hwcap}"

if [ -z "$BIN" ]; then
    echo "usage: bash verify_hw_path.sh <binary> [symbol_regex] [detect_fn_regex]" >&2
    exit 2
fi
if [ ! -f "$BIN" ]; then
    echo "FAIL: binary not found: $BIN" >&2
    exit 2
fi

NM=$(command -v nm || command -v llvm-nm || true)
OBJDUMP=$(command -v objdump || command -v llvm-objdump || true)
if [ -z "$NM" ] || [ -z "$OBJDUMP" ]; then
    echo "FAIL: need nm and objdump (or llvm- equivalents) in PATH" >&2
    exit 2
fi

FAIL=0
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

echo "binary : $BIN"
echo "md5    : $(md5sum "$BIN" 2>/dev/null | awk '{print $1}')"
echo "size   : $(stat -c %s "$BIN" 2>/dev/null || echo unknown)"
echo

# --- 0. 可符号化前提 -------------------------------------------------------
echo "=== [0] debuginfo / symbols ==="
file "$BIN" | tr ',' '\n' | grep -Ei 'ELF|ARM|aarch64|x86|stripped|debug_info' | sed 's/^/  /'
if file "$BIN" | grep -q 'not stripped'; then
    echo "  OK: not stripped"
else
    echo "  WARN: stripped — 符号核验可能不完整"
fi
echo

# 符号表（原始名，未 demangle）: addr size type name
"$NM" -S "$BIN" 2>/dev/null | awk 'NF==4 && $3 ~ /^[TtWw]$/ {print $1, $2, $4}' > "$TMP.symtab"
if [ ! -s "$TMP.symtab" ]; then
    "$NM" "$BIN" 2>/dev/null | awk 'NF==3 && $2 ~ /^[TtWw]$/ {print $1, "-", $3}' > "$TMP.symtab"
fi

# disasm_fn <name> — 定向反汇编单个函数，输出纯指令行
disasm_fn() {
    local name="$1" row addr size stop
    row=$(awk -v n="$name" '$3==n {print; exit}' "$TMP.symtab")
    [ -z "$row" ] && return 1
    addr=$(printf '%s' "$row" | awk '{print $1}')
    size=$(printf '%s' "$row" | awk '{print $2}')
    if [ "$size" != "-" ] && [ "$size" != "0" ]; then
        stop=$(printf '%x' $(( 0x$addr + 0x$size )))
        "$OBJDUMP" -d --start-address="0x$addr" --stop-address="0x$stop" "$BIN" 2>/dev/null \
            | grep -E '^\s+[0-9a-f]+:' | sed 's/^[[:space:]]*//'
    else
        # 无符号尺寸信息：取该地址起 32 条指令
        "$OBJDUMP" -d --start-address="0x$addr" "$BIN" 2>/dev/null \
            | grep -E '^\s+[0-9a-f]+:' | sed 's/^[[:space:]]*//' | head -32
    fi
}

# --- 1. 加速符号是否存在 ---------------------------------------------------
echo "=== [1] acceleration symbols (nm) ==="
accel=$(awk '{print $3}' "$TMP.symtab" | grep -E "$SYM_RE" | sort -u || true)
n_sym=$(printf '%s\n' "$accel" | grep -c . || true)
if [ "$n_sym" -gt 0 ]; then
    echo "  OK: $n_sym symbol(s) matched /$SYM_RE/"
    printf '%s\n' "$accel" | head -20 | sed 's/^/    /'
    [ "$n_sym" -gt 20 ] && echo "    ... ($((n_sym - 20)) more)"
else
    echo "  FAIL: 0 symbols matched /$SYM_RE/ — 加速代码根本没链进来"
    FAIL=1
fi
echo

# --- 2. 探测函数是否被编成常量桩 -------------------------------------------
echo "=== [2] capability-detect functions not stubbed ==="
det_fns=$(awk '{print $3}' "$TMP.symtab" | grep -E "$DET_RE" \
          | grep -Ei 'crc|checksum|hw|isa|sve|pmull|neon' | sort -u || true)
if [ -z "$det_fns" ]; then
    echo "  WARN: no detect functions matched /$DET_RE/ + crc|checksum|hw|isa|sve|pmull|neon"
    echo "        若目标二进制应有能力探测函数，请调整 detect_fn_regex 后重跑。"
    echo "        静默回退软件实现的典型特征就是探测函数被编成常量桩。"
else
    while IFS= read -r fn; do
        [ -z "$fn" ] && continue
        body=$(disasm_fn "$fn" | head -12)
        n_insn=$(printf '%s\n' "$body" | grep -c . || true)
        if [ "$n_insn" -eq 0 ]; then
            echo "  WARN: $fn 反汇编为空（地址/尺寸信息缺失），需人工核对"
            continue
        fi
        # 常量桩特征: 指令数极少、含 ret、且没有任何分支/加载/调用
        if [ "$n_insn" -le 3 ] && printf '%s\n' "$body" | grep -qE '\bret\b' \
           && ! printf '%s\n' "$body" | grep -qE '\b(bl|b\.|bls|bhs|cbz|cbnz|tbz|tbnz|ldr|ldp|adrp)\b'; then
            echo "  FAIL: $fn 是常量桩（$n_insn 条指令，无分支/加载/调用）"
            printf '%s\n' "$body" | sed 's/^/        /'
            FAIL=1
        else
            echo "  OK: $fn 含真实逻辑（$n_insn 条指令）"
        fi
    done <<< "$det_fns"
fi
echo

# --- 3. 加速符号内是否真的落了硬件指令 --------------------------------------
echo "=== [3] hardware instructions inside acceleration symbols ==="
if [ "$n_sym" -eq 0 ]; then
    echo "  SKIP: 无加速符号可查（见 [1]）"
else
    tot=0
    while IFS= read -r fn; do
        [ -z "$fn" ] && continue
        body=$(disasm_fn "$fn")
        [ -z "$body" ] && { echo "  WARN: $fn 反汇编为空"; continue; }
        c=$(printf '%s\n' "$body" | grep -cE '\bcrc32c[xwhb]\b|\bpmull2?\b' || true)
        n=$(printf '%s\n' "$body" | grep -c . || true)
        tot=$((tot + c))
        printf '  %-46s %5s / %5s insn\n' "$fn" "$c" "$n"
    done <<< "$accel"
    if [ "$tot" -eq 0 ]; then
        echo "  FAIL: 加速符号内没有任何 crc32c*/pmull 指令 — 硬件路径未落地"
        FAIL=1
    else
        echo "  OK: 加速符号内共 $tot 条 crc32c*/pmull 指令"
    fi
fi
echo

# --- 结论 -------------------------------------------------------------------
if [ "$FAIL" -eq 0 ]; then
    echo "RESULT: PASS — 加速符号存在、探测函数非常量桩、硬件指令已落地"
    echo "        仍需火焰图 / CPU-秒 A/B 确认运行时真的走了该路径"
else
    echo "RESULT: FAIL — 硬件加速路径未生效，不要开始性能测量"
    echo "        常见根因: 全局 -march 缺 +crc / 特性宏未 #define /"
    echo "                  只支持最高档 ISA 导致 _supported() 恒 false 而静默回退软件实现"
fi
rm -f "$TMP.symtab"
exit "$FAIL"
