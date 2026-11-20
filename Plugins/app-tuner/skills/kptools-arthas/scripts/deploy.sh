#!/bin/bash
# arthas deploy — 检查/安装 arthas-boot.jar（Apache-2.0 开源工具）
set -euo pipefail

print_help() {
    cat <<'EOF'
arthas deploy — Check/install arthas-boot.jar

Behavior:
  - 已在标准路径（$ARTHAS_BOOT_JAR / ~/.arthas/ / /opt/arthas/）→ 校验官方摘要后报告并退出 0
  - 未安装且非 --prewarm：从官方源下载 arthas-boot.jar 到 ~/.arthas/ 并做 sha256 校验
  - 标准路径（~/.arthas/ / /opt/arthas/）下的既有 jar 执行前同样做官方摘要校验，
    不匹配即拒绝（fail-closed）；ARTHAS_BOOT_JAR 显式指定的非标准路径仅打 NOTICE 留痕
  - --prewarm <PID>：对一个可用 JVM 执行一次批处理 attach，
    把完整 arthas 包缓存到 ~/.arthas/（离线环境关键步骤，见 arthas-guide §2.3）

Trust anchor:
  官方源 URL 与期望摘要为脚本内常量，不可被环境变量覆盖（ARTHAS_BOOT_URL /
  ARTHAS_BOOT_SHA256 一经设置即报错退出）——期望摘要可被调用方覆盖会使校验
  退化为自证。上游发新版后摘要变化导致校验失败，属预期 fail-closed 行为，
  升级时同步更新脚本内 OFFICIAL_SHA256 常量。

  exit 0: 成功或已安装；1: 下载/校验/执行失败；3: 参数/环境错误
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

# 信任锚：官方源 URL 与摘要为脚本内常量（arthas-boot 4.3.5）。
# 不可被环境变量覆盖：本脚本的调用方就是 agent，期望摘要可被调用方提供会使
# sha256 校验退化为自证（评审 P2）。上游发布新版本后该摘要不再匹配 →
# 下载/校验失败并退出（fail-closed），升级时须同步更新此常量。
OFFICIAL_URL="https://arthas.aliyun.com/arthas-boot.jar"
OFFICIAL_SHA256="94e63f4b38dc1c54316ffc1ecb0d8f5b868ade4f285094c351113aeb0113ee19"

# 环境覆盖一律拒绝（fail-closed）：避免调用方自选来源/自证摘要静默生效
for _var in ARTHAS_BOOT_URL ARTHAS_BOOT_SHA256; do
    if [ -n "${!_var:-}" ]; then
        echo "ERROR: ${_var} 环境变量不再支持覆盖（信任锚固定为官方源与官方摘要）；确需换版本请修改脚本内常量" >&2
        exit 3
    fi
done

jar_sha() { sha256sum "$1" | awk '{ print $1 }'; }
is_std_jar() { [ "$1" = "$HOME/.arthas/arthas-boot.jar" ] || [ "$1" = "/opt/arthas/arthas-boot.jar" ]; }

JAR_CANDIDATES=("${ARTHAS_BOOT_JAR:-}" "$HOME/.arthas/arthas-boot.jar" "/opt/arthas/arthas-boot.jar")
find_jar() {
    local c
    for c in "${JAR_CANDIDATES[@]}"; do
        if [ -n "$c" ] && [ -f "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}

if JAR=$(find_jar); then
    # 既有 jar 执行前校验：标准路径做官方摘要硬校验（防被替换后静默执行）；
    # ARTHAS_BOOT_JAR 显式指定的非标准路径仅留痕（操作者自担）
    if is_std_jar "$JAR"; then
        ACTUAL_SHA256="$(jar_sha "$JAR")"
        if [ "$ACTUAL_SHA256" != "$OFFICIAL_SHA256" ]; then
            echo "ERROR: ${JAR} sha256 与官方分发摘要不匹配（got=${ACTUAL_SHA256}）：可能被篡改或为其他版本" >&2
            echo "       处置：删除该文件后重跑 deploy.sh 重新下载，或确认版本后更新脚本内 OFFICIAL_SHA256 常量" >&2
            exit 1
        fi
        echo "arthas-boot.jar: already installed (${JAR}, sha256 verified)"
    else
        echo "arthas-boot.jar: already installed (${JAR})"
        echo "NOTICE: ${JAR} 为 ARTHAS_BOOT_JAR 显式指定的非标准路径，未做官方摘要校验（实际 sha256=$(jar_sha "$JAR")）" >&2
    fi
else
    if [ -n "$PREWARM_PID" ]; then
        echo "ERROR: --prewarm 需先安装 arthas-boot.jar（先无参执行本脚本）" >&2
        exit 1
    fi
    URL="$OFFICIAL_URL"
    SHA256="$OFFICIAL_SHA256"
    DEST_DIR="$HOME/.arthas"
    mkdir -p "$DEST_DIR"
    echo "downloading arthas-boot.jar from ${URL} ..."
    if command -v curl &>/dev/null; then
        curl -fsSL -o "$DEST_DIR/arthas-boot.jar" "$URL" || { echo "ERROR: curl 下载失败（检查网络）" >&2; exit 1; }
    elif command -v wget &>/dev/null; then
        wget -q -O "$DEST_DIR/arthas-boot.jar" "$URL" || { echo "ERROR: wget 下载失败（检查网络）" >&2; exit 1; }
    else
        echo "ERROR: 需要 curl 或 wget 下载；离线环境手动放置官方 arthas-boot.jar 到 ~/.arthas/ 或 /opt/arthas/" >&2
        exit 1
    fi
    [ -s "$DEST_DIR/arthas-boot.jar" ] || { echo "ERROR: 下载文件为空" >&2; rm -f "$DEST_DIR/arthas-boot.jar"; exit 1; }
    # 完整性校验后才允许被 java -jar 执行（在线下载的可执行产物必须有信任锚）
    if ! command -v sha256sum &>/dev/null; then
        echo "ERROR: 本机无 sha256sum，无法校验下载产物，拒绝安装（fail-closed）" >&2
        rm -f "$DEST_DIR/arthas-boot.jar"
        exit 1
    fi
    ACTUAL_SHA256="$(jar_sha "$DEST_DIR/arthas-boot.jar")"
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
