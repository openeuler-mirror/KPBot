#!/usr/bin/env bash
# Verify HPRE hardware crypto offload is actually active.
# Read-only: triggers bounded batches of TLS handshakes, then checks the SQE
# counter delta against successful handshakes and the worker maps.
#
# Operator-supplied values (--url/--requests/--concurrency/--pid/--pf/--ssh)
# are validated and passed as separate argv entries locally; remote commands are
# built with %q quoting so a value can never terminate the command or start a
# new one.
set -euo pipefail

SSH_TARGET=""
SSH_OPTS=${SSH_OPTS:--o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new}
REQUESTS=50
CONCURRENCY=20
URL="https://127.0.0.1:10001/index.html"
PF_PCI=${PF_PCI:-}
TARGET_PID=""
JSON=0
DEBUGFS_ROOT=${HPRE_DEBUGFS_ROOT:-/sys/kernel/debug/hisi_hpre}
PROC_ROOT=${PROC_ROOT:-/proc}
CURL_BIN=${CURL_BIN:-curl}

usage() {
    cat <<'EOF'
usage: verify_hpre_offload.sh [options]

Read-only offload verification. A PASS requires HTTPS handshakes that actually
succeed (2xx), a worker process with the offload engine loaded, and an SQE delta
that matches the number of successful handshakes within a bounded tolerance.
Shared-PF counter noise that cannot be attributed yields INSUFFICIENT, not PASS.

options:
  --ssh <user@host>   run curl and maps check remotely (lvirt guest); counter is read locally.
                      Host keys are checked with StrictHostKeyChecking=accept-new;
                      override with SSH_OPTS='-o StrictHostKeyChecking=yes -o UserKnownHostsFile=...'
  --requests <n>      total full handshakes to trigger (default 50, 1..10000)
  --concurrency <n>   maximum in-flight handshakes per batch (default 20, 1..200)
  --url <url>         HTTPS target URL (http:// is rejected; no quotes/spaces)
  --pid <pid>         inspect this process maps instead of auto-detecting nginx workers
  --pf <pci>          HPRE PF address (default: first entry under /sys/kernel/debug/hisi_hpre)
  --json              emit a JSON result object instead of text
  -h, --help          show this help

exit codes:
  0 PASS   1 FAIL/INSUFFICIENT or counter unreadable   2 bad usage
EOF
}

die_usage() {
    echo "ERROR: $*" >&2
    usage >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ssh)         SSH_TARGET=${2:?missing value for --ssh}; shift 2 ;;
        --requests)    REQUESTS=${2:?missing value for --requests}; shift 2 ;;
        --concurrency) CONCURRENCY=${2:?missing value for --concurrency}; shift 2 ;;
        --url)         URL=${2:?missing value for --url}; shift 2 ;;
        --pid)         TARGET_PID=${2:?missing value for --pid}; shift 2 ;;
        --pf)          PF_PCI=${2:?missing value for --pf}; shift 2 ;;
        --json)        JSON=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ---- input validation: these values end up in commands and JSON output ----
