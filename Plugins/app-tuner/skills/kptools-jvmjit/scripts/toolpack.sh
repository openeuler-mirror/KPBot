#!/usr/bin/env bash
# kptools-jvmjit toolpack.sh — 把宿主机 perf（二进制 + 非 glibc 依赖）注入容器
# 适用：镜像无 perf、容器 glibc >= 宿主 glibc、同架构。
# 前提：容器需已具备 perf 权限（--cap-add=PERFMON / SYS_ADMIN，否则采集阶段报 EPERM）。
# 退出码: 0 成功; 1 失败; 3 参数错误
set -uo pipefail

DEST="/tmp/jvmjit-tools"
AGENT_PATH="${JVMJIT_AGENT:-/usr/lib64/libperf-jvmti.so}"
CONTAINER=""
usage() { echo "用法: toolpack.sh --container CTR [--dest DIR] [--agent PATH]"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --container) CONTAINER="${2:?}"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --agent) AGENT_PATH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 3 ;;
    esac
done
[ -z "$CONTAINER" ] && { usage >&2; exit 3; }

# --dest fail-closed 校验：DEST 会拼入容器侧命令与 docker cp 路径，
# 须为绝对路径且仅含安全字符（拒绝 `;` `$` 反引号 空格等 shell 元字符）
if ! [[ "$DEST" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "FAIL: --dest 须为绝对路径且仅含 [A-Za-z0-9._/-] 字符（拒绝 shell 元字符）: $DEST" >&2
    exit 3
fi

command -v perf >/dev/null 2>&1 || { echo "FAIL: 宿主机 perf 不可用" >&2; exit 1; }
docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "FAIL: 容器不存在: $CONTAINER" >&2; exit 1; }

# 等待容器可 exec（刚创建的容器 entrypoint 初始化需要数秒）
for _ in $(seq 1 15); do
    docker exec "$CONTAINER" true >/dev/null 2>&1 && break
    sleep 1
done
docker exec "$CONTAINER" true >/dev/null 2>&1 || { echo "FAIL: 容器 15s 内不可 exec（未运行或初始化卡住）" >&2; exit 1; }

PERF_BIN=$(command -v perf)

WORK=$(mktemp -d /tmp/jvmjit-toolpack.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/lib"

# 非 glibc 族依赖才随包（容器自带新版 libc；宿主 glibc 二进制在容器 glibc>=宿主时前向兼容）
LDD_OUT=$(ldd "$PERF_BIN" 2>/dev/null) || { echo "FAIL: ldd perf 失败" >&2; exit 1; }
LIBS=$(echo "$LDD_OUT" | awk '{for(i=1;i<=NF;i++) if($i ~ /^\//) print $i}' \
    | grep -vE 'libc\.so|libm\.so|libpthread|libdl\.so|ld-linux|librt\.so|libgcc_s' | sort -u)
[ -n "$LIBS" ] && cp $LIBS "$WORK/lib/"

docker exec "$CONTAINER" mkdir -p "$DEST/lib" || { echo "FAIL: 容器 mkdir $DEST 失败" >&2; exit 1; }
docker cp "$PERF_BIN" "$CONTAINER:$DEST/perf" || { echo "FAIL: cp perf 失败" >&2; exit 1; }
if [ -n "$LIBS" ]; then
    docker cp "$WORK/lib/." "$CONTAINER:$DEST/lib/" || { echo "FAIL: cp libs 失败" >&2; exit 1; }
fi
if [ -f "$AGENT_PATH" ]; then
    docker cp "$AGENT_PATH" "$CONTAINER:$DEST/" || { echo "FAIL: cp agent 失败" >&2; exit 1; }
else
    echo "WARN: agent $AGENT_PATH 不存在，仅注入 perf；JVM 加载 agent 前需先放入容器" >&2
fi

# 容器内验证（env 参数数组形式，不经 bash -c 字符串拼接）
VER=$(docker exec "$CONTAINER" env "LD_LIBRARY_PATH=$DEST/lib" "$DEST/perf" --version 2>&1) \
    || { echo "FAIL: 容器内 perf 无法运行（检查架构/glibc: 容器 glibc 须 >= 宿主）: $VER" >&2; exit 1; }
echo "工具注入完成: $DEST"
echo "容器内 perf: $VER"
echo "agent 已放入: $DEST/$(basename "$AGENT_PATH")（JVM 启动参数追加 -agentpath:$DEST/$(basename "$AGENT_PATH")）"
exit 0
