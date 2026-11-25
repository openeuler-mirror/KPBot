#!/usr/bin/env bash
# Idempotent HPRE/UADK crypto offload enablement for OpenSSL 1.1 + nginx.
# Default is dry-run. Real changes require --execute plus --approved-change-id.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EXECUTE=0
APPROVED_ID=""
NGINX_CONF=${NGINX_CONF:-/usr/local/nginx/conf/nginx.conf}
NGINX_BIN=${NGINX_BIN:-}
NGX_PREFIX=${NGX_PREFIX:-/usr/local/nginx}
OPENSSL_DIR=${OPENSSL_DIR:-/usr/local/openssl}
ENGINE_DIR=${ENGINE_DIR:-/usr/lib64/engines-1.1}
ASF_DIR=${ASF_DIR:-${ASF_CONF_PATH:-/etc/asf_conf}}
SSL_CNF=${SSL_CNF:-$NGX_PREFIX/conf/openssl_sse_uadk.cnf}
ARTIFACT_DIR=${ARTIFACT_DIR:-}
INSTALL_PKGS=${INSTALL_PKGS:-0}
FORCE=${FORCE:-0}
QUOTA=${QUOTA:-450OPS}
THROUGHPUT=${THROUGHPUT:-2000OPS}
ONLY_QUOTA=0
ALL_QUOTAS=0

CAPACITY_RE='^[0-9]+([.][0-9]+)?(OPS|GB/s|MB/s|KB/s)$'

usage() {
    cat <<'EOF'
usage: enable_uadk_offload.sh [options]

Default is dry-run. Nothing is modified without --execute.

gates:
  --execute                    really apply changes
  --approved-change-id <id>    required with --execute (from main workflow approval)

targets:
  --nginx-conf <path>          nginx config to patch (default /usr/local/nginx/conf/nginx.conf)
  --nginx-bin <path>           nginx binary used for `-t` validation after patching
                               (default: $NGX_PREFIX/sbin/nginx, then $PATH)
  --openssl-dir <dir>          OpenSSL prefix (default /usr/local/openssl)
  --engine-dir <dir>           engine dir (default /usr/lib64/engines-1.1)
  --asf-dir <dir>              ASF config dir (default /etc/asf_conf)
  --ssl-cnf <path>             OpenSSL engine cnf output path

behaviour:
  --quota <450OPS|1.5GB/s>     ASF scheduler quota (default 450OPS); cannot exceed throughput
  --throughput <2000OPS>       device throughput upper bound (default 2000OPS)
  --only-quota                 only update scheduler.json quota, skip everything else
  --all-quotas                 when scheduler.json has multiple quota entries, rewrite all of them
  --artifact-dir <dir>         directory holding libasf.so/e_sse.so/uadk_engine.so/libwd*
  --install-pkgs               try dnf install for cjson/numactl/uadk_engine/libwd
  --force                      overwrite existing ASF config and cnf
  -h, --help                   show this help
EOF
}

valid_capacity() { [[ $1 =~ $CAPACITY_RE ]]; }

need_capacity() {
    valid_capacity "$2" || { echo "ERROR: invalid value for $1: $2 (expect e.g. 450OPS or 1.5GB/s)" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
    case "$1" in
        --execute)            EXECUTE=1; shift ;;
        --approved-change-id) APPROVED_ID=${2:?missing value for --approved-change-id}; shift 2 ;;
        --nginx-conf)         NGINX_CONF=${2:?}; shift 2 ;;
        --nginx-bin)          NGINX_BIN=${2:?}; shift 2 ;;
        --openssl-dir)        OPENSSL_DIR=${2:?}; shift 2 ;;
        --engine-dir)         ENGINE_DIR=${2:?}; shift 2 ;;
        --asf-dir)            ASF_DIR=${2:?}; shift 2 ;;
        --ssl-cnf)            SSL_CNF=${2:?}; shift 2 ;;
        --quota)              QUOTA=${2:?}; shift 2 ;;
        --throughput)         THROUGHPUT=${2:?}; shift 2 ;;
        --only-quota)         ONLY_QUOTA=1; shift ;;
        --all-quotas)         ALL_QUOTAS=1; shift ;;
        --artifact-dir)       ARTIFACT_DIR=${2:?}; shift 2 ;;
        --install-pkgs)       INSTALL_PKGS=1; shift ;;
        --force)              FORCE=1; shift ;;
        -h|--help)            usage; exit 0 ;;
        *)
            case "$1" in
                *OPS|*GB/s|*MB/s|*KB/s) QUOTA=$1; shift ;;
                *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
            esac
            ;;
    esac
