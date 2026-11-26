#!/usr/bin/env bash
set -euo pipefail

# brbe_ab_benchmark.sh — BRBE/CSSPGO（及任意编译候选）A/B 收益验证 harness
# 两种模式:
#   run:     多轮压测 + 指标提取 + 统计（均值/标准差/CV），产出 <label>.json
#   compare: 基线 vs 候选增益判定（gain<=noise 拒绝，对应 skill 候选动作 reject_condition）
#
# 用法:
#   brbe_ab_benchmark.sh --mode run --label baseline --cmd "<压测命令>" [--rounds 5] [--warmup 1] \
#       [--metric-regex 'tok/s'] [--value-regex '<re>'] [--out-dir /tmp/brbe_ab]
#   brbe_ab_benchmark.sh --mode compare --baseline /tmp/brbe_ab/baseline.json \
#       --candidate /tmp/brbe_ab/use.json [--min-gain 1.0]
#
# 关键纪律（CSSPGO.md 方法论教训）:
#   A/B 两侧必须用完全相同的压测命令与启动器（含 LD_PRELOAD 分配器等运行时），
#   跨编译栈对比未对齐 preload 会被完全带偏。

MODE=""
LABEL=""
CMD=""
ROUNDS=5
WARMUP=1
METRIC_REGEX="tok/s"
VALUE_REGEX="[0-9]+([.][0-9]+)?"
OUT_DIR="/tmp/brbe_ab"
BASELINE=""
CANDIDATE=""
MIN_GAIN="1.0"

usage() {
  cat <<'EOF'
Usage:
  brbe_ab_benchmark.sh --mode run --label <name> --cmd "<压测命令>" [options]
  brbe_ab_benchmark.sh --mode compare --baseline <file> --candidate <file> [options]

run options:
  --label <name>        阶段标签（baseline | gen | use | 自定义），产物 <out-dir>/<label>.json
  --cmd "<command>"     压测命令（A/B 两侧必须完全一致，含启动器/preload）
  --rounds <n>          统计轮数（默认 5，取均值）
  --warmup <n>          预热轮数，不计入统计（默认 1，0 关闭）
  --metric-regex <re>   指标行过滤，不区分大小写（默认 'tok/s'）
  --value-regex <re>    行内数值提取（默认第一个整数/小数）
  --out-dir <dir>       结果目录（默认 /tmp/brbe_ab）

compare options:
  --baseline <file>     run 模式产出的基线 JSON
  --candidate <file>    run 模式产出的候选 JSON
  --min-gain <pct>      接受门槛百分比（默认 1.0）

判定:
  |gain| <= noise                → reject(gain<=noise)     差异不显著
  gain > noise 且 >= min-gain    → accept
  gain > noise 且 < min-gain     → reject(below_min_gain)
  gain < -noise                  → regression（性能退化，回退基线）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:?}"; shift 2 ;;
    --label) LABEL="${2:?}"; shift 2 ;;
    --cmd) CMD="${2:?}"; shift 2 ;;
    --rounds) ROUNDS="${2:?}"; shift 2 ;;
    --warmup) WARMUP="${2:?}"; shift 2 ;;
    --metric-regex) METRIC_REGEX="${2:?}"; shift 2 ;;
    --value-regex) VALUE_REGEX="${2:?}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?}"; shift 2 ;;
    --baseline) BASELINE="${2:?}"; shift 2 ;;
    --candidate) CANDIDATE="${2:?}"; shift 2 ;;
    --min-gain) MIN_GAIN="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage >&2; exit 1 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }
log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

[[ -n "${MODE}" ]] || { usage >&2; fail "--mode is required (run|compare)"; }
command -v python3 >/dev/null 2>&1 || fail "python3 is required（统计与 JSON 产出）"

if [[ "${MODE}" == "run" ]]; then
  # ── run 模式 ──
  [[ -n "${LABEL}" ]] || fail "--mode run 需要 --label"
  [[ -n "${CMD}" ]]   || fail "--mode run 需要 --cmd"
  [[ "${ROUNDS}" =~ ^[1-9][0-9]*$ ]] || fail "--rounds 须为正整数"
  [[ "${WARMUP}" =~ ^[0-9]+$ ]] || fail "--warmup 须为非负整数"
  # LABEL 拼入 ${OUT_DIR}/${LABEL} 产物路径，须按文件名白名单校验（禁 / 与 ..，防路径逃逸）
  [[ "${LABEL}" =~ ^[A-Za-z0-9_.-]+$ ]] || fail "--label 含非法字符（仅允许 [A-Za-z0-9_.-]，禁止 / 与空格，防路径逃逸）"

  mkdir -p "${OUT_DIR}"
  LOG_PREFIX="${OUT_DIR}/${LABEL}"
  log "A/B 压测: label=${LABEL}, rounds=${ROUNDS}, warmup=${WARMUP}"
  log "命令: ${CMD}"
  log "[纪律] A/B 两侧必须同一启动器/环境（LD_PRELOAD 分配器等）；跨栈对比未对齐 preload 会得出错误结论"

  extract_value() { # <logfile>
    local line val
    # grep 未命中返回 1，set -euo pipefail 下赋值失败会静默退出、诊断分支不可达，须 || true 兜底
    line=$(grep -iE "${METRIC_REGEX}" "$1" | head -1 || true)
    if [[ -z "${line}" ]]; then
      echo ""
      return
    fi
    val=$(echo "${line}" | grep -oE "${VALUE_REGEX}" | head -1 || true)
    echo "${val}"
  }

  # 预热轮（不计入）
  for w in $(seq 1 "${WARMUP}" 2>/dev/null); do
    log "warmup ${w}/${WARMUP} ..."
    if ! bash -c "${CMD}" > "${LOG_PREFIX}_warmup.log" 2>&1; then
      fail "warmup 轮失败，日志: ${LOG_PREFIX}_warmup.log"
    fi
  done

  # 统计轮
  declare -a VALUES=()
  for r in $(seq 1 "${ROUNDS}"); do
    local_log="${LOG_PREFIX}_round${r}.log"
    log "round ${r}/${ROUNDS} ..."
    if ! bash -c "${CMD}" > "${local_log}" 2>&1; then
      fail "第 ${r} 轮压测失败，日志: ${local_log}（先修负载再验证收益）"
    fi
    val=$(extract_value "${local_log}")
    if [[ -z "${val}" ]]; then
      fail "第 ${r} 轮未提取到指标（metric_regex='${METRIC_REGEX}', value_regex='${VALUE_REGEX}'）。日志最后一行: $(tail -1 "${local_log}" 2>/dev/null)。请调整正则或查看 ${local_log}"
    fi
    VALUES+=("${val}")
    log "round ${r}: ${val}"
    [[ "${r}" -gt 1 ]] && rm -f "${LOG_PREFIX}_round$((r-1)).log"   # 只保留最近一轮，控制磁盘
  done

  VALS_CSV=$(IFS=,; echo "${VALUES[*]}")
  python3 - "${OUT_DIR}/${LABEL}.json" "${LABEL}" "${CMD}" "${ROUNDS}" "${WARMUP}" \
           "${METRIC_REGEX}" "${VALUE_REGEX}" "${VALS_CSV}" <<'PY'
