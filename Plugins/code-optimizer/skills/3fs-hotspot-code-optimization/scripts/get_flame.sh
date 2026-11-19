#!/usr/bin/env bash
# ============================================================================
# 3FS 进程火焰图采集脚本（storage_main / meta_main / hf3fs_fuse_main）
# 真源：本 skill（3fs-hotspot-code-optimization），阶段 1 取证入口
#
# 用法: bash get_flame.sh [duration] [subdir]
#   duration: 采集时长（秒），默认 5
#   subdir:   输出子目录名（可选），指定则输出到 ${OUTPUT_DIR}/<subdir>/
#             （子目录已存在则覆盖），不指定则输出到 ${OUTPUT_DIR}/
#
# 环境变量:
#   PROC            采集的进程名，默认 storage_main
#                   （可选 meta_main / hf3fs_fuse_main 等）
#   FLAMEGRAPH_DIR  FlameGraph 工具目录，默认 <脚本目录>/FlameGraph；
#                   缺失时自动尝试 git clone，失败则给出手动安装指引
#   OUTPUT_DIR      输出根目录，默认 <脚本目录>/flame
#
# 采集参数依据（SKILL.md 阶段 1a）:
#   --call-graph dwarf,8192 -F 99 -m 2048
#   帧指针(-g)穿不过 Rust release 代码；dwarf,65528 会导致 perf.data 爆炸；
#   lbr 在鲲鹏 920 PMU 上不可用。
#   纪律：采集火焰图的跑次不得用于带宽结论（profiling 会压负载）。
# ============================================================================
set -u

usage() {
    cat <<'EOF'
用法: bash get_flame.sh [duration] [subdir]
  duration   采集时长秒数（默认 5）
  subdir     输出子目录名（可选，已存在则覆盖）
环境变量:
  PROC=storage_main|meta_main|hf3fs_fuse_main   采集的进程名（默认 storage_main）
  FLAMEGRAPH_DIR=<dir>                          FlameGraph 工具目录（默认 <脚本目录>/FlameGraph）
  OUTPUT_DIR=<dir>                              输出根目录（默认 <脚本目录>/flame）
示例:
  bash get_flame.sh 15 run1                     # 采 storage_main 15 秒 → flame/run1/
  PROC=hf3fs_fuse_main bash get_flame.sh 15     # 采 FUSE 客户端进程
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

DURATION="${1:-5}"
SUBDIR="${2:-}"
PROC="${PROC:-storage_main}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_ROOT="${OUTPUT_DIR:-${SCRIPT_DIR}/flame}"
FLAMEGRAPH_DIR="${FLAMEGRAPH_DIR:-${SCRIPT_DIR}/FlameGraph}"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

# 如果指定了子目录，输出到 <root>/subdir/，否则输出到 <root>/
if [ -n "${SUBDIR}" ]; then
    OUTPUT="${OUTPUT_ROOT}/${SUBDIR}"
    # 如果子目录已存在，删除后重新创建（覆盖）
    if [ -d "${OUTPUT}" ]; then
        echo "[INFO] 子目录已存在，将覆盖: ${OUTPUT}"
        rm -rf "${OUTPUT}"
    fi
else
    OUTPUT="${OUTPUT_ROOT}"
fi

mkdir -p "${OUTPUT}"

if ! command -v perf &>/dev/null; then
    echo "[ERROR] perf 未安装" >&2
    exit 1
fi

if [ ! -x "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" ] || [ ! -x "${FLAMEGRAPH_DIR}/flamegraph.pl" ]; then
    echo "[WARN] FlameGraph 工具未找到: ${FLAMEGRAPH_DIR}，尝试自动 git clone ..."
    if ! git clone --depth 1 https://github.com/brendangregg/FlameGraph.git "${FLAMEGRAPH_DIR}"; then
        echo "[ERROR] 自动 clone 失败（无网络或无 git）。请手动执行:" >&2
        echo "  git clone https://github.com/brendangregg/FlameGraph.git ${FLAMEGRAPH_DIR}" >&2
        echo "  或指定已有工具目录重跑: FLAMEGRAPH_DIR=/opt/FlameGraph bash $0 ${DURATION} ${SUBDIR}" >&2
        exit 1
    fi
fi

echo "============================================================"
echo "  3FS ${PROC} 火焰图采集"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  节点: $(hostname)"
echo "  采集时长: ${DURATION}s"
echo "  输出目录: ${OUTPUT}"
echo "============================================================"

PERF_DATA="${OUTPUT}/perf_${TIMESTAMP}.data"
FOLDED_FILE="${OUTPUT}/perf_${TIMESTAMP}.folded"
FLAME_SVG="${OUTPUT}/flamegraph_${TIMESTAMP}.svg"
CPU_STAT_FILE="${OUTPUT}/cpu_stat_${TIMESTAMP}.txt"

echo "[INFO] 查找 ${PROC} 进程..."
PROC_INFO=$(ps aux | grep "${PROC}" | grep -v grep)
PIDS=$(echo "${PROC_INFO}" | awk '{print $2}' | tr '\n' ',' | sed 's/,$//')

if [ -z "${PIDS}" ]; then
    echo "[ERROR] 未找到 ${PROC} 进程" >&2
    exit 1
fi

echo "[INFO] 找到 ${PROC} 进程:"
echo "USER         PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND"
echo "${PROC_INFO}"
echo ""
echo "[INFO] 采集 PIDs: ${PIDS}"

echo "[INFO] 开始采集 CPU 使用率..."
(
    echo "=== CPU 使用率采样 ($(date '+%Y-%m-%d %H:%M:%S')) ===" > "${CPU_STAT_FILE}"
    echo "" >> "${CPU_STAT_FILE}"
    for i in $(seq 1 "${DURATION}"); do
        echo "--- 采样 $i/${DURATION} ---" >> "${CPU_STAT_FILE}"
        top -bn1 | head -20 >> "${CPU_STAT_FILE}"
        echo "" >> "${CPU_STAT_FILE}"
        sleep 1
    done
) &
CPU_MON_PID=$!

echo "[INFO] 开始采集 perf 数据 (${DURATION}s)..."
perf record --call-graph dwarf,8192 -F 99 -m 2048 -p "${PIDS}" -o "${PERF_DATA}" -- sleep "${DURATION}"

kill "${CPU_MON_PID}" 2>/dev/null
wait "${CPU_MON_PID}" 2>/dev/null

if [ ! -f "${PERF_DATA}" ]; then
    echo "[ERROR] perf record 失败" >&2
    exit 1
fi

echo "[INFO] 生成折叠栈文件..."
perf script -i "${PERF_DATA}" 2>/dev/null | "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" > "${FOLDED_FILE}"

echo "[INFO] 生成火焰图..."
"${FLAMEGRAPH_DIR}/flamegraph.pl" --title "3FS ${PROC} Flame Graph" "${FOLDED_FILE}" > "${FLAME_SVG}"

echo ""
echo "============================================================"
echo "  采集完成"
echo "  perf.data:  ${PERF_DATA}"
echo "  folded:     ${FOLDED_FILE}"
echo "  火焰图:     ${FLAME_SVG}"
echo "  CPU 统计:   ${CPU_STAT_FILE}"
echo "============================================================"
