#!/usr/bin/env bash
# kptools-jvmjit check.sh — 先决条件检查（宿主机 / 容器模式）
# 退出码: 0 全部满足; 2 先决缺失（stderr 列明缺失项与修复指引）; 3 参数错误
set -uo pipefail

AGENT_PATH="${JVMJIT_AGENT:-/usr/lib64/libperf-jvmti.so}"
PID=""
CONTAINER=""

usage() { sed -n '2,5p' "$0"; echo "用法: check.sh [-p PID] [--container CTR] [--agent PATH]"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="${2:?}"; shift 2 ;;
        --container) CONTAINER="${2:?}"; shift 2 ;;
        --agent) AGENT_PATH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 3 ;;
    esac
done

# PID fail-closed 数值校验：PID 会拼入容器侧命令，非数字一律拒绝（防 shell 注入）
[ -n "$PID" ] && { [[ "$PID" =~ ^[0-9]+$ ]] || { echo "FAIL: PID 须为数字" >&2; exit 3; }; }

FAILS=0
fail() { echo "FAIL: $*" >&2; FAILS=$((FAILS+1)); }
ok()   { echo "PASS: $*"; }

# 1. perf 存在且支持 --jit
if command -v perf >/dev/null 2>&1; then
    if perf inject --help 2>&1 | grep -q -- '--jit'; then
        ok "perf $(perf --version 2>/dev/null) 支持 --jit"
    else
        fail "perf 不支持 --jit，请升级 perf"
    fi
else
    fail "perf 不可用"
fi

# 2. agent 文件
if [ -f "$AGENT_PATH" ]; then
    ok "agent: $AGENT_PATH"
else
    fail "agent 不存在: $AGENT_PATH（openEuler/CentOS: /usr/lib64/libperf-jvmti.so，随 perf 包发布；也可 JVMJIT_AGENT=/path 指定）"
fi

# 3. 容器模式：docker + 容器存在 + 容器内 perf 试跑
if [ -n "$CONTAINER" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        fail "docker 不可用"
    elif ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
        fail "容器不存在: $CONTAINER"
    else
        ok "容器存在: $CONTAINER"
        if docker exec "$CONTAINER" true 2>/dev/null; then
            # 容器侧探测一律用参数数组形式（env/直接 argv），不经 bash -c 字符串拼接
            if docker exec "$CONTAINER" perf stat -e cycles -- true >/dev/null 2>&1 \
               || docker exec "$CONTAINER" env LD_LIBRARY_PATH=/tmp/jvmjit-tools/lib /tmp/jvmjit-tools/perf stat -e cycles -- true >/dev/null 2>&1; then
                ok "容器内 perf 可用且有采样权限"
            else
                fail "容器内 perf_event_open 无权限：重建容器加 --cap-add=PERFMON（旧运行时用 --cap-add SYS_ADMIN），参考 jvmjit-guide §5.1"
            fi
        else
            fail "容器未运行: $CONTAINER"
        fi
    fi
fi

# 4. 目标进程（PID 提供时）
if [ -n "$PID" ]; then
    if [ -n "$CONTAINER" ]; then
        # 容器内读取必须让重定向/路径发生在容器侧：cat 在容器内执行（参数数组，无 shell 拼接），
        # 宿主侧 `< /proc/...` 重定向会打开宿主 /proc（PID 命名空间不同）且无 -i 时容器进程读到 EOF
        CMDLINE=$(docker exec "$CONTAINER" cat "/proc/$PID/cmdline" 2>/dev/null | tr '\0' ' ' || true)
    else
        CMDLINE=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null) || true
    fi
    if [ -z "$CMDLINE" ]; then
        fail "PID $PID 不存在（容器模式下须用容器内 PID）"
    elif ! echo "$CMDLINE" | grep -qiE '(^|/)java|jvm'; then
        fail "PID $PID 非 JVM 进程，按 SKILL.md 职责边界路由（Python → kptools-py-spy 等）"
    else
        ok "PID $PID 是 JVM 进程"
        # 4.1 agent 是否随 JVM 启动（关键先决）
        if echo "$CMDLINE" | grep -q -- '-agentpath'; then
            ok "JVM 启动参数含 -agentpath"
        else
            fail "JVM 启动参数无 -agentpath（agent 无法运行期补加）：启动参数追加 '-agentpath:$AGENT_PATH' 后重启目标 JVM；容器场景先把 agent cp 进容器"
        fi
        # 4.2 调用栈模式提示
        if echo "$CMDLINE" | grep -q 'PreserveFramePointer'; then
            ok "-XX:+PreserveFramePointer 已启用（可 --call-graph）"
        else
            echo "WARN: 无 -XX:+PreserveFramePointer，collect.sh 不要用 --call-graph（flat 模式不受影响）" >&2
        fi
    fi
fi

if [ "$FAILS" -gt 0 ]; then
    echo "先决缺失 $FAILS 项，详见 stderr" >&2
    exit 2
fi
echo "全部先决满足。"
exit 0