import json, statistics, sys, datetime
out_path, label, cmd, rounds, warmup, mre, vre, vals_csv = sys.argv[1:9]
values = [float(x) for x in vals_csv.split(',')]
mean = statistics.mean(values)
stdev = statistics.stdev(values) if len(values) > 1 else 0.0
cv_pct = (stdev / mean * 100.0) if mean else 0.0
data = {
    "label": label, "cmd": cmd, "rounds": int(rounds), "warmup": int(warmup),
    "metric_regex": mre, "value_regex": vre,
    "values": values, "mean": round(mean, 4), "stdev": round(stdev, 4),
    "cv_pct": round(cv_pct, 3),
    "timestamp": datetime.datetime.now().isoformat(timespec='seconds'),
    "launcher_note": "A/B 两侧必须同一启动器/环境（LD_PRELOAD 分配器等），否则结论被带偏",
}
with open(out_path, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"mean={data['mean']} stdev={data['stdev']} cv={data['cv_pct']}% -> {out_path}")
print(json.dumps({k: data[k] for k in ("label", "mean", "stdev", "cv_pct", "rounds")}, ensure_ascii=False))
PY

elif [[ "${MODE}" == "compare" ]]; then
  # ── compare 模式 ──
  [[ -n "${BASELINE}" ]]  || fail "--mode compare 需要 --baseline"
  [[ -n "${CANDIDATE}" ]] || fail "--mode compare 需要 --candidate"
  [[ -f "${BASELINE}" ]]  || fail "基线文件不存在: ${BASELINE}"
  [[ -f "${CANDIDATE}" ]] || fail "候选文件不存在: ${CANDIDATE}"

  RESULT_JSON="${OUT_DIR}/compare_$(basename "${BASELINE}" .json)_vs_$(basename "${CANDIDATE}" .json).json"
  mkdir -p "${OUT_DIR}"
  python3 - "${BASELINE}" "${CANDIDATE}" "${MIN_GAIN}" "${RESULT_JSON}" <<'PY'
import json, sys, math, datetime
base_path, cand_path, min_gain, out_path = sys.argv[1:5]
base = json.load(open(base_path)); cand = json.load(open(cand_path))
bm, cm = base["mean"], cand["mean"]
bcv, ccv = base.get("cv_pct", 0.0), cand.get("cv_pct", 0.0)
gain_pct = (cm - bm) / bm * 100.0
noise_pct = math.sqrt(bcv ** 2 + ccv ** 2)
if abs(gain_pct) <= noise_pct:
    verdict, reason = "reject", "gain<=noise（差异不显著，拒绝归因）"
elif gain_pct < -noise_pct:
    verdict, reason = "regression", "性能退化（回退基线，对应候选 rollback）"
elif gain_pct < float(min_gain):
    verdict, reason = "reject", "below_min_gain（有增益但低于接受门槛）"
else:
    verdict, reason = "accept", "增益显著且达到门槛"
result = {
    "baseline": {"label": base["label"], "mean": bm, "cv_pct": bcv, "values": base["values"]},
    "candidate": {"label": cand["label"], "mean": cm, "cv_pct": ccv, "values": cand["values"]},
    "gain_pct": round(gain_pct, 3), "noise_pct": round(noise_pct, 3),
    "min_gain_pct": float(min_gain), "verdict": verdict, "reason": reason,
    "timestamp": datetime.datetime.now().isoformat(timespec='seconds'),
}
with open(out_path, 'w') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"baseline {base['label']}: mean={bm} (cv {bcv}%)")
print(f"candidate {cand['label']}: mean={cm} (cv {ccv}%)")
print(f"gain={gain_pct:+.3f}%  noise={noise_pct:.3f}%  -> verdict={verdict}: {reason}")
print(json.dumps(result, ensure_ascii=False))
PY

else
  fail "未知 --mode: ${MODE}（run|compare）"
fi
