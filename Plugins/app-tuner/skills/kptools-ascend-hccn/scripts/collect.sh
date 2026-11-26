#!/bin/bash
# collect.sh — hccn_tool 采集统一入口（只读查询 + 显式确认的 RoCE 测试）
# stdout: 原始命令输出 | stderr: 诊断 | 退出码: 0=成功 2=环境不可用 3=参数错误 4=命令失败
set -uo pipefail

FIRMWARE_URL="https://www.hiascend.com/hdk/firmware"
MAX_CHIP=${HCCN_MAX_CHIP:-7}   # 芯片遍历上限（默认 0~7；16 芯片超节点/Box 用 HCCN_MAX_CHIP=15 扩展）
if ! [[ "$MAX_CHIP" =~ ^[0-9]+$ ]] || [ "$MAX_CHIP" -gt 63 ]; then
    echo "collect: HCCN_MAX_CHIP must be an integer 0~63 (got: ${MAX_CHIP})" >&2
    exit 3
fi

# ---------- 帮助 ----------
help() {
    cat <<'EOF'
ascend-hccn collect — hccn_tool collection wrapper (read-only + explicit RoCE test)

Usage:
  collect.sh <subcommand> [options]

Subcommands:
  ip [-i <chip>]              RoCE IP/netmask (-ip -g); chips 0-7 if no -i
  link [-i <chip>]            Port link status (-link -g)
  stat [-i <chip>]            RX/TX packet and error counters (-stat -g)
  status [-i <chip>]          Port status info (-status -g)
  bandwidth [-i <chip>] [-t <ms>]
                              Realtime bandwidth (100-10000ms window)
  net-health [-i <chip>]      Network health vs configured detection IP
  config [-i <chip>]          RoCE config bundle (gateway/mtu/speed/eth_rate;
                              unsupported items or no-preset gateway are
                              reported as expected, not counted as failure)
  test -m <mode> [-i <chip>] [-s <bytes>] [-D <sec>] [-b] [--timeout <sec>] [address] --force
                              RoCE bandwidth/latency test (ib_read_bw|ib_write_bw|
                              ib_send_bw|ib_read_lat|ib_write_lat|ib_send_lat);
                              --force required; no address = server mode;
                              --timeout default 60s, auto-scaled to -D+30s when -D given
  test-reset [-i <chip>]      Kill device-side roce_test processes (-roce_test reset)
  all                         Run ip, link, stat, status, config (read-only)

Global options (before subcommand):
  --tool <path>               Explicit hccn_tool path

Chip semantics: -i is the chip Phy-ID (0-15 depending on product); default sweep
  auto-detects chips via 'npu-smi info -m' (fallback: 0..MAX_CHIP blind sweep);
  extend the sweep via HCCN_MAX_CHIP (e.g. HCCN_MAX_CHIP=15 for 16-chip supernodes).

Exit codes: 0=ok 2=environment unavailable 3=usage error (incl. missing --force) 4=command failed
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
find_hccn_tool() {
    if [ -n "$TOOL_OVERRIDE" ] && [ -x "$TOOL_OVERRIDE" ]; then
        echo "$TOOL_OVERRIDE"; return 0
    fi
    if command -v hccn_tool &>/dev/null; then
        command -v hccn_tool; return 0
    fi
    local p
    for p in /usr/bin/hccn_tool /usr/local/Ascend/driver/tools/hccn_tool; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

HCCN_BIN="$(find_hccn_tool)" || {
    echo "collect: hccn_tool not found (PATH, /usr/bin, /usr/local/Ascend/driver/tools)." >&2
    echo "  hccn_tool ships with the Ascend NPU driver, install from: ${FIRMWARE_URL}" >&2
    echo "  In container: mount host /usr/bin/hccn_tool into the container." >&2
    exit 2
}

# ---------- 通用执行 ----------
OK_COUNT=0
FAILED=0
BENIGN_PATTERN=""   # 非空时：命令失败但输出匹配该 pattern → 属预期状态（不计 FAILED）
LAST_BENIGN=false   # run_cmd 最近一次失败是否为预期状态（供 run_chips 的 stderr 诊断区分）

# 选项缺值防护：$1=选项名 $2=剩余参数个数（避免 set -u 下 unbound variable 以 rc=1 退出）
req_val() {
    if [ "$2" -lt 2 ]; then
        echo "$1: requires a value" >&2
        exit 3
    fi
}

# timeout 不可用时直接执行（放弃超时保护）
run_with_timeout() { # $1=timeout_sec 其余=命令
    local tmo="$1"; shift
    if command -v timeout &>/dev/null; then
        timeout "$tmo" "$@"
    else
        "$@"
    fi
}

run_cmd() {
    echo "=== $* ==="
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    echo "$out"
    LAST_BENIGN=false
    if [ $rc -ne 0 ]; then
        if [ -n "$BENIGN_PATTERN" ] && echo "$out" | grep -qiE "$BENIGN_PATTERN"; then
            LAST_BENIGN=true
            echo "--- (rc=${rc}, expected on this system — not counted as failure) ---"
        else
            echo "--- (rc=${rc}) ---"
            FAILED=$((FAILED + 1))
        fi
    else
        OK_COUNT=$((OK_COUNT + 1))
    fi
    return $rc
}

# ---------- 芯片枚举（优先 npu-smi info -m 的 Phy-ID，兜底 0~MAX_CHIP 盲扫） ----------
PHY_CACHE=""
CHIPS_NOTE_PRINTED=false
list_phy_ids() {
    [ -n "$PHY_CACHE" ] && { echo "$PHY_CACHE"; return 0; }
    local smi=""
    if command -v npu-smi &>/dev/null; then
        smi="$(command -v npu-smi)"
    else
        local p
        for p in /usr/local/sbin/npu-smi /usr/local/bin/npu-smi; do
            [ -x "$p" ] && { smi="$p"; break; }
        done
    fi
    [ -z "$smi" ] && return 1
    local out
    out="$("$smi" info -m 2>/dev/null)" || return 1
    # 表头须含 Phy-ID 列（数据行固定：第 4 列 Phy-ID，第 5 列芯片名；Mcu 行排除）
    echo "$out" | head -1 | grep -q "Phy-ID" || return 1
    local ids
    ids=$(echo "$out" | awk -v max="$MAX_CHIP" \
        'NR>1 && NF>=5 && $4 ~ /^[0-9]+$/ && $5 != "Mcu" && $4 <= max {print $4}' | sort -nu)
    [ -n "$ids" ] || return 1
    PHY_CACHE="$ids"
    echo "$ids"
    return 0
}

run_chips() { # $@=除 -i 外的完整参数；遍历芯片或单芯片
    local chips=()
    local sweep=true   # 遍历模式下个别芯片缺失属正常，仅全部失败才算整体失败
    if [ -n "$TARGET_CHIP" ]; then
        chips=("$TARGET_CHIP")
        sweep=false
    else
        local detected
        if detected="$(list_phy_ids)"; then
            chips=($detected)
            if [ "$CHIPS_NOTE_PRINTED" != true ]; then
                echo "### chips auto-detected via 'npu-smi info -m' (Phy-ID): ${chips[*]}" >&2
                CHIPS_NOTE_PRINTED=true
            fi
        else
            chips=($(seq 0 "$MAX_CHIP"))
            if [ "$CHIPS_NOTE_PRINTED" != true ]; then
                echo "### chips blind-sweep 0..${MAX_CHIP} (npu-smi Phy-ID detection unavailable)" >&2
                CHIPS_NOTE_PRINTED=true
            fi
        fi
    fi
    # 遍历模式：探测不存在的芯片（稀疏/容器分卡）属正常，不计失败
    local save_pattern="$BENIGN_PATTERN"
    if [ "$sweep" = true ]; then
        if [ -z "$BENIGN_PATTERN" ]; then
            BENIGN_PATTERN='Get invalid device id'
        else
            BENIGN_PATTERN="${BENIGN_PATTERN}|Get invalid device id"
        fi
    fi
    local c rc first=1 ok_before=$OK_COUNT fail_before=$FAILED
    for c in "${chips[@]}"; do
        [ $first -eq 0 ] && echo ""
        first=0
        # 芯片标记与单芯片诊断走 stdout，与所属分节同流输出，保证 2>&1 合并查看时顺序不交错
        echo "### chip ${c}"
        run_cmd "$HCCN_BIN" -i "$c" "$@"
        rc=$?
        if [ $rc -ne 0 ]; then
            if [ "$LAST_BENIGN" = true ]; then
                echo "### chip ${c}: $* rc=${rc} (expected/skipped)"
            else
                echo "### chip ${c}: $* failed (rc=${rc})"
            fi
        fi
    done
    BENIGN_PATTERN="$save_pattern"
    if [ "$sweep" = true ] && [ "$OK_COUNT" -gt "$ok_before" ]; then
        FAILED=$fail_before  # 遍历模式下部分成功即整体成功，恢复失败计数基线
    fi
    return 0
}

TARGET_CHIP=""

# ---------- 子命令 ----------
parse_target_chip() { # 解析 -i <chip>，其余选项报错
    TARGET_CHIP=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; TARGET_CHIP="$2"; shift 2 ;;
            *) echo "unknown option: $1 (see --help)" >&2; exit 3 ;;
        esac
    done
    if [ -n "$TARGET_CHIP" ] && ! [[ "$TARGET_CHIP" =~ ^[0-9]+$ ]]; then
        echo "-i must be a numeric chip Phy-ID" >&2; exit 3
    fi
}

cmd_ip()          { parse_target_chip "$@"; run_chips -ip -g; }
cmd_link()        { parse_target_chip "$@"; run_chips -link -g; }
cmd_stat()        { parse_target_chip "$@"; run_chips -stat -g; }
cmd_net_health() {
    parse_target_chip "$@"
    # Init = 驱动侧未配置网络检测对象 IP（前置条件），现场提示避免误解为网络故障
    echo "### note: net-health requires a driver-side detection-object IP (-detect_ip -s, config cmd outside this skill); status 'Init' = not configured" >&2
    run_chips -net_health -g
}

cmd_status() {
    parse_target_chip "$@"
    # status 权限要求高于普通查询：非 root/非运行用户组报 rc=12 permission denied 属预期，
    # 降级为提示不计失败（可用 ip/link/stat 替代采集）
    BENIGN_PATTERN='permission denied'
    run_chips -status -g
    BENIGN_PATTERN=""
}

cmd_bandwidth() {
    TARGET_CHIP=""
    local window=1000
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) req_val "$1" "$#"; TARGET_CHIP="$2"; shift 2 ;;
            -t) req_val "$1" "$#"; window="$2"; shift 2 ;;
            *) echo "bandwidth: unknown option: $1" >&2; exit 3 ;;
        esac
    done
    if [ -n "$TARGET_CHIP" ] && ! [[ "$TARGET_CHIP" =~ ^[0-9]+$ ]]; then
        echo "bandwidth: -i must be a numeric chip Phy-ID" >&2; exit 3
    fi
    if ! [[ "$window" =~ ^[0-9]+$ ]] || [ "$window" -lt 100 ] || [ "$window" -gt 10000 ]; then
        echo "bandwidth: -t must be 100~10000 ms" >&2
        exit 3
    fi
    run_chips -bandwidth -g time "$window"
}

