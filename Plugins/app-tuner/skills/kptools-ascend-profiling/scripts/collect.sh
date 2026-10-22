#!/bin/bash
# ascend-profiling collect — 采集控制（方案一 API / 方案二 开关）+ 产物解析
# 能力边界：采集依赖目标服务已按 profiler 配置启动；不改造服务启动命令，仅输出指引
set -euo pipefail

EVIDENCE_BASE="./ascend-profiling-evidence"

print_help() {
    cat <<'EOF'
kptools-ascend-profiling collect — NPU operator/service-framework profiling control

方案一（Ascend PyTorch Profiler，算子级，API 控制；服务需以 --profiler-config 启动）:
  pt-start   -u <vllm_url> [-o <dir>]    POST /start_profile
  pt-stop    -u <vllm_url>               POST /stop_profile
  pt-analyse -d <ascend_pt目录>          torch_npu.profiler.analyse 离线解析

方案二（MS Service Profiler，框架级，配置文件控制）:
  msp-enable  -c <config.json>           enable 0 → 1（首次启用需重启服务）
  msp-disable -c <config.json>           enable 1 → 0
  msp-parse   -d <profiler输出目录>       msserviceprofiler parse
  msp-domains                             输出可采集 domain 清单

Notes:
  stdout: 请求结果/产物路径；stderr: 诊断与指引
  exit 0: 成功；2: 依赖缺失；3: 参数错误；1: 请求/命令失败
  profiling 期间 npu-smi 设备指标失真（AICore 返回 0），解读时注意
EOF
    exit 0
}

if [ $# -gt 0 ]; then
    case "${!#}" in
        -u|-o|-d|-c) echo "ERROR: option ${!#} requires a value" >&2; exit 3 ;;
    esac
fi

CMD="${1:-}"
shift || true

case "$CMD" in
    pt-start|pt-stop|pt-analyse|msp-enable|msp-disable|msp-parse|msp-domains) ;;
    --help|"") print_help ;;
    *) echo "ERROR: unknown subcommand: $CMD（见 --help）" >&2; exit 3 ;;
esac

URL=""
OUT_DIR=""
DATA_DIR=""
CFG=""

while [ $# -gt 0 ]; do
    case "$1" in
        -u) URL="$2"; shift 2 ;;
        -o) OUT_DIR="$2"; shift 2 ;;
        -d) DATA_DIR="$2"; shift 2 ;;
        -c) CFG="$2"; shift 2 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

post_profile() {
    # $1=path（/start_profile | /stop_profile）
    local path="$1"
    local url="$URL${path}"
    local code body
    body=$(curl -s --max-time 10 -X POST -o /dev/null -w '%{http_code}' "$url" 2>/dev/null) || body="000"
    case "$body" in
        200|201) echo "POST $path -> 200 OK"; return 0 ;;
        000) echo "ERROR: vLLM 服务不可达: $URL（确认服务地址）" >&2; return 1 ;;
        404) echo "ERROR: $path 不存在（HTTP 404）。服务需以 --profiler-config 注入启动：" >&2
             echo "  python3 -m vllm.entrypoints.openai.api_server --profiler-config '{\"profiler\":\"torch\",\"torch_profiler_dir\":\"./vllm_profile\"}'" >&2
             return 1 ;;
        *) echo "ERROR: $path 请求失败（HTTP $body）" >&2; return 1 ;;
    esac
}

