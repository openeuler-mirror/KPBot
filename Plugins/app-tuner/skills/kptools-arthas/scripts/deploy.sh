#!/bin/bash
# arthas deploy — 检查/安装 arthas-boot.jar（Apache-2.0 开源工具）
set -euo pipefail

print_help() {
    cat <<'EOF'
arthas deploy — Check/install arthas-boot.jar

Behavior:
  - 已在标准路径（$ARTHAS_BOOT_JAR / ~/.arthas/ / /opt/arthas/）→ 报告并退出 0
  - 未安装且非 --prewarm：下载 arthas-boot.jar 到 ~/.arthas/（来源可用 ARTHAS_BOOT_URL 覆盖）
  - --prewarm <PID>：对一个可用 JVM 执行一次批处理 attach，
    把完整 arthas 包缓存到 ~/.arthas/（离线环境关键步骤，见 arthas-guide §2.3）

  exit 0: 成功或已安装；1: 下载/执行失败；3: 参数错误
EOF
    exit 0
}

PREWARM_PID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --prewarm) PREWARM_PID="$2"; shift 2 ;;
        --help|-h) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

JAR_CANDIDATES=("${ARTHAS_BOOT_JAR:-}" "$HOME/.arthas/arthas-boot.jar" "/opt/arthas/arthas-boot.jar")
find_jar() {
    local c
    for c in "${JAR_CANDIDATES[@]}"; do
        if [ -n "$c" ] && [ -f "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}

if JAR=$(find_jar); then
    echo "arthas-boot.jar: already installed (${JAR})"
else
    if [ -n "$PREWARM_PID" ]; then
        echo "ERROR: --prewarm 需先安装 arthas-boot.jar（先无参执行本脚本）" >&2
        exit 1
    fi
    URL="${ARTHAS_BOOT_URL:-https://arthas.aliyun.com/arthas-boot.jar}"
    DEST_DIR="$HOME/.arthas"
    mkdir -p "$DEST_DIR"
    echo "downloading arthas-boot.jar from ${URL} ..."
    if command -v curl &>/dev/null; then
        curl -fsSL -o "$DEST_DIR/arthas-boot.jar" "$URL" || { echo "ERROR: curl 下载失败（检查网络或用 ARTHAS_BOOT_URL 指定镜像）" >&2; exit 1; }
    elif command -v wget &>/dev/null; then
        wget -q -O "$DEST_DIR/arthas-boot.jar" "$URL" || { echo "ERROR: wget 下载失败（检查网络或用 ARTHAS_BOOT_URL 指定镜像）" >&2; exit 1; }
    else
        echo "ERROR: 需要 curl 或 wget 下载；离线环境手动放置 arthas-boot.jar 到 ~/.arthas/ 或 /opt/arthas/" >&2
        exit 1
    fi
    [ -s "$DEST_DIR/arthas-boot.jar" ] || { echo "ERROR: 下载文件为空" >&2; exit 1; }
    echo "arthas-boot.jar: installed ($DEST_DIR/arthas-boot.jar)"
    echo "NOTICE: 首次 attach 会从 maven 仓库拉取完整 arthas 包缓存到 ~/.arthas/；完全离线环境先执行: bash scripts/deploy.sh --prewarm <PID>" >&2
fi

if [ -n "$PREWARM_PID" ]; then
    if ! kill -0 "$PREWARM_PID" 2>/dev/null; then
        echo "ERROR: prewarm 目标 PID=${PREWARM_PID} 不存在" >&2
        exit 1
    fi
    if ! command -v java &>/dev/null; then
        echo "ERROR: 本机无 java，无法 prewarm" >&2
        exit 1
    fi
    JAR=$(find_jar)
    echo "prewarming: attach pid=${PREWARM_PID} 执行一次批处理 ..."
    set +e
    timeout 180 java -jar "$JAR" "$PREWARM_PID" -b -c "version"
    RC=$?
    set -e
    if [ "$RC" -ne 0 ]; then
        echo "ERROR: prewarm 失败（exit ${RC}）：确认目标为 Java 进程且同 uid；首次 attach 需网络下载完整包" >&2
        exit 1
    fi
    echo "prewarm: 完成（~/.arthas/ 已含完整包，此后可离线 attach）"
fi
