#!/bin/bash
# arthas deploy — 检查/安装 arthas-boot.jar（Apache-2.0 开源工具）
set -euo pipefail

print_help() {
    cat <<'EOF'
arthas deploy — Check/install arthas-boot.jar

Behavior:
  - 已在标准路径（$ARTHAS_BOOT_JAR / ~/.arthas/ / /opt/arthas/）→ 报告并退出 0
  - 未安装且非 --prewarm：下载 arthas-boot.jar 到 ~/.arthas/（来源可用 ARTHAS_BOOT_URL 覆盖，
    但覆盖时必须同时提供 ARTHAS_BOOT_SHA256，否则 fail-closed 拒绝）
  - 下载后执行 sha256 完整性校验（默认固定官方 arthas-boot 4.3.5 摘要；上游发新版后
    摘要变化会导致校验失败，属预期 fail-closed 行为，升级时同步更新脚本内常量）
  - --prewarm <PID>：对一个可用 JVM 执行一次批处理 attach，
    把完整 arthas 包缓存到 ~/.arthas/（离线环境关键步骤，见 arthas-guide §2.3）

Environment:
  ARTHAS_BOOT_URL      下载来源覆盖（须配对 ARTHAS_BOOT_SHA256）
  ARTHAS_BOOT_SHA256   期望摘要覆盖（默认为官方 4.3.5 固定摘要）

  exit 0: 成功或已安装；1: 下载/校验/执行失败；3: 参数错误
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

# 固定官方发布摘要（arthas-boot 4.3.5，https://arthas.aliyun.com/arthas-boot.jar）。
# 上游发布新版本后该摘要不再匹配 → 下载校验失败并退出（fail-closed），
# 升级时须同步更新此常量。
DEFAULT_URL="https://arthas.aliyun.com/arthas-boot.jar"
DEFAULT_SHA256="94e63f4b38dc1c54316ffc1ecb0d8f5b868ade4f285094c351113aeb0113ee19"

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
    # 来源覆盖必须配对摘要（fail-closed）：不允许对任意来源免校验下载可执行 jar
    if [ -n "${ARTHAS_BOOT_URL:-}" ] && [ -z "${ARTHAS_BOOT_SHA256:-}" ]; then
        echo "ERROR: 覆盖 ARTHAS_BOOT_URL 时必须同时提供 ARTHAS_BOOT_SHA256（防任意来源未校验执行）" >&2
        exit 3
    fi
    URL="${ARTHAS_BOOT_URL:-$DEFAULT_URL}"
    SHA256="${ARTHAS_BOOT_SHA256:-$DEFAULT_SHA256}"
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
    [ -s "$DEST_DIR/arthas-boot.jar" ] || { echo "ERROR: 下载文件为空" >&2; rm -f "$DEST_DIR/arthas-boot.jar"; exit 1; }
    # 完整性校验后才允许被 java -jar 执行（在线下载的可执行产物必须有信任锚）
    if ! command -v sha256sum &>/dev/null; then
        echo "ERROR: 本机无 sha256sum，无法校验下载产物，拒绝安装（fail-closed）" >&2
        rm -f "$DEST_DIR/arthas-boot.jar"
        exit 1
    fi
    ACTUAL_SHA256="$(sha256sum "$DEST_DIR/arthas-boot.jar" | awk '{ print $1 }')"
    if [ "$ACTUAL_SHA256" != "$SHA256" ]; then
        echo "ERROR: arthas-boot.jar sha256 校验失败: got=${ACTUAL_SHA256} expect=${SHA256}" >&2
        echo "       （上游发新版本时摘要会变化，属预期 fail-closed；确认后更新脚本内固定摘要）" >&2
        rm -f "$DEST_DIR/arthas-boot.jar"
        exit 1
    fi
    echo "arthas-boot.jar: installed ($DEST_DIR/arthas-boot.jar, sha256 verified)"
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