done

need_capacity "--quota" "$QUOTA"
need_capacity "--throughput" "$THROUGHPUT"

if [ "$EXECUTE" = "1" ] && [ -z "$APPROVED_ID" ]; then
    echo "ERROR: --execute requires --approved-change-id (main workflow approval)" >&2
    exit 2
fi
if [ "$EXECUTE" = "1" ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: --execute requires root" >&2
    exit 2
fi

say()  { printf '%s\n' "$*"; }
plan() { printf '[dry-run] %s\n' "$*"; }

# Run a command directly (argv array, no shell re-parsing): operator-supplied
# paths/values are never interpolated into a command string.
run() {
    if [ "$EXECUTE" = "1" ]; then
        printf '+'
        printf ' %q' "$@"
        printf '\n'
        "$@"
    else
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
    fi
}

write_file() {
    local path=$1
    local content
    content=$(cat)
    if [ "$EXECUTE" = "1" ]; then
        mkdir -p "$(dirname "$path")"
        printf '%s\n' "$content" > "$path"
        say "wrote $path"
    else
        plan "write $path"
        printf '%s\n' "$content" | sed 's/^/    /'
    fi
}

backup_file() {
    local path=$1 keep
    [ -f "$path" ] || return 0
    keep="$path.bak.$(date +%Y%m%dT%H%M%S)"
    say "backup: $keep (rollback: cp -p '$keep' '$path')"
    run cp -p "$path" "$keep"
}

have() { command -v "$1" >/dev/null 2>&1; }

# Convert "450OPS"/"1.5GB/s" to "<dimension> <numeric>" so two values can be
# compared only when they share a dimension (ops vs bytes/s).
capacity_token() {
    local raw=$1 num unit
    [[ $raw =~ ^([0-9]+([.][0-9]+)?)(OPS|GB/s|MB/s|KB/s)$ ]] || return 1
    num=${BASH_REMATCH[1]}
    unit=${BASH_REMATCH[3]}
    case "$unit" in
        OPS)  printf 'ops %s\n' "$num" ;;
        GB/s) printf 'bps %s\n' "$(awk -v n="$num" 'BEGIN { printf "%.0f", n * 1024 * 1024 * 1024 }')" ;;
        MB/s) printf 'bps %s\n' "$(awk -v n="$num" 'BEGIN { printf "%.0f", n * 1024 * 1024 }')" ;;
        KB/s) printf 'bps %s\n' "$(awk -v n="$num" 'BEGIN { printf "%.0f", n * 1024 }')" ;;
    esac
}

# Playbook §6 hard constraint: quota must never exceed the same algorithm's
# throughput in dev_metadata. Returns 1 only when the check fails, so it can be
# used as `check_quota_le_throughput ... || exit 2`.
check_quota_le_throughput() {
    local q=$1 t=$2 qt tt
    qt=$(capacity_token "$q") || return 0
    tt=$(capacity_token "$t") || return 0
    if [ "${qt%% *}" != "${tt%% *}" ]; then
        echo "ERROR: quota ($q) and throughput ($t) use incomparable units" >&2
        return 1
    fi
    if awk -v q="${qt#* }" -v t="${tt#* }" 'BEGIN { exit !(q + 0 > t + 0) }'; then
        echo "ERROR: quota $q exceeds throughput $t (playbook §6 hard constraint)" >&2
        return 1
    fi
    return 0
}