cmd_config() {
    parse_target_chip "$@"
    # RoCE 配置聚合：每项独立标注，单项失败不阻断；
    # 已知预期状态不计失败：网关未预设（点对点直连组网属合法）、eth_rate 子命令缺失（部分驱动版本）
    local items=(-gateway -mtu -speed -eth_rate)
    local item
    for item in "${items[@]}"; do
        echo "=== ${item} ==="
        case "$item" in
            -gateway)  BENIGN_PATTERN='no gateway was preset' ;;
            -eth_rate) BENIGN_PATTERN='invalid major cmd' ;;
            *)         BENIGN_PATTERN="" ;;
        esac
        run_chips "$item" -g
        BENIGN_PATTERN=""
        echo ""
    done
}

cmd_test() {
    TARGET_CHIP=""
    local mode="" size="" duration="" bidir=false force=false timeout_sec=""
    local address=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -m) req_val "$1" "$#"; mode="$2"; shift 2 ;;
            -i) req_val "$1" "$#"; TARGET_CHIP="$2"; shift 2 ;;
            -s) req_val "$1" "$#"; size="$2"; shift 2 ;;
            -D) req_val "$1" "$#"; duration="$2"; shift 2 ;;
            -b) bidir=true; shift ;;
            --timeout) req_val "$1" "$#"; timeout_sec="$2"; shift 2 ;;
            --force) force=true; shift ;;
            -*) echo "test: unknown option: $1 (use --help)" >&2; exit 3 ;;
            *) address="$1"; shift ;;
        esac
    done

    case "$mode" in
        ib_read_bw|ib_write_bw|ib_send_bw|ib_read_lat|ib_write_lat|ib_send_lat) ;;
        "") echo "test: -m <mode> is required (ib_read_bw|ib_write_bw|ib_send_bw|ib_read_lat|ib_write_lat|ib_send_lat)" >&2; exit 3 ;;
        *) echo "test: invalid mode: $mode" >&2; exit 3 ;;
    esac
    if [ "$force" != true ]; then
        echo "test: RoCE test injects traffic onto the cluster network and may affect running jobs." >&2
        echo "  Re-run with --force to confirm. Usage: test -m <mode> [-i chip] [-s bytes] [-D sec] [address] --force" >&2
        exit 3
    fi
    if [ -n "$TARGET_CHIP" ] && ! [[ "$TARGET_CHIP" =~ ^[0-9]+$ ]]; then
        echo "test: -i must be a numeric chip Phy-ID" >&2; exit 3
    fi
    if [ -n "$size" ]; then
        if ! [[ "$size" =~ ^[0-9]+$ ]] || [ "$size" -lt 1 ] || [ "$size" -gt 874736000 ]; then
            echo "test: -s must be 1~874736000 bytes" >&2
            exit 3
        fi
    fi
    if [ -n "$duration" ]; then
        if ! [[ "$duration" =~ ^[0-9]+$ ]] || [ "$duration" -lt 1 ] || [ "$duration" -gt 86400 ]; then
            echo "test: -D must be 1~86400 seconds" >&2
            exit 3
        fi
    fi
    # --timeout 未显式指定时：默认 60s；给了 -D 时自动放宽为打流时长 + 30s 收尾余量，
    # 避免长时打流被 wrapper 提前杀死
    if [ -z "$timeout_sec" ]; then
        if [ -n "$duration" ]; then
            timeout_sec=$((duration + 30))
        else
            timeout_sec=60
        fi
    fi
    if ! [[ "$timeout_sec" =~ ^[0-9]+$ ]] || [ "$timeout_sec" -lt 5 ] || [ "$timeout_sec" -gt 86430 ]; then
        echo "test: --timeout must be 5~86430 seconds" >&2
        exit 3
    fi

    # 组装底层命令
    local args=(-roce_test "$mode")
    [ -n "$TARGET_CHIP" ] && args=(-i "$TARGET_CHIP" "${args[@]}")
    [ -n "$size" ] && args+=(-s "$size")
    [ -n "$duration" ] && args+=(-D "$duration")
    [ "$bidir" = true ] && args+=(-b)
    [ -n "$address" ] && args+=("$address")

    echo "### RoCE test (${mode}) — traffic injection confirmed; timeout ${timeout_sec}s" >&2
    echo "### server mode if no address given; pair with a client on the peer chip" >&2
    echo "### after test, re-run 'collect.sh stat' to compare error counters" >&2
    echo "### if hung, recover with: collect.sh test-reset [-i ${TARGET_CHIP:-<chip>}]" >&2
    local out rc
    out="$(run_with_timeout "$timeout_sec" "$HCCN_BIN" "${args[@]}" 2>&1)"; rc=$?
    echo "=== hccn_tool ${args[*]} ==="
    echo "$out"
    if [ $rc -eq 124 ]; then
        echo "--- (timed out after ${timeout_sec}s; if still running on device side, run test-reset) ---" >&2
        FAILED=$((FAILED + 1))
    elif [ $rc -ne 0 ]; then
        echo "--- (rc=${rc}) ---"
        FAILED=$((FAILED + 1))
    else
        OK_COUNT=$((OK_COUNT + 1))
    fi
}

cmd_test_reset() {
    parse_target_chip "$@"
    run_chips -roce_test reset
}

cmd_all() {
    cmd_ip; echo ""
    cmd_link; echo ""
    cmd_stat; echo ""
    cmd_status; echo ""
    cmd_config
}

# ---------- 入口 ----------
[ $# -eq 0 ] && { help; exit 0; }
case "$1" in
    ip)          shift; cmd_ip "$@" ;;
    link)        shift; cmd_link "$@" ;;
    stat)        shift; cmd_stat "$@" ;;
    status)      shift; cmd_status "$@" ;;
    bandwidth)   shift; cmd_bandwidth "$@" ;;
    net-health)  shift; cmd_net_health "$@" ;;
    config)      shift; cmd_config "$@" ;;
    test)        shift; cmd_test "$@" ;;
    test-reset)  shift; cmd_test_reset "$@" ;;
    all)         shift; cmd_all "$@" ;;
    *) echo "Unknown subcommand: $1 (see --help)" >&2; exit 3 ;;
esac

[ "$FAILED" -gt 0 ] && exit 4
exit 0
