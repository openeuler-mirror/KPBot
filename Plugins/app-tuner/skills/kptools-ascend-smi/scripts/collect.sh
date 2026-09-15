#!/bin/bash
# collect.sh — npu-smi 只读采集统一入口
# stdout: 原始命令输出 | stderr: 诊断 | 退出码: 0=成功 2=环境不可用 3=参数错误 4=命令失败
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIRMWARE_URL="https://www.hiascend.com/hdk/firmware"

# ---------- 帮助 ----------
help() {
    cat <<'EOF'
ascend-smi collect — Read-only npu-smi collection wrapper

Usage:
  collect.sh <subcommand> [options]

Subcommands:
  info                                  Basic info + device list (npu-smi info, info -l)
  usages        [-i <id>]               Aicore/Aivector usage (info -t usages); all devices if no -i
  watch         -i <id> [-c <chip>] [-d <sec>] [-s <types>] [-T <sec>]
                                        Polling monitor (info watch); types: p t a i c m b u;
                                        -T timeout (default 600s, 0 = infinite)
  health        [-i <id>]               Health status (info -t health); all devices if no -i
  topology                              CPU/NPU affinity + topology (info -t topo, fallback -m+lspci)
  hccs          -i <id> -c <chip>       HCCS link info (info -t hccs, fallback -t board)
  hccs-bw       -i <id> -c <chip> [-time <ms>]
                                        HCCS link bandwidth (info -t hccs-bw)
  version                               npu-smi + CANN + torch_npu versions
  permission                            /dev/davinci* permissions + group membership
  all                                   Run info, usages, health, topology, permission, version

Global options (before subcommand):
  --tool <path>                         Explicit npu-smi path

Exit codes: 0=ok 2=environment unavailable 3=usage error 4=command failed
EOF
}

# ---------- 全局选项（--tool/--help 先于工具定位解析） ----------
TOOL_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --tool)
            if [ $# -lt 2 ]; then
                echo "--tool requires a path argument" >&2
                exit 3
            fi
            TOOL_OVERRIDE="$2"; shift 2 ;;
        --help|-h) help; exit 0 ;;
        *) break ;;
    esac
done

# ---------- 工具定位 ----------
find_npu_smi() {
    if [ -n "$TOOL_OVERRIDE" ] && [ -x "$TOOL_OVERRIDE" ]; then
        echo "$TOOL_OVERRIDE"; return 0
    fi
    if command -v npu-smi &>/dev/null; then
        command -v npu-smi; return 0
    fi
    local p
    for p in /usr/local/sbin/npu-smi /usr/local/bin/npu-smi; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

NPU_SMI_BIN="$(find_npu_smi)" || {
    echo "collect: npu-smi not found (PATH, /usr/local/sbin, /usr/local/bin)." >&2
    echo "  npu-smi ships with the Ascend NPU driver, install from: ${FIRMWARE_URL}" >&2
    exit 2
}

# ---------- 通用执行 ----------
run_cmd() { # $@=命令
    local banner="$*"
    echo "=== ${banner} ==="
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ $rc -ne 0 ]; then
        echo "$out"
        echo "--- (rc=${rc}) ---"
        FAILED=$((FAILED + 1))
    else
        echo "$out"
        OK_COUNT=$((OK_COUNT + 1))
    fi
    return $rc
}

run_cmd_tol() { # $1=timeout_sec 其余=命令
    local tmo="$1"; shift
    echo "=== $* (timeout ${tmo}s) ==="
    local out rc
    out="$(run_with_timeout "$tmo" "$@" 2>&1)"; rc=$?
    if [ $rc -eq 124 ]; then
        echo "(timed out after ${tmo}s)"
        FAILED=$((FAILED + 1))
    elif [ $rc -ne 0 ]; then
        echo "$out"
        echo "--- (rc=${rc}) ---"
        FAILED=$((FAILED + 1))
    else
        echo "$out"
        OK_COUNT=$((OK_COUNT + 1))
    fi
    return $rc
}

OK_COUNT=0
FAILED=0

# timeout 不可用时直接执行（放弃超时保护）
run_with_timeout() { # $1=timeout_sec 其余=命令
    local tmo="$1"; shift
    if command -v timeout &>/dev/null; then
        timeout "$tmo" "$@"
    else
        "$@"
    fi
}

run_with_timeout_int() { # 同上，但发 SIGINT（模拟 Ctrl+C，让 watch 类命令刷出缓冲数据）
    local tmo="$1"; shift
    if command -v timeout &>/dev/null; then
        timeout -s INT "$tmo" "$@"
    else
        "$@"
    fi
}