validate_quota_plan() {
    local scheduler=$1 metadata=$2 quota=$3 all_quotas=$4
    python3 "$SCRIPT_DIR/validate_quota_plan.py" \
        "$scheduler" "$metadata" "$quota" "$all_quotas"
}

if [ "$EXECUTE" = "0" ]; then
    say "mode: dry-run (no changes; add --execute --approved-change-id <id> to apply)"
else
    say "mode: execute (approved-change-id=$APPROVED_ID)"
fi

# --- quota-only fast path -------------------------------------------------
if [ "$ONLY_QUOTA" = "1" ]; then
    f="$ASF_DIR/scheduler.json"
    metadata="$ASF_DIR/dev_metadata/uadk_engine.json"
    [ -f "$f" ] || { echo "ERROR: not found: $f" >&2; exit 2; }
    [ -f "$metadata" ] || { echo "ERROR: not found: $metadata" >&2; exit 2; }
    count=$(grep -oE '"quota"[[:space:]]*:' "$f" 2>/dev/null | wc -l | tr -d ' ' || true)
    if [ "${count:-0}" -gt 1 ] && [ "$ALL_QUOTAS" != "1" ]; then
        echo "ERROR: $f has $count quota entries; pass --all-quotas to rewrite all of them" >&2
        exit 2
    fi
    # 按算法/设备逐项匹配 throughput；缺失、单位不可比或超限均在写盘前退出。
    validate_quota_plan "$f" "$metadata" "$QUOTA" "$ALL_QUOTAS" || exit 2
    say "current: $(grep -oE '"quota"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1)"
    # 多设备原 quota 可能不同；改写前备份完整 scheduler.json，回滚按整文件恢复。
    QUOTA_BACKUP="$f.bak.$(date +%Y%m%dT%H%M%S)"
    run cp -p "$f" "$QUOTA_BACKUP"
    say "backup: $QUOTA_BACKUP (rollback: cp -p '$QUOTA_BACKUP' '$f')"
    if [ "$ALL_QUOTAS" = "1" ]; then
        run sed -i -E "s|(\"quota\"[[:space:]]*:[[:space:]]*\")[^\"]*\"|\\1$QUOTA\"|g" "$f"
    else
        run sed -i -E "s|(\"quota\"[[:space:]]*:[[:space:]]*\")[^\"]*\"|\\1$QUOTA\"|" "$f"
    fi
    if [ "$EXECUTE" = "1" ]; then
        grep -oE '"quota"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1 || true
    else
        plan "grep quota $f"
    fi
    say "restart nginx so ASF reloads $f"
    exit 0
fi

# --- 1. uacce device ------------------------------------------------------
# uacce device ids are allocated from a global xarray, not per driver: with
# SEC/ZIP also using uacce, HPRE may be hisi_hpre-1 or higher. Always match the
# hisi_hpre-* wildcard; never assume hisi_hpre-0.
hpre_uacce_present() {
    compgen -G '/sys/class/uacce/hisi_hpre-*' >/dev/null 2>&1
}
if hpre_uacce_present; then
    say "uacce: already present ($(ls /sys/class/uacce 2>/dev/null | tr '\n' ' '))"
else
    say "uacce: missing, switching hisi_hpre to uacce_mode=2"
    run modprobe -r hisi_hpre || true
    run modprobe hisi_hpre uacce_mode=2 || true
    if [ "$EXECUTE" = "1" ] && ! hpre_uacce_present; then
        echo "ERROR: no /sys/class/uacce/hisi_hpre-* after modprobe; check HPRE VF passthrough and driver" >&2
        exit 1
    fi
fi

# --- 2. packages ----------------------------------------------------------
if [ "$INSTALL_PKGS" = "1" ]; then
    run dnf install -y cjson numactl-libs uadk_engine || true
    run dnf install -y libwd || run dnf install -y kae-driver || true
fi

# --- 3. artifacts ---------------------------------------------------------
find_src() {
    local name=$1 dir path
    [ -n "$ARTIFACT_DIR" ] || return 1
    for dir in "$ARTIFACT_DIR" "$ARTIFACT_DIR/lib" "$ARTIFACT_DIR/engines-1.1"; do
        for path in "$dir/$name"; do
            [ -f "$path" ] && { printf '%s\n' "$path"; return 0; }
        done
    done
    return 1
}

