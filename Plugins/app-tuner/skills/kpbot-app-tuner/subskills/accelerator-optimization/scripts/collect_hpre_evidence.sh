#!/usr/bin/env bash
# Read-only evidence collection for HPRE / UADK crypto offload.
# Emits JSON; when --evidence-dir is given it validates snapshot freshness first
# and writes hpre_offload_evidence.json next to snapshot_metadata.json.
set -u

OUT=""
PID=""
EVIDENCE_DIR=""
CURRENT_RUN_ID=""
CURRENT_RUN_STARTED_AT=""
TARGET_IDENTITY_PATH=""
OPENSSL_DIR=${OPENSSL_DIR:-/usr/local/openssl}
ENGINE_DIR=${ENGINE_DIR:-/usr/lib64/engines-1.1}
ASF_DIR=${ASF_CONF_PATH:-/etc/asf_conf}
NGX_PREFIX=${NGX_PREFIX:-/usr/local/nginx}
NGINX_CONF=${NGINX_CONF:-$NGX_PREFIX/conf/nginx.conf}
SSL_CERT=${SSL_CERT:-$NGX_PREFIX/conf/ssl/server.crt}
SSL_CNF=${SSL_CNF:-$NGX_PREFIX/conf/openssl_sse_uadk.cnf}
PF_PCI=${PF_PCI:-}

