#!/bin/bash
# =============================================================================
# ARM SPE 数据 → 标准 CSV（支持接入外部开源 spe-parser）
# arm-spe-analysis skill - scripts/spe-csv.sh
# 用途：把 perf.data 解析为统一 schema 的 CSV，供深度时延分析/报告使用。
#       - 默认解析器：perf 内核 SPE 解码器（perf script）
#       - 外部解析器：--parser 接入任意开源 spe-parser（需输出标准 CSV）
# 用法：
#   bash spe-csv.sh spe.data                      # 输出 CSV 到 stdout
#   bash spe-csv.sh -o samples.csv spe.data       # 输出到文件
#   bash spe-csv.sh --validate samples.csv        # 校验 CSV schema
#   bash spe-csv.sh --parser "spe-parser -c" spe.data > samples.csv
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONVERTER="$SCRIPT_DIR/spe-to-csv.py"

# perf script 输出字段（顺序与 spe-to-csv.py 的解析约定一致）
FULL_FIELDS="comm,pid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr"

DATA_FILE=""
OUTPUT=""
PARSER=""
VALIDATE=""
CHECK_ONLY=false

usage() {
    cat <<EOF
spe-csv.sh — ARM SPE 数据解析为标准 CSV

用法:
  bash spe-csv.sh [选项] <perf.data>
  bash spe-csv.sh --validate <samples.csv>

选项:
  -o, --output FILE     输出 CSV 文件（默认 stdout）
  --parser CMD          使用外部开源 spe-parser：CMD 从 stdin 读 perf script
                        输出、向 stdout 写标准 CSV（默认使用内置 perf 解析）
  --validate FILE       校验已有 CSV 是否符合标准 schema
  --schema              打印标准 CSV schema 说明
  -h, --help            帮助

标准 CSV 列:
  comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr

示例:
  bash spe-csv.sh -o /tmp/samples.csv spe.data
  bash spe-csv.sh --parser "python3 /opt/spe-parser/parse.py" spe.data > samples.csv
  bash spe-csv.sh --validate /tmp/samples.csv
  bash spe-csv.sh --schema
EOF
    exit 0
}

warn() { echo "[warn] $*" >&2; }
err()  { echo "[error] $*" >&2; }

# 用内置 perf 解析器：优先带 weight/data_src/addr，失败回退基础字段
parse_with_perf() {
    local data="$1"
    if perf script -F "$FULL_FIELDS" -i "$data" 2>/dev/null | python3 "$CONVERTER"; then
        return 0
    fi
    warn "perf 不支持 weight/data_src/addr 字段，回退为基础字段（时延/数据源将为空）"
    perf script -i "$data" | python3 "$CONVERTER"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)   OUTPUT="$2"; shift 2 ;;
        --parser)      PARSER="$2"; shift 2 ;;
        --validate)    VALIDATE="$2"; shift 2 ;;
        --schema)      python3 "$CONVERTER" --schema; exit 0 ;;
        -h|--help)     usage ;;
        -*)            err "未知选项: $1"; usage ;;
        *)             DATA_FILE="$1"; shift ;;
    esac
done

# 校验模式
if [ -n "$VALIDATE" ]; then
    if [ ! -f "$VALIDATE" ]; then
        err "文件不存在: $VALIDATE"
        exit 1
    fi
    python3 "$CONVERTER" --validate "$VALIDATE"
    exit $?
fi

if [ -z "$DATA_FILE" ]; then
    err "请指定 perf.data 文件"
    usage
fi
if [ ! -f "$DATA_FILE" ]; then
    err "文件不存在: $DATA_FILE"
    exit 1
fi
if ! command -v python3 &>/dev/null; then
    err "python3 不可用（spe-to-csv.py 依赖）"
    exit 1
fi

CSV_TMP="$(mktemp)"
trap 'rm -f "$CSV_TMP"' EXIT

if [ -n "$PARSER" ]; then
    # 外部开源 spe-parser：约定 stdin=perf script 输出，stdout=标准 CSV
    if ! perf script -i "$DATA_FILE" | eval "$PARSER" > "$CSV_TMP"; then
        err "外部 spe-parser 执行失败: $PARSER"
        exit 1
    fi
    # 校验外部输出，避免非标准 CSV 流入下游
    if ! python3 "$CONVERTER" --validate "$CSV_TMP" >/dev/null 2>&1; then
        err "外部 spe-parser 输出不符合标准 CSV schema，请用 --schema 查看格式"
        exit 1
    fi
else
    parse_with_perf "$DATA_FILE" > "$CSV_TMP"
fi

if [ -n "$OUTPUT" ]; then
    cp "$CSV_TMP" "$OUTPUT"
    echo "CSV 已写入: $OUTPUT ($(($(wc -l < "$OUTPUT") - 1)) 条样本)"
else
    cat "$CSV_TMP"
fi