install_artifact() {
    local name=$1 dest=$2 src
    if [ -e "$dest/$name" ]; then say "present: $dest/$name"; return 0; fi
    if [ "$dest" = "/usr/lib64" ] && have ldconfig && ldconfig -p 2>/dev/null | grep -q "$name"; then
        say "present: $name (ldconfig)"; return 0
    fi
    if src=$(find_src "$name"); then
        run install -m 0755 "$src" "$dest/$name"
        [ "$EXECUTE" = "1" ] && say "installed: $dest/$name (from $src)"
        return 0
    fi
    return 1
}

missing=0
missing_names=""
note_missing() {
    missing=$((missing + 1))
    missing_names="$missing_names $1"
}
for lib in libasf.so libcjson.so.1 libnuma.so.1 libwd.so.2 libwd_crypto.so.2; do
    install_artifact "$lib" /usr/lib64 || note_missing "$lib"
done
for eng in e_sse.so uadk_engine.so; do
    install_artifact "$eng" "$ENGINE_DIR" || note_missing "$eng"
done
if have ldconfig; then run ldconfig; fi
[ "$missing" -gt 0 ] && say "missing dependencies:$missing_names (source and target dir in playbook §4)"

# --- 4. OpenSSL engine cnf ------------------------------------------------
CNF_EXISTED=0
CNF_BACKUP=""
CNF_TOUCHED=0
if [ -f "$SSL_CNF" ] && [ "$FORCE" != "1" ]; then
    say "present: $SSL_CNF (use --force to overwrite)"
else
    if [ -f "$SSL_CNF" ]; then
        CNF_EXISTED=1
        CNF_BACKUP="$SSL_CNF.bak.$(date +%Y%m%dT%H%M%S)"
        say "backup: $CNF_BACKUP (rollback: cp -p '$CNF_BACKUP' '$SSL_CNF')"
        run cp -p "$SSL_CNF" "$CNF_BACKUP"
    else
        say "new file: $SSL_CNF (rollback: rm -f '$SSL_CNF')"
    fi
    CNF_TOUCHED=1
    write_file "$SSL_CNF" <<EOF
openssl_conf=openssl_def
[openssl_def]
engines=engine_section
[engine_section]
uadk=uadk_section
e_sse=sse_section
[uadk_section]
engine_id=uadk_engine
dynamic_path=$ENGINE_DIR/uadk_engine.so
default_algorithms = RSA
[sse_section]
engine_id=e_sse
dynamic_path=$ENGINE_DIR/e_sse.so
LOG_LEVEL=0
LOG_FILE_SIZE=500
EOF
fi

# --- 5. ASF config --------------------------------------------------------
if [ -f "$ASF_DIR/scheduler.json" ] && [ "$FORCE" != "1" ]; then
    say "present: $ASF_DIR/scheduler.json (use --force to overwrite)"
else
    check_quota_le_throughput "$QUOTA" "$THROUGHPUT" || exit 2
    backup_file "$ASF_DIR/asf_conf.json"
    backup_file "$ASF_DIR/dev_metadata/uadk_engine.json"
    backup_file "$ASF_DIR/scheduler.json"
    write_file "$ASF_DIR/asf_conf.json" <<'EOF'
{
    "LOG_CONF": {
        "LOG_LEVEL": "NONE",
        "LOG_FILE_SIZE": 200,
        "LOG_DIR_PATH": "/var/log"
    }
}
EOF
    write_file "$ASF_DIR/dev_metadata/uadk_engine.json" <<EOF
{
    "id": "uadk_engine",
    "enabled": true,
    "processor": [
        {
            "name": "hpre",
            "algorithm": [
                {
                    "SN": "RSA",
                    "nid": 19,
                    "enabled": true,
                    "throughput": "$THROUGHPUT"
                }
            ]
        }
    ]
}
EOF
    write_file "$ASF_DIR/scheduler.json" <<EOF
{
    "scheduler": [
        {
            "SN": "RSA",
            "nid": 19,
            "sched_mode": "priority",
            "sched_dev": [
                {
                    "name": "uadk_engine",
                    "processor": "hpre",
                    "quota": "$QUOTA",
                    "priority": 1
                }
            ]
        }
    ]
}
EOF
fi

