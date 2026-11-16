#!/bin/bash
# arthas collect — Arthas 批处理性能分析采集统一入口（只读证据采集）
set -euo pipefail

PID=""
CMDS=""
PRESET=""
OUT_DIR=""
JAR_OVERRIDE=""
TIMEOUT_SEC=120
DO_SHUTDOWN=0
TELNET_PORT=""

print_help() {
    cat <<'EOF'
kptools-arthas collect — Batch performance-analysis evidence collection via Arthas

Usage:
  collect.sh -p <PID> (-c "<cmds>" | --preset <name>) [options]

Options:
  -p <PID>            目标 Java 进程（必填）
  -c "<cmds>"         arthas 命令串，';' 分隔多条；逐条 attach 执行。
                      注意：单条命令内不要使用含 ';' 的 OGNL 表达式
  --preset <name>     预设场景（与 -c 二选一）：
                        cpu      = dashboard -n 1; thread -n 5
                        blocked  = thread --state BLOCKED; thread -b
                        overview = dashboard -n 1; memory; vmopt
  -o <dir>            证据目录（默认 ./arthas-evidence/<时间戳>/）
  --jar <path>        arthas-boot.jar 路径（默认搜索 $ARTHAS_BOOT_JAR → ~/.arthas/ → /opt/arthas/）
  --timeout <sec>     单条命令 attach 超时（默认 120；首次 attach 下载完整包时可调大）
  --telnet-port <n>   指定 arthas telnet 端口（默认 3658；被其他进程的 arthas 会话
                      占用时未指定则自动探测并换随机端口，目标进程自身占用则复用）
  --shutdown          结束后 best-effort 执行 shutdown 释放 arthas 服务端
  --help              Show this help

Notes:
  stdout: 证据文件路径
  stderr: 语言路由/uid 告警、端口自动切换提示、单条命令失败清单
  exit 0: 成功（部分命令失败仍为 0，失败清单见 stderr）；2: 依赖缺失（java/arthas-boot.jar）；
          3: 参数错误；1: 全部命令失败
EOF
    exit 0
}

# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -p|-c|-o|--preset|--jar|--timeout|--telnet-port) echo "ERROR: option ${!#} requires a value" >&2; exit 3 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="$2"; shift 2 ;;
        -c) CMDS="$2"; shift 2 ;;
        -o) OUT_DIR="$2"; shift 2 ;;
        --preset) PRESET="$2"; shift 2 ;;
        --jar) JAR_OVERRIDE="$2"; shift 2 ;;
        --timeout) TIMEOUT_SEC="$2"; shift 2 ;;
        --telnet-port) TELNET_PORT="$2"; shift 2 ;;
        --shutdown) DO_SHUTDOWN=1; shift ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

[ -n "$PID" ] || { echo "ERROR: -p <PID> is required" >&2; exit 3; }
[[ "$PID" =~ ^[0-9]+$ ]] || { echo "ERROR: -p 必须为数字 PID" >&2; exit 3; }
if [ -n "$CMDS" ] && [ -n "$PRESET" ]; then
    echo "ERROR: -c 与 --preset 二选一" >&2; exit 3
fi
if [ -z "$CMDS" ] && [ -z "$PRESET" ]; then
    echo "ERROR: 需要 -c \"<cmds>\" 或 --preset <name>" >&2; exit 3
fi
[[ "$TIMEOUT_SEC" =~ ^[0-9]+$ ]] || { echo "ERROR: --timeout 必须为正整数" >&2; exit 3; }

if [ -n "$PRESET" ]; then
    case "$PRESET" in
        cpu)      CMDS="dashboard -n 1; thread -n 5" ;;
        blocked)  CMDS="thread --state BLOCKED; thread -b" ;;
        overview) CMDS="dashboard -n 1; memory; vmopt" ;;
        *) echo "ERROR: --preset 仅支持 cpu|blocked|overview" >&2; exit 3 ;;
    esac