usage() {
    cat <<'EOF'
usage: collect_hpre_evidence.sh [options]

Read-only HPRE/UADK offload evidence collector. Outputs JSON, exits 0 even when
the accelerator is absent (check verdict.crypto_accelerator_present).

pipeline binding (all four are required when writing into an evidence snapshot dir):
  --evidence-dir <dir>          dir containing snapshot_metadata.json; validates
                                current_run_id / evidence status / snapshot_time /
                                target_identity, then writes hpre_offload_evidence.json
  --current-run-id <id>         current run id, must match snapshot_metadata.json
  --current-run-started-at <ts> ISO 8601 run start; required and must parse
  --target-identity <path>      target instance identity JSON; required and must match

collection scope:
  --output <path>               write JSON to <path> instead of stdout
  --pid <pid>                   inspect this process maps for offload engine libs
  --openssl-dir <dir>           OpenSSL prefix (default /usr/local/openssl)
  --engine-dir <dir>            OpenSSL engine dir (default /usr/lib64/engines-1.1)
  --asf-dir <dir>               ASF config dir (default /etc/asf_conf)
  --nginx-conf <path>           nginx config to inspect
  --ssl-cnf <path>              OpenSSL engine cnf used by engine -t
  --cert <path>                 TLS certificate to inspect
  --pf <pci>                    HPRE PF address for SQE counter (default: first found)
  -h, --help                    show this help

exit codes:
  0 success  1 evidence write failed  2 bad usage
  3 evidence freshness gate failed  4 snapshot metadata unreadable
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output)                  OUT=${2:?missing value for --output}; shift 2 ;;
        --pid)                     PID=${2:?missing value for --pid}; shift 2 ;;
        --evidence-dir)            EVIDENCE_DIR=${2:?}; shift 2 ;;
        --current-run-id)          CURRENT_RUN_ID=${2:?}; shift 2 ;;
        --current-run-started-at)  CURRENT_RUN_STARTED_AT=${2:?}; shift 2 ;;
        --target-identity)         TARGET_IDENTITY_PATH=${2:?}; shift 2 ;;
        --openssl-dir)             OPENSSL_DIR=${2:?}; shift 2 ;;
        --engine-dir)              ENGINE_DIR=${2:?}; shift 2 ;;
        --asf-dir)                 ASF_DIR=${2:?}; shift 2 ;;
        --nginx-conf)              NGINX_CONF=${2:?}; shift 2 ;;
        --ssl-cnf)                 SSL_CNF=${2:?}; shift 2 ;;
        --cert)                    SSL_CERT=${2:?}; shift 2 ;;
        --pf)                      PF_PCI=${2:?}; shift 2 ;;
        -h|--help)                 usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -n "$PID" ]; then
    [[ $PID =~ ^[0-9]+$ ]] || { echo "ERROR: --pid must be numeric (got: $PID)" >&2; exit 2; }
    PID=$((10#$PID))
    [ "$PID" -gt 0 ] || { echo "ERROR: --pid must be greater than 0" >&2; exit 2; }
fi
if [ -n "$EVIDENCE_DIR" ]; then
    [ -n "$CURRENT_RUN_ID" ] || { echo "ERROR: --evidence-dir requires --current-run-id" >&2; exit 2; }
    [ -n "$CURRENT_RUN_STARTED_AT" ] || {
        echo "ERROR: --evidence-dir requires --current-run-started-at (ISO 8601)" >&2; exit 2;
    }
    [ -n "$TARGET_IDENTITY_PATH" ] || {
        echo "ERROR: --evidence-dir requires --target-identity <path>" >&2; exit 2;
    }
fi

STARTED_AT="${CURRENT_RUN_STARTED_AT:-}"
unbound=1
if [ -z "$CURRENT_RUN_ID" ]; then
    CURRENT_RUN_ID=""
    STARTED_AT=""
fi

# --- evidence freshness gate (mirrors scripts/create_subagent_tasks.py) ----
if [ -n "$EVIDENCE_DIR" ]; then
    META="$EVIDENCE_DIR/snapshot_metadata.json"
    if [ ! -f "$META" ]; then
        echo "ERROR: snapshot_metadata.json not found in $EVIDENCE_DIR" >&2
        exit 4
    fi
    gate_msg=$(python3 - "$META" "$CURRENT_RUN_ID" "$CURRENT_RUN_STARTED_AT" "$TARGET_IDENTITY_PATH" 2>&1 <<'PY'
import json, sys
from datetime import datetime, timezone

meta_path, run_id, started_at, target_path = sys.argv[1:5]
try:
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
except Exception as exc:  # noqa: BLE001
    print(f"cannot parse {meta_path}: {exc}")
    sys.exit(4)

if not isinstance(meta, dict):
    print(f"{meta_path} must contain a JSON object")
    sys.exit(4)


def parse_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


errors = []
meta_run_id = meta.get("current_run_id")
if not meta_run_id:
    errors.append("snapshot_metadata.json is missing current_run_id")
elif meta_run_id != run_id:
    errors.append(
        f"evidence current_run_id mismatch: metadata={meta_run_id!r}, requested={run_id!r}"
    )
status = meta.get("current_evidence_status")
if status != "current":
    errors.append(f"current evidence status must be 'current' (got {status!r})")

started_dt = parse_ts(started_at)
snapshot_raw = meta.get("snapshot_time")
snapshot_dt = parse_ts(snapshot_raw)
if started_dt is None:
    errors.append(f"current_run_started_at is missing or not ISO 8601: {started_at!r}")
if snapshot_dt is None:
    errors.append(f"snapshot_metadata.json snapshot_time is missing or not ISO 8601: {snapshot_raw!r}")
if started_dt is not None and snapshot_dt is not None and snapshot_dt < started_dt:
    errors.append(
        "evidence snapshot_time is earlier than current_run_started_at: "
        f"snapshot_time={snapshot_raw!r}, started={started_at!r}"
    )

try:
    with open(target_path, encoding="utf-8") as fh:
        target = json.load(fh)
except Exception as exc:  # noqa: BLE001
    errors.append(f"cannot parse target identity {target_path}: {exc}")
    target = None
if target is not None and not isinstance(target, dict):
    errors.append(f"target identity {target_path} must contain a JSON object")
    target = None
meta_target = meta.get("target_identity")
if not isinstance(meta_target, dict) or not meta_target:
    errors.append("snapshot_metadata.json is missing a non-empty target_identity object")
elif target is not None and target != meta_target:
    errors.append("target identity JSON does not match snapshot metadata target_identity")

if errors:
    print("; ".join(errors))
    sys.exit(3)
print("ok")
PY
)
    gate_rc=$?
    if [ "$gate_rc" -ne 0 ]; then
        echo "ERROR: evidence freshness gate failed: $gate_msg" >&2
        exit "$gate_rc"
    fi
    unbound=0
    OUT="$EVIDENCE_DIR/hpre_offload_evidence.json"
fi

esc() { printf '%s' "${1:-}" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
str_or_null() { if [ -n "${1:-}" ]; then printf '"%s"' "$(esc "$1")"; else printf 'null'; fi; }
num_or_null() { if [ -n "${1:-}" ]; then printf '%s' "$1"; else printf 'null'; fi; }
bool() { if [ "${1:-0}" = "1" ]; then printf 'true'; else printf 'false'; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

notes=()
add_note() { notes+=("$1"); }

arch=$(uname -m 2>/dev/null || echo unknown)
kernel=$(uname -r 2>/dev/null || echo unknown)

# --- device presence: prefer driver/uacce truth, lspci only as supplement ---
hpre_pci=""
if have lspci; then
    hpre_pci=$(lspci -nnk 2>/dev/null | awk '
        /^[^ \t]/ {
            if (keep) print addr
            keep = 0
            addr = $1
            next
        }
        /Kernel driver in use: hisi_hpre/ { keep = 1 }
        END { if (keep) print addr }
    ' | paste -sd';' -)
    if [ -z "$hpre_pci" ]; then
        hpre_pci=$(lspci -nn 2>/dev/null | grep -E '19e5:a25[89]' | awk '{print $1}' | paste -sd';' -)
        [ -n "$hpre_pci" ] && add_note "hpre_detected_by_pci_id_only (driver not bound)"
    fi
    [ -z "$hpre_pci" ] && add_note "lspci found no HPRE device"
else
    add_note "lspci_missing; device presence not verified"
fi

uacce_all=""
if [ -d /sys/class/uacce ]; then
    uacce_all=$(ls /sys/class/uacce 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]*$//')
fi
hpre_uacce=$(printf '%s\n' "$uacce_all" | tr ' ' '\n' | grep -E '^hisi_hpre-' | tr '\n' ' ' | sed 's/[[:space:]]*$//' || true)
[ -z "$uacce_all" ] && add_note "no uacce device under /sys/class/uacce"
if [ -n "$uacce_all" ] && [ -z "$hpre_uacce" ]; then
    add_note "uacce devices present but none is HPRE: $uacce_all"
fi

dev_nodes=$(ls -1 /dev/hisi_hpre-* 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]*$//')

uacce_mode=$(cat /sys/module/hisi_hpre/parameters/uacce_mode 2>/dev/null || echo "")
[ -z "$uacce_mode" ] && add_note "hisi_hpre module not loaded or uacce_mode unreadable"

# --- libraries ---
lib_json=""
for lib in libasf.so libcjson.so.1 libnuma.so.1 libwd.so.2 libwd_crypto.so.2; do
    present=0
    if [ -e "/usr/lib64/$lib" ] || [ -e "/usr/local/lib/$lib" ] || [ -e "/lib64/$lib" ]; then
        present=1
    elif have ldconfig && ldconfig -p 2>/dev/null | grep -q "$(printf '%s' "$lib" | sed 's/\./\\./g')"; then
        present=1
    fi
    [ -n "$lib_json" ] && lib_json="$lib_json,"
    lib_json="$lib_json\"$lib\":$(bool "$present")"
done

# --- engines ---
eng_json=""
for so in e_sse.so uadk_engine.so; do
    path="$ENGINE_DIR/$so"
    present=0; missing=""
    if [ -e "$path" ]; then
        present=1
        if have ldd; then
            missing=$(ldd "$path" 2>/dev/null | awk '/not found/{print $1}' | paste -sd' ' -)
        fi
    fi
    [ -n "$eng_json" ] && eng_json="$eng_json,"
    eng_json="$eng_json\"$so\":{\"present\":$(bool "$present"),\"path\":$(str_or_null "$path"),\"missing_libs\":$(str_or_null "$missing")}"
done

# --- openssl engine self-test ---
eng_test_json=""
ossl_bin="$OPENSSL_DIR/bin/openssl"

engine_state() {
    local eng=$1 out
    [ -x "$ossl_bin" ] || { printf 'no_openssl\n'; return 0; }
    if ! out=$(LD_LIBRARY_PATH="$OPENSSL_DIR/lib:${LD_LIBRARY_PATH:-}" \
               OPENSSL_ENGINES="$ENGINE_DIR" \
               OPENSSL_CONF="$SSL_CNF" \
               "$ossl_bin" engine -t "$eng" 2>&1); then
        printf 'unavailable\n'
        return 0
    fi
    # OpenSSL 1.1.1 prints "[ unavailable ]" for initialization failures;
    # match the complete status line instead of a substring.
    if printf '%s\n' "$out" | grep -qE '^[[:space:]]*\[ available \][[:space:]]*$'; then
        printf 'available\n'
    else
        printf 'unavailable\n'
    fi
}
for eng in uadk_engine e_sse; do
    state=$(engine_state "$eng")
    [ -n "$eng_test_json" ] && eng_test_json="$eng_test_json,"
    eng_test_json="$eng_test_json\"$eng\":\"$state\""
done
[ ! -x "$ossl_bin" ] && add_note "openssl binary not found at $ossl_bin"

# --- ASF config ---
quota=""; throughput=""; asf_enabled=""
if [ -f "$ASF_DIR/scheduler.json" ]; then
    quota=$(grep -oE '"quota"[[:space:]]*:[[:space:]]*"[^"]*"' "$ASF_DIR/scheduler.json" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
fi
if [ -f "$ASF_DIR/dev_metadata/uadk_engine.json" ]; then
    throughput=$(grep -oE '"throughput"[[:space:]]*:[[:space:]]*"[^"]*"' "$ASF_DIR/dev_metadata/uadk_engine.json" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    asf_enabled=$(grep -oE '"enabled"[[:space:]]*:[[:space:]]*(true|false)' "$ASF_DIR/dev_metadata/uadk_engine.json" 2>/dev/null | head -1 | grep -oE '(true|false)')
fi
[ -z "$quota" ] && add_note "scheduler.json missing or quota unreadable under $ASF_DIR"

# --- nginx config ---
ssl_engine=""; session_cache=""; session_tickets=""; worker_processes=""
if [ -f "$NGINX_CONF" ]; then
    ssl_engine=$(grep -oE '^[[:space:]]*ssl_engine[[:space:]]+[A-Za-z0-9_]+' "$NGINX_CONF" 2>/dev/null | head -1 | awk '{print $2}')
    session_cache=$(grep -oE '^[[:space:]]*ssl_session_cache[[:space:]]+[A-Za-z]+' "$NGINX_CONF" 2>/dev/null | head -1 | awk '{print $2}')
    session_tickets=$(grep -oE '^[[:space:]]*ssl_session_tickets[[:space:]]+(on|off)' "$NGINX_CONF" 2>/dev/null | head -1 | awk '{print $2}')
    worker_processes=$(grep -oE '^[[:space:]]*worker_processes[[:space:]]+[A-Za-z0-9]+' "$NGINX_CONF" 2>/dev/null | head -1 | awk '{print $2}')
else
    add_note "nginx config not found: $NGINX_CONF"
fi

# --- certificate ---
cert_alg=""
if [ -f "$SSL_CERT" ] && [ -x "$ossl_bin" ]; then
    cert_alg=$("$ossl_bin" x509 -in "$SSL_CERT" -noout -text 2>/dev/null | awk -F': ' '/Public Key Algorithm/{print $2; exit}')
else
    add_note "certificate not readable or openssl missing: $SSL_CERT"
fi

# --- worker maps ---
maps_json="null"
maps_loaded=0
if [ -n "$PID" ]; then
    if [ -r "/proc/$PID/maps" ]; then
        loaded=$(grep -oE 'e_sse\.so|uadk_engine\.so|libasf\.so' "/proc/$PID/maps" 2>/dev/null | sort -u | paste -sd' ' -)
        [ -n "$loaded" ] && maps_loaded=1
        maps_json="{\"pid\":$PID,\"loaded\":$(str_or_null "$loaded")}"
    else
        maps_json="{\"pid\":$PID,\"loaded\":null}"
        add_note "cannot read /proc/$PID/maps (permission or pid not found)"
    fi
fi

# --- SQE counter (aggregate across every device that exposes it) ---
if [ -n "$PF_PCI" ]; then
    regs_list="/sys/kernel/debug/hisi_hpre/$PF_PCI/qm/regs"
    sqe_scope="$PF_PCI"
else
    regs_list=$(ls /sys/kernel/debug/hisi_hpre/*/qm/regs 2>/dev/null | tr '\n' ' ' || true)
    sqe_scope="aggregate"
fi
sqe_json="null"
if [ -n "${regs_list// /}" ]; then
    sqe_total=0
    sqe_devices=0
    sqe_dev_json=""
    for reg in $regs_list; do
        dev=$(printf '%s' "$reg" | awk -F/ '{print $(NF-2)}')
        raw=""
        if [ -r "$reg" ]; then
            raw=$(cat "$reg" 2>/dev/null || true)
        elif have sudo; then
            raw=$(sudo -n cat "$reg" 2>/dev/null || true)
        fi
        [ -z "$raw" ] && continue
        cnt=$(printf '%s\n' "$raw" | awk '/QM_DFX_SEND_SQE_TO_ACC_CNT/{for(i=1;i<=NF;i++) if($i ~ /^(0x[0-9a-fA-F]+|[0-9]+)$/) {print $i; exit}}')
        [ -z "$cnt" ] && continue
        cnt_dec=$((cnt))
        sqe_total=$((sqe_total + cnt_dec))
        sqe_devices=$((sqe_devices + 1))
        [ -n "$sqe_dev_json" ] && sqe_dev_json="$sqe_dev_json,"
        sqe_dev_json="$sqe_dev_json{\"device\":$(str_or_null "$dev"),\"value\":$cnt_dec,\"raw\":$(str_or_null "$cnt")}"
    done
    if [ "$sqe_devices" -gt 0 ]; then
        sqe_json="{\"scope\":$(str_or_null "$sqe_scope"),\"devices_counted\":$sqe_devices,\"total\":$sqe_total,\"devices\":[$sqe_dev_json]}"
    else
        add_note "SQE counter present but QM_DFX_SEND_SQE_TO_ACC_CNT not readable (need root)"
    fi
else
    add_note "no /sys/kernel/debug/hisi_hpre entry; cannot read SQE counter"
fi

# --- verdict ---
present=0
if [ -n "$hpre_pci" ] || [ -n "$hpre_uacce" ] || [ -n "$dev_nodes" ]; then
    present=1
fi
if [ "$present" = "1" ] && [ -z "$hpre_uacce" ]; then
    add_note "HPRE detected by PCI/driver but no hisi_hpre-* uacce device; enable uacce_mode=2 before judging UADK readiness"
fi

uacce_ready=0
[ -n "$hpre_uacce" ] && [ "$uacce_mode" = "2" ] && uacce_ready=1
uadk_ready=0
if [ "$uacce_ready" = "1" ]; then
    if printf '%s' "$eng_test_json" | grep -q '"uadk_engine":"available"'; then
        uadk_ready=1
    fi
fi

offload_configured=0
if [ "$maps_loaded" = "1" ]; then
    offload_configured=1
elif [ -n "$ssl_engine" ] && [ "$ssl_engine" != "off" ]; then
    offload_configured=1
fi

if [ "$present" = "0" ]; then
    add_note "crypto_accelerator_present=false; HPRE offload not applicable on this node"
elif [ "$uadk_ready" = "0" ]; then
    add_note "crypto accelerator present but UADK path not ready; candidate actions should target enablement"
fi
[ -n "$cert_alg" ] && [ "$cert_alg" != "rsaEncryption" ] && add_note "certificate algorithm is $cert_alg; HPRE RSA offload will not apply"

notes_json=""
for n in "${notes[@]:-}"; do
    [ -z "$n" ] && continue
    [ -n "$notes_json" ] && notes_json="$notes_json,"
    notes_json="$notes_json\"$(esc "$n")\""
done

if [ "$unbound" = "1" ]; then
    evidence_status="unbound"
else
    evidence_status="current"
fi

json=$(cat <<EOF
{
  "schema": "hpre_offload_evidence.v1",
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)",
  "current_run_id": $(str_or_null "$CURRENT_RUN_ID"),
  "current_run_started_at": $(str_or_null "$STARTED_AT"),
  "current_evidence_status": "$evidence_status",
  "snapshot_metadata_path": $(str_or_null "${EVIDENCE_DIR:+$EVIDENCE_DIR/snapshot_metadata.json}"),
  "arch": $(str_or_null "$arch"),
  "kernel": $(str_or_null "$kernel"),
  "device": {
    "hpre_pci": $(str_or_null "$hpre_pci"),
    "uacce_devices": $(str_or_null "$uacce_all"),
    "hpre_uacce_devices": $(str_or_null "$hpre_uacce"),
    "dev_nodes": $(str_or_null "$dev_nodes"),
    "uacce_mode": $(str_or_null "$uacce_mode")
  },
  "libraries": {$lib_json},
  "engines": {$eng_json},
  "openssl_engine_test": {$eng_test_json},
  "openssl_prefix": $(str_or_null "$OPENSSL_DIR"),
  "engine_dir": $(str_or_null "$ENGINE_DIR"),
  "asf": {
    "conf_dir": $(str_or_null "$ASF_DIR"),
    "quota": $(str_or_null "$quota"),
    "throughput": $(str_or_null "$throughput"),
    "enabled": $(str_or_null "$asf_enabled")
  },
  "nginx": {
    "conf": $(str_or_null "$NGINX_CONF"),
    "ssl_engine": $(str_or_null "$ssl_engine"),
    "ssl_session_cache": $(str_or_null "$session_cache"),
    "ssl_session_tickets": $(str_or_null "$session_tickets"),
    "worker_processes": $(str_or_null "$worker_processes")
  },
  "certificate": {
    "path": $(str_or_null "$SSL_CERT"),
    "algorithm": $(str_or_null "$cert_alg")
  },
  "worker_maps": $maps_json,
  "sqe_counter": $sqe_json,
  "verdict": {
    "crypto_accelerator_present": $(bool "$present"),
    "uacce_ready": $(bool "$uacce_ready"),
    "uadk_path_ready": $(bool "$uadk_ready"),
    "offload_configured": $(bool "$offload_configured")
  },
  "fallback_notes": [$notes_json]
}
EOF
)

if [ -n "$OUT" ]; then
    out_dir=$(dirname "$OUT")
    tmp="${OUT}.tmp.$$"
    if ! mkdir -p "$out_dir"; then
        echo "ERROR: cannot create evidence directory: $out_dir" >&2
        exit 1
    fi
    if ! printf '%s\n' "$json" > "$tmp"; then
        rm -f "$tmp"
        echo "ERROR: cannot write temporary evidence file: $tmp" >&2
        exit 1
    fi
    # Same-directory rename keeps the target atomic: readers see the old file
    # until the complete JSON is in place, and a failed write never exits 0.
    if ! mv -f "$tmp" "$OUT"; then
        rm -f "$tmp"
        echo "ERROR: cannot replace evidence file atomically: $OUT" >&2
        exit 1
    fi
    echo "evidence written: $OUT" >&2
else
    printf '%s\n' "$json"
fi