# 选项缺值防护：$1=选项名 $2=剩余参数个数（避免 set -u 下 unbound variable 以 rc=1 退出）
req_val() {
    if [ "$2" -lt 2 ]; then
        echo "$1: requires a value" >&2
        exit 3
    fi
}

# ---------- 设备 ID 枚举 ----------
list_npu_ids() {
    # npu-smi info -l 输出含 NPU ID 行（新格式 25.x，设备号可能稀疏如容器分卡）、
    # NPU<n> 标记（老格式）或 Total Count 行（老格式，假设设备号 0~count-1 连续）
    local out
    out="$("$NPU_SMI_BIN" info -l 2>/dev/null)" || return 1
    local ids
    ids=$(echo "$out" | grep -oE 'NPU ID[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' | sort -nu)
    [ -n "$ids" ] && { echo "$ids"; return 0; }
    ids=$(echo "$out" | grep -oE 'NPU[0-9]+' | grep -oE '[0-9]+' | sort -nu)
    [ -n "$ids" ] && { echo "$ids"; return 0; }
    local count
    count=$(echo "$out" | grep -oiE 'Total[[:space:]]+count[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$')
    if [ -n "$count" ] && [ "$count" -gt 0 ] && [ "$count" -le 64 ]; then
        seq 0 $((count - 1)); return 0
    fi
    return 1
}

# ---------- 子命令 ----------
cmd_info() {
    run_cmd "$NPU_SMI_BIN" info
    run_cmd "$NPU_SMI_BIN" info -l
}

cmd_usages() {
    local dev=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; dev="$2"; shift 2 ;;
            *) echo "usages: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -n "$dev" ]; then
        if ! [[ "$dev" =~ ^[0-9]+$ ]]; then
            echo "usages: -i must be a numeric NPU ID" >&2; exit 3
        fi
        run_cmd "$NPU_SMI_BIN" info -t usages -i "$dev" || \
            echo "usages: NPU ${dev} query failed — run 'collect.sh info' to list valid NPU IDs (IDs may be sparse, e.g. containers)" >&2
    else
        # 枚举失败不中断（all 场景需继续执行后续子命令），计入 FAILED 由最终退出码体现
        local ids
        ids="$(list_npu_ids)" || { echo "usages: cannot enumerate NPU IDs" >&2; FAILED=$((FAILED + 1)); return 0; }
        local d first=1
        for d in $ids; do
            [ $first -eq 0 ] && echo ""
            first=0
            run_cmd "$NPU_SMI_BIN" info -t usages -i "$d"
        done
    fi
}

cmd_health() {
    local dev=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; dev="$2"; shift 2 ;;
            *) echo "health: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -n "$dev" ]; then
        if ! [[ "$dev" =~ ^[0-9]+$ ]]; then
            echo "health: -i must be a numeric NPU ID" >&2; exit 3
        fi
        run_cmd "$NPU_SMI_BIN" info -t health -i "$dev" || \
            echo "health: NPU ${dev} query failed — run 'collect.sh info' to list valid NPU IDs (IDs may be sparse, e.g. containers)" >&2
    else
        # 枚举失败不中断（all 场景需继续执行后续子命令），计入 FAILED 由最终退出码体现
        local ids
        ids="$(list_npu_ids)" || { echo "health: cannot enumerate NPU IDs" >&2; FAILED=$((FAILED + 1)); return 0; }
        local d first=1
        for d in $ids; do
            [ $first -eq 0 ] && echo ""
            first=0
            run_cmd "$NPU_SMI_BIN" info -t health -i "$d"
        done
    fi
}