fi

# 目标进程存活与语言路由（/proc 检查零开销，先于依赖检查；非 Java 进程直接拒绝，不浪费 attach）
if ! kill -0 "$PID" 2>/dev/null; then
    echo "ERROR: 目标进程（pid ${PID}）不存在或已退出" >&2
    exit 1
fi
if [ -r "/proc/$PID/cmdline" ] && ! tr '\0' ' ' < "/proc/$PID/cmdline" | grep -qiE 'java'; then
    echo "ERROR: 目标进程（pid ${PID}）不是 Java 进程；请先语言路由：Python→kptools-py-spy，Node.js→kptools-node-profiler，C/C++→kptools-devkit tuner hotspot" >&2
    exit 1
fi

[ "$TELNET_PORT" != "" ] && { [[ "$TELNET_PORT" =~ ^[0-9]+$ ]] || { echo "ERROR: --telnet-port 必须为数字端口" >&2; exit 3; }; }

# 依赖：java
JAVA_BIN="${JAVA_BIN:-$(command -v java 2>/dev/null || true)}"
if [ -z "${JAVA_BIN:-}" ] || [ ! -x "$JAVA_BIN" ]; then
    echo "ERROR: java not found. 安装 JDK 或运行 bash scripts/discover.sh 检查环境" >&2
    exit 2
fi