case "$CMD" in
    pt-start)
        if [ -z "$URL" ]; then echo "ERROR: pt-start 需要 -u <vllm_url>" >&2; exit 3; fi
        OUT_DIR="${OUT_DIR:-$EVIDENCE_BASE/$(date +%Y%m%d_%H%M%S)}"
        mkdir -p "$OUT_DIR"
        post_profile "/start_profile" || exit $?
        echo "profiling started（发送业务请求后执行 pt-stop；产物目录以服务端 torch_profiler_dir 为准，本目录仅作本地记录）: $OUT_DIR"
        ;;
    pt-stop)
        if [ -z "$URL" ]; then echo "ERROR: pt-stop 需要 -u <vllm_url>" >&2; exit 3; fi
        post_profile "/stop_profile" || exit $?
        echo "profiling stopped；等待服务写出 ascend_pt 产物后执行 pt-analyse"
        ;;
    pt-analyse)
        if [ -z "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then
            echo "ERROR: pt-analyse 需要 -d <ascend_pt产物目录> 且目录存在" >&2
            exit 3
        fi
        if ! python3 -c "import torch_npu" &>/dev/null; then
            echo "ERROR: torch_npu 不可导入（离线解析需 torch_npu；安装后重试）" >&2
            exit 2
        fi
        echo "=== analyse $DATA_DIR ==="
        python3 - "$DATA_DIR" <<'PYEOF' || { echo "ERROR: analyse 失败（核对产物目录格式 *_ascend_pt/）" >&2; exit 1; }
import sys
from torch_npu.profiler.profiler import analyse
analyse(sys.argv[1])
PYEOF
        # 不做无校验的成功文案：analyse 返回 0 不代表产物存在（空目录同样返回 0）
        if find "$DATA_DIR" -type f \( -name 'operator_details.csv' -o -name 'kernel_details.csv' \
                -o -name 'op_statistic.csv' -o -name 'step_trace_time.csv' -o -name 'trace_view.json' \) -print -quit | grep -q .; then
            echo "analyse 完成；产物含 operator_details.csv / kernel_details.csv / op_statistic.csv / step_trace_time.csv / trace_view.json 等"
        else
            echo "WARNING: analyse 返回 0 但 $DATA_DIR 下未找到预期产物（operator_details.csv 等）；请人工核对 analyse 输出位置" >&2
            exit 1
        fi
        ;;
esac

case "$CMD" in
    msp-enable|msp-disable)
        if [ -z "$CFG" ] || [ ! -r "$CFG" ]; then
            echo "ERROR: $CMD 需要 -c <config.json> 且文件可读" >&2
            exit 3
        fi
        if [ "$CMD" = "msp-enable" ]; then
            sed -i 's/"enable":\s*0/"enable": 1/' "$CFG"
            state=1
        else
            sed -i 's/"enable":\s*1/"enable": 0/' "$CFG"
            state=0
        fi
        if grep -q "\"enable\": $state" "$CFG"; then
            echo "config updated: enable=$state ($CFG)"
            [ "$state" = "1" ] && echo "NOTICE: 首次启用 MS Service Profiler 需以 SERVICE_PROF_CONFIG_PATH 环境变量重启服务" >&2
            exit 0
        fi
        echo "ERROR: 更新失败或配置文件中无 \"enable\" 键（人工核对 $CFG）" >&2
        exit 1
        ;;
    msp-parse)
        if [ -z "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then
            echo "ERROR: msp-parse 需要 -d <profiler输出目录> 且目录存在" >&2
            exit 3
        fi
        if ! command -v msserviceprofiler &>/dev/null; then
            echo "ERROR: msserviceprofiler 不存在（CANN toolkit 自带，或从 gitcode.com/Ascend/msserviceprofiler 源码构建）" >&2
            exit 2
        fi
        OUT_DIR="${OUT_DIR:-$EVIDENCE_BASE/$(date +%Y%m%d_%H%M%S)}"
        mkdir -p "$OUT_DIR"
        echo "=== msserviceprofiler parse $DATA_DIR ==="
        msserviceprofiler parse --input-path="$DATA_DIR" --output-path="$OUT_DIR" || { echo "ERROR: parse 失败" >&2; exit 1; }
        echo "parse 完成: $OUT_DIR（chrome_tracing.json / request.csv / kvcache.csv / batch.csv / profiler.db）"
        ;;
    msp-domains)
        cat <<'EOF'
MS Service Profiler 可采集 domain:
  Request        请求级生命周期（request.csv）
  KVCache        KV Cache 管理（kvcache.csv）
  ModelExecute   模型执行
  BatchSchedule  Batch 调度（batch.csv）
  Communication  通信（HCCL）
控制方式: JSON 配置文件 enable: 0/1（msp-enable/msp-disable）
EOF
        ;;
esac