cmd_watch() {
    local dev="" chip="" delay=1 types="ptain" tmo=600
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; dev="$2"; shift 2 ;;
            -c) req_val "$1" "$#"; chip="$2"; shift 2 ;;
            -d) req_val "$1" "$#"; delay="$2"; shift 2 ;;
            -s) req_val "$1" "$#"; types="$2"; shift 2 ;;
            -T) req_val "$1" "$#"; tmo="$2"; shift 2 ;;
            *) echo "watch: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -z "$dev" ]; then
        echo "watch: -i <device_id> is required (see 'collect.sh info' for NPU IDs)" >&2
        exit 3
    fi
    if ! [[ "$dev" =~ ^[0-9]+$ ]]; then
        echo "watch: -i must be a numeric NPU ID" >&2
        exit 3
    fi
    if [ -n "$chip" ] && ! [[ "$chip" =~ ^[0-9]+$ ]]; then
        echo "watch: -c must be a numeric chip ID" >&2
        exit 3
    fi
    if ! echo "$types" | grep -qE '^[ptaincmbu]+$'; then
        echo "watch: -s must combine letters from p t a i c m b u" >&2
        exit 3
    fi
    if ! [[ "$delay" =~ ^[0-9]+$ ]] || [ "$delay" -lt 1 ] || [ "$delay" -gt 100 ]; then
        echo "watch: -d must be 1~100 seconds" >&2
        exit 3
    fi
    if ! [[ "$tmo" =~ ^[0-9]+$ ]] || [ "$tmo" -gt 86400 ]; then
        echo "watch: -T must be 0~86400 seconds (0 = no timeout)" >&2
        exit 3
    fi
    # watch 为无限轮询，必须超时保护（默认 600s；SIGINT 模拟 Ctrl+C 使 npu-smi 刷出已采数据）
    echo "### watch runs for ${tmo}s max (Ctrl+C to stop earlier); interval ${delay}s" >&2
    local args=("$NPU_SMI_BIN" info watch -i "$dev" -d "$delay" -s "$types")
    [ -n "$chip" ] && args=("$NPU_SMI_BIN" info watch -i "$dev" -c "$chip" -d "$delay" -s "$types")
    # 输出实时流式打印（不缓冲至命令结束），长时间轮询期间用户可见增量数据
    local rc
    echo "=== ${args[*]} ==="
    if [ "$tmo" -eq 0 ]; then
        "${args[@]}" 2>&1
        rc=$?
    else
        run_with_timeout_int "$tmo" "${args[@]}" 2>&1
        rc=$?
    fi
    if [ $rc -eq 124 ] || [ $rc -eq 130 ]; then
        # 124=超时停止 130=Ctrl+C：均为 watch 的正常结束方式
        OK_COUNT=$((OK_COUNT + 1))
    elif [ $rc -ne 0 ]; then
        echo "--- (rc=${rc}) ---"
        echo "watch: NPU ${dev} query failed — run 'collect.sh info' to list valid NPU IDs" >&2
        FAILED=$((FAILED + 1))
    else
        OK_COUNT=$((OK_COUNT + 1))
    fi
}

cmd_topology() {
    local out rc
    out="$("$NPU_SMI_BIN" info -t topo 2>&1)"; rc=$?
    # 部分设备/驱动对不支持的 -t topo 返回 rc=0 + "not support" 文案，也需降级
    if [ $rc -eq 0 ] && ! echo "$out" | grep -qiE 'not[[:space:]]support|unsupport'; then
        echo "=== npu-smi info -t topo ==="
        echo "$out"
        OK_COUNT=$((OK_COUNT + 1))
        return 0
    fi
    # 降级：旧驱动无 -t topo，或本设备不支持 topo 查询
    echo "### -t topo unavailable (rc=${rc} / unsupported), falling back to -m + lspci" >&2
    run_cmd "$NPU_SMI_BIN" info -m
    if command -v lspci &>/dev/null; then
        local lout lrc
        echo "=== lspci -d 19e5: -v (timeout 30s) ==="
        lout="$(run_with_timeout 30 lspci -d 19e5: -v 2>&1)"; lrc=$?
        # 过滤容器/受限权限环境下 lspci 的已知噪声行，不影响设备信息本身
        lout="$(echo "$lout" | grep -v '^lspci: Unable to load libkmod resources')"
        echo "$lout"
        if [ $lrc -eq 124 ]; then
            echo "(timed out after 30s)"
            FAILED=$((FAILED + 1))
        elif [ $lrc -ne 0 ]; then
            echo "--- (rc=${lrc}) ---"
            FAILED=$((FAILED + 1))
        else
            OK_COUNT=$((OK_COUNT + 1))
        fi
    else
        echo "### lspci not available, PCIe scan skipped" >&2
    fi
}