# 依赖：arthas-boot.jar（搜索顺序与 discover.sh 一致；显式 --jar 不存在时直接报错，不静默回退）
find_jar() {
    local c
    for c in "${JAR_OVERRIDE}" "${ARTHAS_BOOT_JAR:-}" "$HOME/.arthas/arthas-boot.jar" "/opt/arthas/arthas-boot.jar"; do
        if [ -n "$c" ] && [ -f "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}
if [ -n "$JAR_OVERRIDE" ]; then
    [ -f "$JAR_OVERRIDE" ] || { echo "ERROR: --jar 指定的文件不存在: ${JAR_OVERRIDE}" >&2; exit 2; }
    JAR="$JAR_OVERRIDE"
else
    JAR=$(find_jar) || { echo "ERROR: arthas-boot.jar not found. 运行 bash scripts/deploy.sh 安装，或 --jar 指定路径" >&2; exit 2; }
fi

# uid 预检（仅告警，root 不受限）
if [ -r "/proc/$PID/status" ]; then
    TUID=$(awk '/^Uid:/{print $2}' "/proc/$PID/status" 2>/dev/null || echo "")
    MUID=$(id -u)
    if [ -n "$TUID" ] && [ "$TUID" != "$MUID" ] && [ "$MUID" != "0" ]; then
        echo "NOTICE: 目标 JVM uid=${TUID} 与当前 uid=${MUID} 不同，attach 将被拒绝（同 uid 或 root 执行）" >&2
    fi
fi

# telnet 端口决策：默认 3658 被其他进程的 arthas 会话占用时自动换随机端口；目标进程自身占用则复用
port_in_use() {
    timeout 1 bash -c "echo > /dev/tcp/127.0.0.1/$1" 2>/dev/null
}
PORT_ARGS=()
if [ -n "$TELNET_PORT" ]; then
    PORT_ARGS=(--telnet-port "$TELNET_PORT" --http-port -1)
elif port_in_use 3658; then
    OWNER_PID=$(ss -ltnp 2>/dev/null | awk '$4 ~ /:3658$/ {print $0}' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    if [ -z "$OWNER_PID" ] || [ "$OWNER_PID" != "$PID" ]; then
        NEW_PORT=""
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            CAND=$((20000 + RANDOM % 30000))
            if ! port_in_use "$CAND"; then NEW_PORT="$CAND"; break; fi
        done
        if [ -n "$NEW_PORT" ]; then
            echo "NOTICE: 默认 telnet 端口 3658 被其他进程（${OWNER_PID:-owner 未知}）的 arthas 会话占用，自动改用 --telnet-port ${NEW_PORT}" >&2
            PORT_ARGS=(--telnet-port "$NEW_PORT" --http-port -1)
        else
            echo "NOTICE: 默认 telnet 端口 3658 被占用且随机端口探测失败，仍将尝试默认端口" >&2
        fi
    fi
fi

# 命令串按 ';' 拆分（含空项过滤与去空白）
CMDS_LIST=()
OLD_IFS="$IFS"
IFS=";"
for c in ${CMDS}; do
    IFS="$OLD_IFS"
    c_trim="$(echo "$c" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -n "$c_trim" ] && CMDS_LIST+=("$c_trim")
done
IFS="$OLD_IFS"
[ "${#CMDS_LIST[@]}" -gt 0 ] || { echo "ERROR: 命令串为空" >&2; exit 3; }

# 证据目录
if [ -z "$OUT_DIR" ]; then
    OUT_DIR="./arthas-evidence/$(date +%Y%m%d_%H%M%S)"
fi
mkdir -p "$OUT_DIR" || { echo "ERROR: 证据目录不可创建: $OUT_DIR" >&2; exit 3; }
EVID_FILE="$OUT_DIR/arthas_batch_${PID}_$(date +%Y%m%d_%H%M%S).txt"

{
    echo "# arthas batch evidence"
    echo "# pid: ${PID}"
    echo "# jar: ${JAR}"
    echo "# commands: ${CMDS}"
    echo "# started: $(date '+%F %T')"
    echo
} > "$EVID_FILE"

run_arthas() {
    # $1 = 命令；输出与错误都追加进证据文件；返回 arthas 退出码
    local cmd="$1" rc
    local -a run_cmd=("$JAVA_BIN" -jar "$JAR" "$PID" -b -c "$cmd")
    [ "${#PORT_ARGS[@]}" -gt 0 ] && run_cmd+=("${PORT_ARGS[@]}")
    if command -v timeout &>/dev/null; then
        set +e; timeout "$TIMEOUT_SEC" "${run_cmd[@]}" >> "$EVID_FILE" 2>&1; rc=$?; set -e
    else
        set +e; "${run_cmd[@]}" >> "$EVID_FILE" 2>&1; rc=$?; set -e
    fi
    return $rc
}

TOTAL=0; FAILED=0
for c in "${CMDS_LIST[@]}"; do
    TOTAL=$((TOTAL + 1))
    echo "=== CMD: $c ===" >> "$EVID_FILE"
    if run_arthas "$c"; then
        echo "  [ok] $c" >&2
    else
        rc=$?
        FAILED=$((FAILED + 1))
        echo "  [WARN] 命令失败 (exit ${rc}): $c" >&2
        if [ "$rc" = "124" ]; then
            echo "         attach 超时：首次 attach 在下载完整包（无网环境须先 deploy.sh --prewarm），或 arthas 端口驻留冲突（见 SKILL.md 错误处理）" >&2
        fi
    fi
    echo >> "$EVID_FILE"
done

if [ "$DO_SHUTDOWN" = "1" ]; then
    echo "=== CMD: shutdown (best-effort) ===" >> "$EVID_FILE"
    if run_arthas "shutdown"; then
        echo "  [ok] shutdown（arthas 服务端已释放）" >&2
    else
        echo "  [NOTICE] shutdown 未成功（服务端可能未驻留，无需处理）" >&2
    fi
    echo >> "$EVID_FILE"
fi

if [ "$FAILED" -eq "$TOTAL" ]; then
    echo "ERROR: 全部 ${TOTAL} 条命令失败，证据见 ${EVID_FILE}" >&2
    exit 1
fi
[ "$FAILED" -gt 0 ] && echo "NOTICE: ${FAILED}/${TOTAL} 条命令失败，其余成功；失败原因见 stderr 与证据文件" >&2
echo "$EVID_FILE"
