#!/bin/bash
# collect.sh — P/D 环境采集 + 分析 + 自动对比的唯一入口
# stdout: 进度与结论 | stderr: 诊断 | 退出码: 0 成功 1 参数/采集包问题
#         2 环境不可用 3 没采到或环境不足两个 130 中断
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

short_help() {
    cat <<'HELP'
ascend-pd-env collect — Collect and compare vLLM-Ascend P/D container environments

Usage:
  collect.sh <容器名|采集包> [更多对象...] [--out 目录] [--role P|D|PD|other] [--python 路径]
  collect.sh --help

一个对象就采集并出单环境报告，多个对象就采完自动比。容器名和已有采集包可以混着写。
HELP
}

if [ $# -eq 0 ]; then
    short_help >&2
    echo "" >&2
    echo "至少要给一个对象。完整帮助: collect.sh --help" >&2
    exit 1
fi

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
        PY=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PY" ]; then
    echo "PYTHON_MISSING" >&2
    echo "宿主机上没有 python3 >= 3.6，采集和对比都跑不了。先运行 discover.sh 看环境。" >&2
    exit 2
fi

export PD_ENV_PROG="collect.sh"
exec "$PY" "$SCRIPT_DIR/pd_env.py" "$@"
