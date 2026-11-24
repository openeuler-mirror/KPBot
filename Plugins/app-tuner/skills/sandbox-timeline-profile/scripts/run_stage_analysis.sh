#!/usr/bin/env bash
# Run only stage analysis and publish a complete, validated output set.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  cat <<'EOF'
Usage: run_stage_analysis.sh PROFILE_DIR [OUTPUT_DIR]

Consumes PROFILE_DIR/evidence and PROFILE_DIR/startup-latency. Writes
analysis.json, stage-latency.csv, create-container-details.jsonl, and
analysis-summary.md to OUTPUT_DIR (default: PROFILE_DIR).
EOF
  exit 0
fi
if (($# < 1 || $# > 2)); then
  printf 'Usage: run_stage_analysis.sh PROFILE_DIR [OUTPUT_DIR]\n' >&2
  exit 2
fi
profile_dir=$1
out_dir=${2:-$profile_dir}
if [[ ! -d "$profile_dir/evidence" || ! -d "$profile_dir/startup-latency" ]]; then
  printf 'BLOCKED: PROFILE_DIR must contain evidence/ and startup-latency/: %s\n' "$profile_dir" >&2
  exit 2
fi
for tool in node python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'BLOCKED: required tool is unavailable: %s\n' "$tool" >&2
    exit 2
  fi
done

manifest="$profile_dir/evidence/evidence-manifest.json"
if [[ -f "$manifest" ]]; then
  python3 - "$manifest" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    manifest = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    print(f"BLOCKED: invalid evidence manifest {path}: {error}", file=sys.stderr)
    raise SystemExit(2)
status = manifest.get("status") if isinstance(manifest, dict) else None
if status != "success":
    print(
        f"BLOCKED: evidence manifest status must be success, got {status!r}: {path}; "
        "evidence is retained and no analysis was published. Recollect or repair the failed collection first.",
        file=sys.stderr,
    )
    raise SystemExit(2)
PY
fi

mkdir -p "$out_dir"
work_dir=$(mktemp -d "$out_dir/.stage-analysis.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT

node "$script_dir/analyze_c50_profile.mjs" "$profile_dir" \
  --csv-out "$work_dir/stage-latency.csv" \
  --details-out "$work_dir/create-container-details.jsonl" \
  >"$work_dir/analysis.json"

python3 - "$work_dir" "$profile_dir" "$out_dir" <<'PY'
import csv
import json
from pathlib import Path
import sys

work = Path(sys.argv[1])
profile = Path(sys.argv[2]).resolve()
out = Path(sys.argv[3]).resolve()
analysis_path = work / "analysis.json"
data = json.loads(analysis_path.read_text(encoding="utf-8"))
if not isinstance(data, dict) or not isinstance(data.get("benchmark"), dict):
    raise SystemExit("invalid analyzer output: benchmark object is required")
with (work / "stage-latency.csv").open(encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle))
if not rows or not any(row.get("stage_id") == "api.create" for row in rows):
    raise SystemExit("invalid analyzer output: stage topology is empty")
if not (work / "create-container-details.jsonl").is_file():
    raise SystemExit("invalid analyzer output: details JSONL is missing")

data["generated_files"] = {
    "stage_latency_csv": str((out / "stage-latency.csv").relative_to(profile))
        if (out / "stage-latency.csv").is_relative_to(profile)
        else str(out / "stage-latency.csv"),
    "create_container_details_jsonl": str((out / "create-container-details.jsonl").relative_to(profile))
        if (out / "create-container-details.jsonl").is_relative_to(profile)
        else str(out / "create-container-details.jsonl"),
}
analysis_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

stages = []
for component_key in ("cubelet_stages", "shim_stages", "cubemaster_stages"):
    for name, values in data.get(component_key, {}).items():
        if values and values.get("avg") is not None:
            stages.append((values["avg"], component_key, name, values.get("count", 0)))
stages.sort(reverse=True)
bench = data["benchmark"]
lines = [
    f"# 阶段分析摘要：{data.get('case', 'unknown')}",
    "",
    f"- 成功请求：{bench.get('successful', 'n/a')}；错误：{bench.get('errors', 'n/a')}",
    f"- 跨组件关联实例：{data.get('correlated_instances', 0)}",
    f"- 拓扑阶段行数：{len(rows)}",
    "",
    "## 平均耗时最高的已观测阶段",
    "",
]
lines.extend(f"- `{name}`（{key}）：avg={avg:.3f} ms，count={count}" for avg, key, name, count in stages[:8])
lines.extend([
    "",
    "## 解读边界",
    "",
    "- 父子、同调用对端、并行与传播副本不可直接相加。",
    "- 跨进程计时不可逐样本相减；阈值日志仅覆盖部分样本。",
    "- API 与 CubeMaster 的 warmup 口径可能不同；轮转 warning 必须随结果审阅。",
    "",
])
(work / "analysis-summary.md").write_text("\n".join(lines), encoding="utf-8")
PY

for name in analysis.json stage-latency.csv create-container-details.jsonl analysis-summary.md; do
  mv "$work_dir/$name" "$out_dir/$name"
done
trap - EXIT
rm -rf "$work_dir"
printf 'Stage analysis written to %s\n' "$out_dir"