# --- 6. nginx ssl_engine --------------------------------------------------
# ssl_engine is a main-context directive (nginx core, not ngx_http_ssl):
# exactly one top-level "ssl_engine e_sse;" is valid. A previous version
# wrongly inserted it inside http{}, so remove any nested/stale directive and
# keep a single main-context one.
nginx_ssl_engine_ok() {
    python3 - "$NGINX_CONF" <<'PY'
import re
import sys

lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()


def code_only(line):
    out, quote, esc = [], None, False
    for ch in line:
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


ssl, depth = [], 0
for line in lines:
    if re.match(r"^[ \t]*ssl_engine\b", line):
        ssl.append((depth, line.strip()))
    code = code_only(line)
    depth += code.count("{") - code.count("}")
if (len(ssl) == 1 and ssl[0][0] == 0
        and re.fullmatch(r"ssl_engine[ \t]+e_sse[ \t]*;.*", ssl[0][1])):
    sys.exit(0)
sys.exit(1)
PY
}

patch_nginx_conf() {
    local conf=$1 tmp
    tmp=$(mktemp "${TMPDIR:-/tmp}/nginx.conf.XXXXXX")
    python3 - "$conf" "$tmp" <<'PY'
import re
import sys

source, target = sys.argv[1:3]
lines = open(source, encoding="utf-8", errors="replace").read().splitlines()


def strip_ssl_directive(line):
    """Remove ssl_engine directives but keep other config on the same line.

    nginx accepts multiple main-context directives on one line, e.g.
    ``ssl_engine old; worker_processes 8;``. Dropping the whole line would
    silently reset worker_processes, so only delete the directive up to its
    terminating semicolon and preserve the remainder.
    """
    code = line.split("#", 1)[0]
    if not re.search(r"\bssl_engine\b", code):
        return line
    prefix = re.match(r"^[ \t]*", line).group(0)
    rest = re.sub(r"\bssl_engine\b[^;]*;", "", code).strip()
    if not rest:
        return ""
    return prefix + rest


out = []
for line in lines:
    kept = strip_ssl_directive(line)
    if kept:
        out.append(kept)
index = 0
while index < len(out) and (not out[index].strip() or out[index].lstrip().startswith("#")):
    index += 1
out.insert(index, "ssl_engine e_sse;")
text = "\n".join(out)
if not text.endswith("\n"):
    text += "\n"
with open(target, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
    cat "$tmp" > "$conf"
    rm -f "$tmp"
    say "updated: single main-context ssl_engine e_sse; in $conf"
}

restore_nginx() {
    cp -p "$BACKUP" "$NGINX_CONF"
    echo "restored: $NGINX_CONF" >&2
}

restore_engine_cnf() {
    [ "${CNF_TOUCHED:-0}" = "1" ] || return 0
    if [ -n "${CNF_BACKUP:-}" ] && [ -f "$CNF_BACKUP" ]; then
        cp -p "$CNF_BACKUP" "$SSL_CNF"
        echo "restored: $SSL_CNF (from $CNF_BACKUP)" >&2
    elif [ "${CNF_EXISTED:-0}" = "0" ] && [ -e "$SSL_CNF" ]; then
        rm -f "$SSL_CNF"
        echo "removed new engine cnf: $SSL_CNF" >&2
    fi
}

validate_nginx() {
    local bin="$NGINX_BIN" t_out t_rc
    [ -z "$bin" ] && [ -x "$NGX_PREFIX/sbin/nginx" ] && bin="$NGX_PREFIX/sbin/nginx"
    [ -z "$bin" ] && bin=$(command -v nginx 2>/dev/null || true)
    if [ -z "$bin" ] || [ ! -x "$bin" ]; then
        echo "ERROR: no nginx binary available to validate $NGINX_CONF; restoring backup" >&2
        echo "       pass --nginx-bin <path> so the patched config can be checked before restart" >&2
        restore_nginx
        restore_engine_cnf
        exit 3
    fi
    t_out=$(LD_LIBRARY_PATH="$OPENSSL_DIR/lib:${LD_LIBRARY_PATH:-}" \
            OPENSSL_CONF="$SSL_CNF" OPENSSL_ENGINES="$ENGINE_DIR" \
            "$bin" -t -c "$NGINX_CONF" 2>&1) && t_rc=0 || t_rc=$?
    if [ "$t_rc" = "0" ]; then
        say "nginx -t passed: $bin"
        return 0
    fi
    if printf '%s' "$t_out" | grep -qi 'error while loading shared libraries'; then
        echo "ERROR: cannot run $bin -t (missing runtime libraries); config not validated, restoring backup" >&2
        printf '%s\n' "$t_out" | sed 's/^/    /' >&2
        restore_nginx
        restore_engine_cnf
        exit 3
    fi
    echo "ERROR: nginx -t failed for $NGINX_CONF; restoring $BACKUP" >&2
    printf '%s\n' "$t_out" | sed 's/^/    /' >&2
    restore_nginx
    restore_engine_cnf
    exit 1
}

if [ -f "$NGINX_CONF" ]; then
    if nginx_ssl_engine_ok; then
        say "present: single main-context ssl_engine e_sse in $NGINX_CONF"
    else
        BACKUP="$NGINX_CONF.bak.$(date +%Y%m%dT%H%M%S)"
        if [ "$EXECUTE" = "1" ]; then
            say "backup: $BACKUP (rollback: cp -p '$BACKUP' '$NGINX_CONF')"
            run cp -p "$NGINX_CONF" "$BACKUP"
            patch_nginx_conf "$NGINX_CONF"
            validate_nginx
        else
            plan "ensure exactly one main-context 'ssl_engine e_sse;' in $NGINX_CONF"
            plan "remove any nested/wrong ssl_engine directive (main context only)"
            plan "nginx -t -c '$NGINX_CONF' (after apply; failure restores the backup)"
        fi
    fi
else
    say "SKIP: nginx config not found: $NGINX_CONF"
fi

# --- 7. self check --------------------------------------------------------
if [ "$EXECUTE" = "1" ]; then
    export LD_LIBRARY_PATH="$OPENSSL_DIR/lib:${LD_LIBRARY_PATH:-}"
    export OPENSSL_ENGINES="$ENGINE_DIR"
    export OPENSSL_CONF="$SSL_CNF"
    for so in "$ENGINE_DIR/e_sse.so" "$ENGINE_DIR/uadk_engine.so"; do
        if [ -f "$so" ]; then
            miss=$(ldd "$so" 2>/dev/null | awk '/not found/{print $1}' | paste -sd' ' -)
            [ -n "$miss" ] && { echo "ERROR: $so missing runtime libraries: $miss" >&2; exit 1; }
        fi
    done
    engine_available() {
        local eng=$1 out
        [ -x "$OPENSSL_DIR/bin/openssl" ] || return 1
        if ! out=$("$OPENSSL_DIR/bin/openssl" engine -t "$eng" 2>&1); then
            return 1
        fi
        # "[ unavailable ]" must not satisfy this check.
        printf '%s\n' "$out" | grep -qE '^[[:space:]]*\[ available \][[:space:]]*$'
    }
    for eng in uadk_engine e_sse; do
        if engine_available "$eng"; then
            say "engine available: $eng"
        else
            echo "ERROR: engine unavailable: $eng (check $SSL_CNF, $ENGINE_DIR, ldd)" >&2
            exit 1
        fi
    done
    say "done; restart nginx to load engine settings"
else
    plan "verify with: bash verify_hpre_offload.sh --requests 50"
fi

if [ "$missing" -gt 0 ]; then
    say "WARN: $missing artifact(s) missing; enablement incomplete"
    [ "$EXECUTE" = "1" ] && exit 1
fi

exit 0