[[ $REQUESTS =~ ^[0-9]+$ ]] || die_usage "--requests must be a positive integer (got: $REQUESTS)"
REQUESTS=$((10#$REQUESTS))
[ "$REQUESTS" -ge 1 ] && [ "$REQUESTS" -le 10000 ] \
    || die_usage "--requests must be between 1 and 10000 (got: $REQUESTS)"
[[ $CONCURRENCY =~ ^[0-9]+$ ]] \
    || die_usage "--concurrency must be a positive integer (got: $CONCURRENCY)"
CONCURRENCY=$((10#$CONCURRENCY))
[ "$CONCURRENCY" -ge 1 ] && [ "$CONCURRENCY" -le 200 ] \
    || die_usage "--concurrency must be between 1 and 200 (got: $CONCURRENCY)"
[ "$CONCURRENCY" -le "$REQUESTS" ] || CONCURRENCY=$REQUESTS

URL_RE='^https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+$'
[[ $URL =~ $URL_RE ]] \
    || die_usage "--url contains unsupported characters (allowed: A-Z a-z 0-9 . _ ~ : / ? # @ ! \$ & ( ) * + , ; = % -): $URL"
case "$URL" in
    https://*) ;;
    *) die_usage "--url must use https:// (HPRE offload verification requires TLS handshakes)" ;;
esac
if [ -n "$TARGET_PID" ]; then
    [[ $TARGET_PID =~ ^[0-9]+$ ]] || die_usage "--pid must be numeric (got: $TARGET_PID)"
    TARGET_PID=$((10#$TARGET_PID))
    [ "$TARGET_PID" -gt 0 ] || die_usage "--pid must be greater than 0"
fi
if [ -n "$PF_PCI" ]; then
    [[ $PF_PCI =~ ^[0-9A-Fa-f:.]+$ ]] || die_usage "--pf must be a PCI address (got: $PF_PCI)"
fi
if [ -n "$SSH_TARGET" ]; then
    case "$SSH_TARGET" in
        -*) die_usage "--ssh must be user@host, not an option: $SSH_TARGET" ;;
        *[!A-Za-z0-9_.@:-]*) die_usage "--ssh contains unsupported characters: $SSH_TARGET" ;;
    esac
fi

have() { command -v "$1" >/dev/null 2>&1; }

# Remote shell parses $1, so every caller-supplied value must already be %q-quoted.
ssh_run() {
    # shellcheck disable=SC2086  # SSH_OPTS is an operator-configured option list
    ssh $SSH_OPTS -- "$SSH_TARGET" "$1"
}

if [ -n "$PF_PCI" ]; then
    REGS_LIST="$DEBUGFS_ROOT/$PF_PCI/qm/regs"
    SCOPE="$PF_PCI"
else
    REGS_LIST=$(ls "$DEBUGFS_ROOT"/*/qm/regs 2>/dev/null | tr '\n' ' ' || true)
    SCOPE="aggregate"
fi
if [ -z "${REGS_LIST// /}" ]; then
    echo "ERROR: no HPRE regs under $DEBUGFS_ROOT (mount debugfs or pass --pf <pci>)" >&2
    exit 1
fi

# prints "<total> <devices>" when at least one device exposes the counter
read_cnt() {
    local total=0 n=0 reg raw v
    for reg in $REGS_LIST; do
        raw=""
        if [ -r "$reg" ]; then
            raw=$(cat "$reg" 2>/dev/null || true)
        elif have sudo; then
            raw=$(sudo -n cat "$reg" 2>/dev/null || true)
        fi
        [ -z "$raw" ] && continue
        v=$(printf '%s\n' "$raw" | awk '/QM_DFX_SEND_SQE_TO_ACC_CNT/{for(i=1;i<=NF;i++) if($i ~ /^(0x[0-9a-fA-F]+|[0-9]+)$/) {print $i; exit}}')
        [ -z "$v" ] && continue
        total=$((total + v))
        n=$((n + 1))
    done
    [ "$n" -gt 0 ] && printf '%s %s\n' "$total" "$n"
    return 0
}

# `|| true` keeps `set -e` from killing the script before the diagnostic below:
# read_cnt returns 1 when no counter could be read, which is the common
# non-root case that must fall through to the friendly ERROR message.
before_pair=$(read_cnt) || true
if [ -z "${before_pair:-}" ]; then
    echo "ERROR: QM_DFX_SEND_SQE_TO_ACC_CNT not readable (scope=$SCOPE; need root and debugfs mounted)" >&2
    exit 1
fi
before=${before_pair%% *}
DEVCNT=${before_pair##* }

STATUS_FILE="$(mktemp "${TMPDIR:-/tmp}/hpre-status.XXXXXX")"
cleanup() { rm -f "${STATUS_FILE}"; }
trap cleanup EXIT

run_local_batch() {
    local count=$1 i
    for ((i = 0; i < count; i++)); do
        "$CURL_BIN" -sk --max-time 5 -o /dev/null -w '%{http_code}\n' "$URL" >>"${STATUS_FILE}" &
    done
    wait || true
}

trigger_handshakes() {
    local remaining=$REQUESTS batch remote
    if [ -n "$SSH_TARGET" ]; then
        # Bounded batches: at most CONCURRENCY curls in flight, never 100k
        # processes at once. Each curl prints one status line.
        remote="i=0; while [ \"\$i\" -lt ${REQUESTS} ]; do j=0; while [ \"\$j\" -lt ${CONCURRENCY} ] && [ \"\$i\" -lt ${REQUESTS} ]; do curl -sk --max-time 5 -o /dev/null -w '%{http_code}\\n' $(printf '%q' "$URL") & i=\$((i+1)); j=\$((j+1)); done; wait; done"
        ssh_run "$remote" >"${STATUS_FILE}" 2>/dev/null || true
    else
        while [ "$remaining" -gt 0 ]; do
            batch="$CONCURRENCY"
            [ "$batch" -le "$remaining" ] || batch="$remaining"
            run_local_batch "$batch"
            remaining=$((remaining - batch))
        done
    fi
}
trigger_handshakes

after_pair=$(read_cnt) || true
if [ -z "${after_pair:-}" ]; then
    echo "ERROR: counter became unreadable (scope=$SCOPE)" >&2
    exit 1
fi
after=${after_pair%% *}

delta=$((after - before))

collect_loaded_libs() {
    local pid=${1:-} remote
    if [ -n "$SSH_TARGET" ]; then
        if [ -n "$pid" ]; then
            remote="grep -oE 'e_sse\.so|uadk_engine\.so|libasf\.so' /proc/$(printf '%q' "$pid")/maps 2>/dev/null | sort -u | paste -sd' ' -"
        else
            remote="p=\$(pgrep -f 'nginx: worke[r]' | head -1); [ -n \"\$p\" ] && grep -oE 'e_sse\.so|uadk_engine\.so|libasf\.so' /proc/\$p/maps | sort -u | paste -sd' ' -"
        fi
        ssh_run "$remote" 2>/dev/null || true
    else
        if [ -z "$pid" ]; then
            pid=$(pgrep -f 'nginx: worke[r]' | head -1 || true)
            [ -n "$pid" ] || return 0
        fi
        grep -oE 'e_sse\.so|uadk_engine\.so|libasf\.so' "$PROC_ROOT/$pid/maps" 2>/dev/null \
            | sort -u | paste -sd' ' - || true
    fi
}

if [ -n "$TARGET_PID" ]; then
    loaded=$(collect_loaded_libs "$TARGET_PID")
else
    loaded=$(collect_loaded_libs)
fi

handshakes_completed=$(wc -l <"${STATUS_FILE}" | tr -d ' ')
handshakes_success=$(awk '$1 ~ /^2[0-9][0-9]$/ {n++} END {print n+0}' "${STATUS_FILE}")
http_status_counts=$(awk '{c[$1]++} END {for (k in c) printf "%s:%d ", k, c[k]}' "${STATUS_FILE}" | sed 's/[[:space:]]*$//')

counter_ok=0; [ "$delta" -gt 0 ] && counter_ok=1
e_sse_ok=0
printf '%s' "$loaded" | grep -q 'e_sse\.so' && e_sse_ok=1
uadk_ok=0
printf '%s' "$loaded" | grep -q 'uadk_engine\.so' && uadk_ok=1

if [ "$e_sse_ok" = "1" ]; then
    offload_mode="asf"
elif [ "$uadk_ok" = "1" ]; then
    offload_mode="direct"
else
    offload_mode="none"
fi

# Attribute the counter delta to this round's successful handshakes. At least
# 90% of the requested handshakes must succeed; the delta must cover them and
# must not exceed a small tolerance (shared PF traffic can inflate it).
attribution="aligned"
status=PASS
if [ "$handshakes_success" -eq 0 ]; then
    attribution="no_successful_handshakes"
    status="INSUFFICIENT"
elif [ $((handshakes_success * 10)) -lt $((REQUESTS * 9)) ]; then
    attribution="too_few_successful_handshakes"
    status="INSUFFICIENT"
elif [ "$delta" -eq 0 ]; then
    attribution="no_sqe_growth"
    status="FAIL"
elif [ "$delta" -lt "$handshakes_success" ]; then
    attribution="sqe_below_successful_handshakes"
    status="INSUFFICIENT"
elif [ "$delta" -gt $((handshakes_success * 3)) ]; then
    attribution="sqe_exceeds_handshake_attribution"
    status="INSUFFICIENT"
elif [ "$offload_mode" = "none" ]; then
    attribution="engine_not_loaded"
    status="FAIL"
fi

if [ "$JSON" = "1" ]; then
    esc() { printf '%s' "${1:-}" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
    cat <<EOF
{
  "schema": "hpre_offload_verification.v1",
  "scope": "$SCOPE",
  "devices_counted": $DEVCNT,
  "pid": ${TARGET_PID:-null},
  "requests": $REQUESTS,
  "concurrency": $CONCURRENCY,
  "url": "$(esc "$URL")",
  "before": $((before)),
  "after": $((after)),
  "delta": $delta,
  "handshakes_requested": $REQUESTS,
  "handshakes_completed": $handshakes_completed,
  "handshakes_success": $handshakes_success,
  "http_status_counts": "$(esc "$http_status_counts")",
  "worker_loaded_libs": "$(esc "$loaded")",
  "counter_grew": $( [ "$counter_ok" = "1" ] && echo true || echo false ),
  "attribution": "$attribution",
  "offload_mode": "$offload_mode",
  "e_sse_loaded": $( [ "$e_sse_ok" = "1" ] && echo true || echo false ),
  "uadk_engine_loaded": $( [ "$uadk_ok" = "1" ] && echo true || echo false ),
  "status": "$status"
}
EOF
else
    printf 'scope=%s devices=%s\nbefore=%s\nafter=%s\ndelta=%s (requested=%s success=%s)\nhttp_status=%s\nworker_libs=%s\noffload_mode=%s\nattribution=%s\n' \
        "$SCOPE" "$DEVCNT" "$before" "$after" "$delta" "$REQUESTS" "$handshakes_success" \
        "${http_status_counts:-none}" "${loaded:-none}" "$offload_mode" "$attribution"
    [ "$counter_ok" = "1" ] || echo "NOTE: no SQE growth; check ssl_engine, RSA cert, session cache, quota, device enabled"
    [ "$offload_mode" != "none" ] || echo "NOTE: worker has no offload engine loaded; nginx not restarted or engine path wrong"
    [ "$handshakes_success" -gt 0 ] || echo "NOTE: no successful HTTPS handshakes; fix the URL/certificate/network before measuring"
    echo "$status: hardware crypto offload $( [ "$status" = "PASS" ] && echo active || echo inactive ) (mode=$offload_mode)"
fi

[ "$status" = "PASS" ] || exit 1
