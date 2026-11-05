#!/bin/bash
# discover.sh — 检查 P/D 环境采集所需的运行条件
# 输出: <status> 到 stdout; 诊断与处理建议到 stderr; 退出码 0=READY 2=不可用 3=参数错误
set -uo pipefail

help() {
    cat <<'HELP'
ascend-pd-env discover — Check docker and python3 availability for P/D env collection

Usage:
  discover.sh [options]

Options:
  --help          Show this help message

Status codes (stdout first line):
  READY               docker reachable and python3 >= 3.6 present
  PYTHON_MISSING      no python3 >= 3.6 on the host (nothing works without it)
  DOCKER_MISSING      docker binary not found (only existing bundles can be compared)
  DOCKER_UNAVAILABLE  docker found but the daemon does not answer
HELP
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h) help; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 3 ;;
    esac
done

fail() { # $1=status  $2..=stderr guidance
    local status="$1"; shift
    echo "$status"
    for line in "$@"; do echo "$line" >&2; done
    exit 2
}

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
        PY=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PY" ]; then
    fail PYTHON_MISSING \
        "宿主机上没有 python3 >= 3.6，采集和对比都跑不了。" \
        "装一个系统自带的 python3 即可，不需要任何第三方库。"
fi
echo "python3: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))" >&2

if ! command -v docker >/dev/null 2>&1; then
    fail DOCKER_MISSING \
        "找不到 docker，本机容器采不了。" \
        "已有的采集包（目录或 .tar.gz）仍然可以直接拿来对比。"
fi
echo "docker: $(command -v docker)" >&2

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    fail DOCKER_UNAVAILABLE \
        "docker 在，但连不上守护进程。" \
        "检查 dockerd 是否启动，以及当前用户是否在 docker 组里（或用 root 跑）。"
fi
echo "docker server: $(docker version --format '{{.Server.Version}}' 2>/dev/null)" >&2

echo READY
exit 0
