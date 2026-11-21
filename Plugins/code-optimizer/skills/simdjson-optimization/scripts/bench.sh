#!/usr/bin/env bash
# simdjson parse 基准多轮采样：输出每轮与中位数的 stage1/stage2/all c/b 与 GB/s。
# 用法: bench.sh [-a IMPL] [-j JSON] [-n ITERS] [-R ROUNDS] [-b build_dir] [--perf-stat] [--numa N] [--no-force-env]
set -euo pipefail

IMPL="arm64"
JSON="twitter.json"
ITERS=200
ROUNDS=3
BUILD_DIR=""
PERF_STAT=0
NUMA_NODE=""
FORCE_ENV=1
PERF_EVENTS="cycles,instructions,cache-references,cache-misses,branch-misses"

usage() {
  cat <<EOF
用法: $0 [选项]

  -a IMPL    强制 SIMD 实现 (默认: arm64)
  -j JSON    JSON 语料 (默认: twitter.json)
  -n ITERS   parse 迭代次数 (默认: 200)
  -R ROUNDS  采样轮数 (默认: 3)
  -b DIR     构建目录 (默认: <repo>/build-Release)
  --perf-stat  每轮前跑一次 perf stat
  --perf-events E  自定义 perf 事件 (默认: ${PERF_EVENTS})
  --numa N   绑 NUMA 节点 (numactl -N N -m N)
  --no-force-env  不设置 SIMDJSON_FORCE_IMPLEMENTATION
  -h         帮助

输出: 每轮 7 列 + 中位数行, 并落盘 perf_data/bench_<ts>.tsv
EOF
  exit 0
}

PARSED_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -a) IMPL="$2"; shift 2;;
    -j) JSON="$2"; shift 2;;
    -n) ITERS="$2"; shift 2;;
    -R) ROUNDS="$2"; shift 2;;
    -b) BUILD_DIR="$2"; shift 2;;
    --perf-stat) PERF_STAT=1; shift;;
    --perf-events) PERF_EVENTS="$2"; shift 2;;
    --numa) NUMA_NODE="$2"; shift 2;;
    --no-force-env) FORCE_ENV=0; shift;;
    -h) usage;;
    *) PARSED_ARGS+=("$1"); shift;;
  esac
done

REPO_ROOT="${SIMDJSON_ROOT:-$(pwd)}"
REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)"
BUILD_DIR="${BUILD_DIR:-${REPO_ROOT}/build-Release}"
PARSE_BIN="${BUILD_DIR}/benchmark/dom/parse"

if [[ ! -x "${PARSE_BIN}" ]]; then
  echo "错误: parse 基准未构建: ${PARSE_BIN}" >&2
  echo "先运行: scripts/build.sh -t Release" >&2
  exit 1
fi

# 解析语料路径
_resolve_json() {
  local f="$1"
  [[ -f "$f" ]] && { echo "$f"; return; }
  [[ -f "${REPO_ROOT}/${f}" ]] && { echo "${REPO_ROOT}/${f}"; return; }
  [[ -f "${BUILD_DIR}/${f}" ]] && { echo "${BUILD_DIR}/${f}"; return; }
  [[ -f "${BUILD_DIR}/jsonexamples/${f}" ]] && { echo "${BUILD_DIR}/jsonexamples/${f}"; return; }
  echo "错误: JSON 文件未找到: $f" >&2
  exit 1
}
JSON_PATH="$(_resolve_json "${JSON}")"

OUT_DIR="${REPO_ROOT}/perf_data"
mkdir -p "${OUT_DIR}"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${OUT_DIR}/bench_${TS}.tsv"

NUMA_PREFIX=()
if [[ -n "$NUMA_NODE" ]]; then
  NUMA_PREFIX=(numactl -N "${NUMA_NODE}" -m "${NUMA_NODE}")
fi

FORCE_ENV_PREFIX=()
if [[ $FORCE_ENV -eq 1 ]]; then
  FORCE_ENV_PREFIX=(env "SIMDJSON_FORCE_IMPLEMENTATION=${IMPL}")
fi

echo "=== parse 基准: impl=${IMPL} json=$(basename "${JSON_PATH}") iters=${ITERS} rounds=${ROUNDS} ==="
echo "=== 输出: ${OUT_FILE} ==="
echo -e "round\tname\talloc_c_b\tstage1_c_b\tstage2_c_b\tall_c_b\tGB_s_all\tGB_s_stage1\tGB_s_stage2" > "${OUT_FILE}"

collect_one() {
  # $1 = round label; 输出到 stdout: 8 列 (label + 7 列)
  local r="$1"
  local line
  line=$("${FORCE_ENV_PREFIX[@]}" "${NUMA_PREFIX[@]}" "${PARSE_BIN}" -a "${IMPL}" -n "${ITERS}" -t "${JSON_PATH}" 2>/dev/null | grep -v '^\[' | tail -1)
  if [[ -z "$line" ]]; then
    echo "错误: parse 无输出 (round ${r})" >&2
    return 1
  fi
  # 第一列是 "name" (带引号), 之后 6 列数字
  echo "$line" | awk -v r="$r" -F'\t' '{ name=$1; gsub(/"/,"",name);
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", r, name, $2, $3, $4, $5, $6, $7, $8 }'
}

I=0
while [[ $I -lt $ROUNDS ]]; do
  I=$((I+1))
  if [[ $PERF_STAT -eq 1 ]]; then
    echo "--- round ${I}: perf stat ---"
    "${FORCE_ENV_PREFIX[@]}" "${NUMA_PREFIX[@]}" perf stat -e "${PERF_EVENTS}" \
      "${PARSE_BIN}" -a "${IMPL}" -n "${ITERS}" -t "${JSON_PATH}" 2>&1 | tail -20
  fi
  row="$(collect_one "${I}")" || exit 1
  echo "$row" | tee -a "${OUT_FILE}"
done

echo ""
echo "=== 中位数 (stage1/stage2 c/b 与 GB/s) ==="
# 中位数按 round 数取排序后的中间值
awk -F'\t' 'NR>1 { s1c[NR]=$4; s1g[NR]=$8; s2g[NR]=$9; allc[NR]=$6; allg[NR]=$7 }
  END{
    n=NR-1; mid=int((n+1)/2);
    asort(s1c); asort(s1g); asort(s2g); asort(allc); asort(allg);
    med_s1c = (n%2) ? s1c[mid] : (s1c[mid]+s1c[mid+1])/2;
    med_s1g = (n%2) ? s1g[mid] : (s1g[mid]+s1g[mid+1])/2;
    med_s2g = (n%2) ? s2g[mid] : (s2g[mid]+s2g[mid+1])/2;
    med_allc = (n%2) ? allc[mid] : (allc[mid]+allc[mid+1])/2;
    med_allg = (n%2) ? allg[mid] : (allg[mid]+allg[mid+1])/2;
    printf "stage1 c/b=%.6f  stage1 GB/s=%.6f  stage2 GB/s=%.6f  all c/b=%.6f  all GB/s=%.6f\n", \
      med_s1c, med_s1g, med_s2g, med_allc, med_allg;
  }' "${OUT_FILE}"

echo ""
echo "=== 完成: ${OUT_FILE} ==="
