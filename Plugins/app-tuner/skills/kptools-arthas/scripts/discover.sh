#!/bin/bash
# arthas discover — 检查 java/arthas 环境与目标 JVM attach 可行性
set -euo pipefail

print_help() {
    cat <<'EOF'
arthas discover — Check java/arthas availability and attach environment

Usage:
  discover.sh [--pid <PID>]

Options:
  --pid <PID>   Check a specific target JVM (language, uid, attach feasibility)

Output:
  java: READY (<path>, <version>) / NOT_FOUND
  jps: <path> / NOT_FOUND（用 ps 兜底）
  arthas-boot.jar: READY (<path>) / NOT_INSTALLED (install hint)
  java 进程列表（--pid 时只检查目标进程）

  exit 0（报告式，采集可行性由调用方结合各状态行判断）
EOF
    exit 0
}

CHECK_PID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pid) CHECK_PID="$2"; shift 2 ;;
        --help|-h) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

# java 检查
if JAVA_BIN=$(command -v java 2>/dev/null || true); [ -n "${JAVA_BIN:-}" ]; then
    JAVA_VER=$(java -version 2>&1 | head -1 || echo "unknown")
    echo "java: READY (${JAVA_BIN}, ${JAVA_VER})"
else
    echo "java: NOT_FOUND（需 JDK 8-21，容器内安装 java-17-openjdk 或挂载宿主 JDK）"
fi

# jps 检查（仅提示；缺失时用 ps 兜底）
if command -v jps &>/dev/null; then
    echo "jps: $(command -v jps)"
else
    echo "jps: NOT_FOUND（用 ps 兜底列 java 进程）"
fi

# arthas-boot.jar 搜索（与 collect.sh 同序）
JAR_CANDIDATES=("${ARTHAS_BOOT_JAR:-}" "$HOME/.arthas/arthas-boot.jar" "/opt/arthas/arthas-boot.jar")
JAR_FOUND=""
for c in "${JAR_CANDIDATES[@]}"; do
    if [ -n "$c" ] && [ -f "$c" ]; then JAR_FOUND="$c"; break; fi
done
if [ -n "$JAR_FOUND" ]; then
    echo "arthas-boot.jar: READY (${JAR_FOUND})"
else
    echo "arthas-boot.jar: NOT_INSTALLED (bash scripts/deploy.sh 在线安装；离线见 references/arthas-guide.md §2.3)"
fi

# 目标进程检查
if [ -n "$CHECK_PID" ]; then
    if ! kill -0 "$CHECK_PID" 2>/dev/null; then
        echo "target(pid=${CHECK_PID}): NOT_ALIVE"
        exit 0
    fi
    if [ -r "/proc/$CHECK_PID/cmdline" ] && tr '\0' ' ' < "/proc/$CHECK_PID/cmdline" | grep -qiE 'java'; then
        echo "target(pid=${CHECK_PID}): JVM（$(tr '\0' ' ' < /proc/$CHECK_PID/cmdline | cut -c1-80)）"
    else
        echo "target(pid=${CHECK_PID}): 非 Java 进程 —— 语言路由：Python→kptools-py-spy，Node.js→kptools-node-profiler，C/C++→kptools-devkit tuner hotspot"
        exit 0
    fi
    if [ -r "/proc/$CHECK_PID/status" ]; then
        TUID=$(awk '/^Uid:/{print $2}' "/proc/$CHECK_PID/status" 2>/dev/null || echo "")
        MUID=$(id -u)
        if [ -n "$TUID" ]; then
            if [ "$TUID" = "$MUID" ] || [ "$MUID" = "0" ]; then
                echo "attach: 可行（uid 匹配或 root）"
            else
                echo "attach: 受限（目标 uid=${TUID} ≠ 当前 uid=${MUID}；需同 uid 或 root）"
            fi
        fi
    fi
    exit 0
fi

# java 进程列表
echo "java 进程:"
if command -v jps &>/dev/null; then
    jps -l 2>/dev/null | grep -viE '^(.* )?Jps$' || echo "  （无）"
else
    ps -eo pid,args 2>/dev/null | awk '$0 ~ /[j]ava/ {printf "  %s %s\n", $1, substr($0, index($0,$2), 100)}' || echo "  （无）"
fi
