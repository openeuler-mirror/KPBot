#!/bin/bash
# node-profiler collect — V8 日志解析 / cpuprofile 定位 / 注入指引
# 能力边界：V8 采样器不支持 attach 运行中进程，采集需重启注入（--guide 输出指引）
set -euo pipefail

MODE=""
LOG_FILE=""
SCAN_DIR=""
GUIDE_PID=""

print_help() {
    cat <<'EOF'
kptools-node-profiler collect — Node.js V8 profiling log parsing and injection guide

Modes:
  --parse -f <isolate-*.log>     node --prof-process 解析单个 V8 日志
  --parse-dir -d <dir>           解析目录内全部 isolate-*.log（多 worker）
  --cpuprofile -d <dir>          定位 *.cpuprofile 产物并输出可视化指引
  --guide -p <PID>               输出针对目标服务的注入指引（node --prof / --cpu-prof / 0x）

Constraints:
  V8 采样器无 attach 能力：对运行中进程只输出注入指引，不伪造采集结果
  stdout: 解析文本 / 文件清单 / 指引；stderr: 诊断
  exit 0: 成功；2: 依赖缺失；3: 参数错误
  解析产物同时存档至 ./node-profiler-evidence/<时间戳>/
EOF
    exit 0
}

if [ $# -gt 0 ]; then
    case "${!#}" in
        -f|-d|-p) echo "ERROR: option ${!#} requires a value" >&2; exit 3 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --parse) MODE="parse"; shift ;;
        --parse-dir) MODE="parse_dir"; shift ;;
        --cpuprofile) MODE="cpuprofile"; shift ;;
        --guide) MODE="guide"; shift ;;
        -f) LOG_FILE="$2"; shift 2 ;;
        -d) SCAN_DIR="$2"; shift 2 ;;
        -p) GUIDE_PID="$2"; shift 2 ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "ERROR: 需要一个模式参数（--parse/--parse-dir/--cpuprofile/--guide，见 --help）" >&2
    exit 3
fi

if ! command -v node &>/dev/null; then
    echo "ERROR: node not found（https://nodejs.org）" >&2
    exit 2
fi

EVIDENCE_DIR="./node-profiler-evidence/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$EVIDENCE_DIR"

case "$MODE" in
    parse)
        if [ -z "$LOG_FILE" ] || [ ! -r "$LOG_FILE" ]; then
            echo "ERROR: 无法读取文件: ${LOG_FILE:-<empty>}（日志由 node --prof 启动注入生成，见 --guide）" >&2
            exit 3
        fi
        echo "## $(basename "$LOG_FILE")"
        node --prof-process "$LOG_FILE" 2>/dev/null | tee "$EVIDENCE_DIR/$(basename "$LOG_FILE").txt"
        echo ""
        echo "evidence: $EVIDENCE_DIR/$(basename "$LOG_FILE").txt" >&2
        ;;
    parse_dir)
        if [ -z "$SCAN_DIR" ] || [ ! -d "$SCAN_DIR" ]; then
            echo "ERROR: 无法读取目录: ${SCAN_DIR:-<empty>}" >&2
            exit 3
        fi
        shopt -s nullglob
        LOGS=("$SCAN_DIR"/isolate-*.log)
        shopt -u nullglob
        if [ ${#LOGS[@]} -eq 0 ]; then
            echo "ERROR: $SCAN_DIR 下未发现 isolate-*.log（日志由 node --prof 启动注入生成，见 --guide）" >&2
            exit 3
        fi
        for log in "${LOGS[@]}"; do
            echo "## $(basename "$log")"
            node --prof-process "$log" 2>/dev/null | tee "$EVIDENCE_DIR/$(basename "$log").txt"
            echo ""
        done
        echo "parsed ${#LOGS[@]} logs" >&2
        echo "evidence: $EVIDENCE_DIR" >&2
        ;;
    cpuprofile)
        if [ -z "$SCAN_DIR" ] || [ ! -d "$SCAN_DIR" ]; then
            echo "ERROR: 无法读取目录: ${SCAN_DIR:-<empty>}" >&2
            exit 3
        fi
        shopt -s nullglob
        PROFILES=("$SCAN_DIR"/*.cpuprofile)
        shopt -u nullglob
        if [ ${#PROFILES[@]} -eq 0 ]; then
            echo "ERROR: $SCAN_DIR 下未发现 *.cpuprofile（由 node --cpu-prof 注入运行生成，见 --guide）" >&2
            exit 3
        fi
        for pf in "${PROFILES[@]}"; do
            echo "cpuprofile: $pf ($(wc -c < "$pf") bytes)"
        done
        echo "visualization: 将 .cpuprofile 拖入 Chrome DevTools → Performance 面板，或使用 speedscope（npm i -g speedscope && speedscope <file>）"
        echo "evidence: $EVIDENCE_DIR" >&2
        ;;
    guide)
        if [ -z "$GUIDE_PID" ]; then
            echo "ERROR: --guide 需要 -p <PID>（用于生成含 PID 的注入命令模板）" >&2
            exit 3
        fi
        cat <<EOF
Node.js V8 profiling 注入指引（目标 PID: $GUIDE_PID）

说明：V8 采样器不支持对运行中进程 attach，需以下列方式之一重启/启动服务：

1. node --prof（内置，零依赖；生成 isolate-*.log 于进程 CWD）:
   node --prof server.js
   # 采集完成后解析: collect.sh --parse -f isolate-*.log

2. --cpu-prof（Node ≥ 12，生成 Chrome DevTools 格式 .cpuprofile）:
   node --cpu-prof --cpu-prof-dir /tmp/cpuprof server.js
   # 产物定位: collect.sh --cpuprofile -d /tmp/cpuprof

3. 0x（可选增强，直接生成交互式火焰图）:
   npm install -g 0x
   0x -o /tmp/flamegraph node server.js

注意事项:
  - kill -USR2 仅翻转 V8 日志，不能开启 CPU profiling（勿用于采集）
  - --prof 采样开销约 1ms 间隔，高 QPS 网关请在低峰采集
  - 容器场景确认 isolate-*.log 所在目录已挂载
  - --cpu-prof 产物在优雅退出时写出，强杀进程可能丢失
  - 目标进程当前命令行（确认注入方式）: tr '\0' ' ' < /proc/$GUIDE_PID/cmdline
EOF
        ;;
esac
