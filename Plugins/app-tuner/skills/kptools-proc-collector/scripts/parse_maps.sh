#!/bin/bash
# /proc/PID/maps parser — aggregates mappings by DSO path and classifies them
set -euo pipefail

print_help() {
    cat <<'EOF'
parse_maps — Parse /proc/PID/maps into a DSO list with classification

Usage:
  parse_maps.sh -p <PID>            read /proc/PID/maps directly
  parse_maps.sh -f <maps_file>      parse a saved maps file

Output (stdout): JSON array sorted by mapped size desc:
  [{"path": "...", "size_kb": 123, "type": "main_binary|libc|third_party|other"}]
type rules: same path as /proc/PID/exe -> main_binary; libc/ld- -> libc;
*.so* -> third_party; others -> other.
EOF
    exit 0
}

PID=""
FILE=""
# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -p|-f) echo "ERROR: option ${!#} requires a value" >&2; exit 1 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="$2"; shift 2 ;;
        -f) FILE="$2"; shift 2 ;;
        --help) print_help ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [ -n "$FILE" ]; then
    if [ ! -r "$FILE" ]; then
        echo "ERROR: cannot read $FILE" >&2
        exit 1
    fi
    MAPS=$(cat "$FILE")
elif [ -n "$PID" ]; then
    if [ ! -r "/proc/$PID/maps" ]; then
        echo "ERROR: cannot read /proc/$PID/maps. Cross-uid target: run as the same user or root." >&2
        exit 1
    fi
    MAPS=$(cat "/proc/$PID/maps")
else
    echo "ERROR: -p <PID> or -f <file> required" >&2
    exit 1
fi

EXE=$(readlink -f "/proc/$PID/exe" 2>/dev/null || echo "")

# strtonum 是 gawk 扩展，mawk/busybox awk 下直接报错——改用可移植的十六进制转换
echo "$MAPS" | awk -v exe="$EXE" '
    function hex2dec(h,   i, c, d, n) {
        sub(/^0[xX]/, "", h)
        n = 0
        for (i = 1; i <= length(h); i++) {
            c = tolower(substr(h, i, 1))
            d = index("0123456789abcdef", c) - 1
            if (d < 0) return 0
            n = n * 16 + d
        }
        return n
    }
    NF >= 6 && $6 !~ /^\[/ {
        split($1, se, "-");
        size = hex2dec(se[2]) - hex2dec(se[1]);
        total[$6] += size;
        seen[$6] = 1;
    }
    END {
        n = 0;
        for (p in seen) { paths[n++] = p }
        # sort by total desc (simple insertion sort, mapping counts are small)
        for (i = 1; i < n; i++) {
            key = total[paths[i]]; kp = paths[i];
            j = i - 1;
            while (j >= 0 && total[paths[j]] < key) { paths[j+1] = paths[j]; total[paths[j+1]] = total[paths[j]]; j-- }
            paths[j+1] = kp; total[paths[j+1]] = key;
        }
        printf "[";
        first = 1;
        for (i = 0; i < n; i++) {
            p = paths[i];
            if (exe != "" && p == exe) t = "main_binary";
            else if (p ~ /\/libc(-[0-9.]+)?\.so|\/ld-/) t = "libc";
            else if (p ~ /\.so/) t = "third_party";
            else t = "other";
            if (!first) printf ", ";
            printf "{\"path\": \"%s\", \"size_kb\": %d, \"type\": \"%s\"}", p, int(total[p]/1024), t;
            first = 0;
        }
        printf "]\n";
    }'