cmd_hccs() {
    local dev chip
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; dev="$2"; shift 2 ;;
            -c) req_val "$1" "$#"; chip="$2"; shift 2 ;;
            *) echo "hccs: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -z "$dev" ] || [ -z "$chip" ]; then
        echo "hccs: requires -i <device_id> -c <chip_id> (see 'collect.sh info' / npu-smi info -m)" >&2
        exit 3
    fi
    if ! [[ "$dev" =~ ^[0-9]+$ ]] || ! [[ "$chip" =~ ^[0-9]+$ ]]; then
        echo "hccs: -i and -c must be numeric IDs" >&2
        exit 3
    fi
    local out rc
    out="$("$NPU_SMI_BIN" info -t hccs -i "$dev" -c "$chip" 2>&1)"; rc=$?
    if [ $rc -eq 0 ]; then
        echo "=== npu-smi info -t hccs -i ${dev} -c ${chip} ==="
        echo "$out"
        OK_COUNT=$((OK_COUNT + 1))
        return 0
    fi
    # 参数类错误（设备/芯片号不存在）不降级，直接报错并提示有效 ID 查询方式
    if echo "$out" | grep -qiE 'invalid (card|chip) id|error parameter'; then
        echo "=== npu-smi info -t hccs -i ${dev} -c ${chip} ==="
        echo "$out"
        echo "--- (rc=${rc}) ---"
        echo "hccs: NPU ${dev} / chip ${chip} query failed — run 'collect.sh info' / 'collect.sh info -m' for valid IDs" >&2
        FAILED=$((FAILED + 1))
        return 0
    fi
    echo "### -t hccs unavailable (rc=${rc}), falling back to -t board" >&2
    run_cmd "$NPU_SMI_BIN" info -t board
}

cmd_hccs_bw() {
    local dev chip tmo=1000
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; dev="$2"; shift 2 ;;
            -c) req_val "$1" "$#"; chip="$2"; shift 2 ;;
            -time) req_val "$1" "$#"; tmo="$2"; shift 2 ;;
            *) echo "hccs-bw: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -z "$dev" ] || [ -z "$chip" ]; then
        echo "hccs-bw: requires -i <device_id> -c <chip_id>" >&2
        exit 3
    fi
    if ! [[ "$dev" =~ ^[0-9]+$ ]] || ! [[ "$chip" =~ ^[0-9]+$ ]]; then
        echo "hccs-bw: -i and -c must be numeric IDs" >&2
        exit 3
    fi
    if ! [[ "$tmo" =~ ^[0-9]+$ ]] || [ "$tmo" -lt 1 ] || [ "$tmo" -gt 1000 ]; then
        echo "hccs-bw: -time must be 1~1000 ms" >&2
        exit 3
    fi
    run_cmd_tol 60 "$NPU_SMI_BIN" info -t hccs-bw -i "$dev" -c "$chip" -time "$tmo" || \
        echo "hccs-bw: NPU ${dev} / chip ${chip} query failed — run 'collect.sh info' for valid IDs" >&2
}

cmd_version() {
    run_cmd "$NPU_SMI_BIN" -v
    if [ -r /usr/local/Ascend/ascend-toolkit/latest/version.cfg ]; then
        echo ""
        echo "=== cat /usr/local/Ascend/ascend-toolkit/latest/version.cfg ==="
        cat /usr/local/Ascend/ascend-toolkit/latest/version.cfg
        OK_COUNT=$((OK_COUNT + 1))
    else
        echo "### CANN version.cfg not found (CANN not installed or non-standard path) — skipped" >&2
    fi
    echo ""
    echo "=== pip show torch_npu ==="
    if pip show torch_npu 2>/dev/null; then
        OK_COUNT=$((OK_COUNT + 1))
    else
        echo "(torch_npu not installed in current python env — skipped)"
    fi
}

cmd_permission() {
    echo "=== ls -la /dev/davinci* ==="
    if ls -la /dev/davinci* 2>/dev/null; then
        OK_COUNT=$((OK_COUNT + 1))
    else
        echo "(no /dev/davinci* nodes)"
        FAILED=$((FAILED + 1))
    fi
    echo ""
    echo "### group membership — runtime group (e.g. HwHiAiUser) grants NPU access; compare with /dev/davinci* ownership above" >&2
    run_cmd id -Gn
}

cmd_all() {
    cmd_info; echo ""
    cmd_usages; echo ""
    cmd_health; echo ""
    cmd_topology; echo ""
    cmd_permission; echo ""
    cmd_version
}

# ---------- 入口 ----------
[ $# -eq 0 ] && { help; exit 0; }
case "$1" in
    info)       shift; cmd_info ;;
    usages)     shift; cmd_usages "$@" ;;
    watch)      shift; cmd_watch "$@" ;;
    health)     shift; cmd_health "$@" ;;
    topology)   shift; cmd_topology ;;
    hccs)       shift; cmd_hccs "$@" ;;
    hccs-bw)    shift; cmd_hccs_bw "$@" ;;
    version)    shift; cmd_version ;;
    permission) shift; cmd_permission ;;
    all)        shift; cmd_all ;;
    *) echo "Unknown subcommand: $1 (see --help)" >&2; exit 3 ;;
esac

[ "$FAILED" -gt 0 ] && exit 4
exit 0
