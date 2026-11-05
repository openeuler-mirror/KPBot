#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
if [ -z "${VLLM_URL:-}" ] || ! python3 -c "import torch_npu" 2>/dev/null; then
    python3 -c "import json;json.dump({'success':True,'skipped':True,'skipped_or_success':True,'reason':'910B/vLLM not available'},open('$CASE_DIR/results/result.json','w'),indent=2)"
    exit 0
fi
bash "$S/collect.sh" pt-start -u "${VLLM_URL}" -o "$CASE_DIR/results/prof" >/dev/null 2>&1
curl -s -X POST "${VLLM_URL}/v1/chat/completions" -H 'Content-Type: application/json'     -d '{"model":"demo","messages":[{"role":"user","content":"hi"}]}' >/dev/null 2>&1
bash "$S/collect.sh" pt-stop -u "${VLLM_URL}" >/dev/null 2>&1
# 产物目录：PROF_OUTPUT_DIR（服务端 torch_profiler_dir，见 case.yaml 部署约束）优先，
# 其次 pt-start 本地 -o 目录
PROF_DIR="${PROF_OUTPUT_DIR:-$CASE_DIR/results/prof}"
bash "$S/collect.sh" pt-analyse -d "$PROF_DIR"/*_ascend_pt/ > "$CASE_DIR/results/analyse.log" 2>&1
ok=0
for f in operator_details.csv kernel_details.csv op_statistic.csv step_trace_time.csv trace_view.json; do
    [ -e "$PROF_DIR"/*_ascend_pt/$f ] && ok=$((ok + 1))
done
python3 -c "import json;json.dump({'success':bool($ok>=3),'artifacts':$ok,'skipped':False,'skipped_or_success':bool($ok>=3)},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((ok >= 3 ? 0 : 1))
