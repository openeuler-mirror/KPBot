#!/usr/bin/env bash
# kptools-cpuoffload-collector 回归测试 — 合成 /proc fixture
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$SCRIPT_DIR/probe.py"
DISCOVER="$SCRIPT_DIR/discover.sh"

pick_py() {
  if [ -n "${AT_PY:-}" ] && "${AT_PY}" -c 'pass' >/dev/null 2>&1; then
    echo "${AT_PY}"; return
  fi
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'pass' >/dev/null 2>&1; then
      command -v "$c"; return
    fi
  done
  echo ""
}
PY="$(pick_py)"
[ -n "$PY" ] || { echo "FATAL: 需要可用的 python3"; exit 1; }
echo "python: $PY"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAKE="$WORK/proc"
mkdir -p "$FAKE"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

# ---------------------------------------------------------------- fixture
mk_rank() {
  local pid=$1 rank=$2 ws=$3 start_kb=$4 nr_pages=$5 pfn_base=$6 present=$7
  local d="$FAKE/$pid"
  mkdir -p "$d"
  printf 'RANK=%s\0LOCAL_RANK=%s\0WORLD_SIZE=%s\0' "$rank" "$rank" "$ws" > "$d/environ"
  printf 'python3\n' > "$d/comm"
  local end_kb=$(( start_kb + nr_pages * 4 ))
  printf '%x-%x rw-p 00000000 00:05 1 /dev/devmm_svm\n' \
    $(( start_kb * 1024 )) $(( end_kb * 1024 )) > "$d/maps"
  printf '%x-%x rw-p 00000000 00:06 2 /dev/shm/other\n' \
    $(( end_kb * 1024 + 4096 )) $(( end_kb * 1024 + 12288 )) >> "$d/maps"
  "$PY" - "$d/pagemap" "$nr_pages" "$pfn_base" "$present" "$start_kb" <<'PYEOF'
import sys, struct
path, nr, pfn_base, present, start_kb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
start_page = start_kb // 4
total_pages = start_page + nr + 1
with open(path, "wb") as fh:
    for i in range(total_pages):
        if i >= start_page and i < start_page + nr:
            w = (1 << 63) | ((pfn_base + i - start_page) & ((1 << 55) - 1)) if present else 0
        else:
            w = 0
        fh.write(struct.pack("<Q", w))
PYEOF
}

echo "(fake procfs)" > "$FAKE/stat"
cat > "$FAKE/zoneinfo" <<'EOF'
Node 0, zone      DMA32
  per-node stats
    start_pfn  0
Node 1, zone      Normal
  per-node stats
    start_pfn  16384
EOF
mk_rank 1000 0 2 4096 2048 20000 1
mk_rank 1001 1 2 16384 1024 500 1
BUNDLE="$WORK/bundle"

# ============================================================ T1 discover
out=$(AT_PROC_ROOT="$FAKE" AT_SYS_ROOT="/nonexistent" PATH="$(dirname "$PY"):$PATH" \
  bash "$DISCOVER" 2>/dev/null)
check_eq "T1 discover 就绪检查" "DISCOVER_OK" "$out"

# ============================================================ T2 auto rank 发现
"$PY" "$PROBE" --auto --tag after_init --out "$BUNDLE" \
  --proc-root "$FAKE" --sample-ratio 64 >/dev/null 2>&1
v=$?
check_eq "T2a --auto 采集 rc=0" "0" "$v"
check_in "T2b probe.after_init.json 生成" "probe.after_init.json" "$(ls "$BUNDLE" 2>/dev/null)"
v=$("$PY" - "$BUNDLE/probe.after_init.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(len(d["ranks"]), sorted(e["rank"] for e in d["ranks"]))
PYEOF
)
check_eq "T2c 发现 2 rank" "2 [0, 1]" "$v"

# ============================================================ T3 全量模式
BUNDLE3="$WORK/bundle3"
"$PY" "$PROBE" --pid 1000 --tag full_init --out "$BUNDLE3" \
  --proc-root "$FAKE" --full >/dev/null 2>&1
v=$?
check_eq "T3a --full rc=0" "0" "$v"
v=$("$PY" - "$BUNDLE3/probe.full_init.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
e = d["ranks"][0]
print(e["total_present_kb"], sorted(e["node_kb"].keys()), e["vmas"][0]["pathname"])
PYEOF
)
check_eq "T3b 全量 present=8192KB/node1" "8192 ['1'] /dev/devmm_svm" "$v"

# ============================================================ T4 trace.log 可读
check_in "T4a trace.log 头部" "CPU offload 内存采集日志" "$(cat "$BUNDLE/trace.log")"
check_in "T4b trace.log 含 tag" "after_init" "$(cat "$BUNDLE/trace.log")"
check_in "T4c trace.log 含 rank 行" "rank 0" "$(cat "$BUNDLE/trace.log")"

# ============================================================ T5 step 序列追加
"$PY" "$PROBE" --auto --tag step_1 --out "$BUNDLE" \
  --proc-root "$FAKE" --sample-ratio 64 >/dev/null 2>&1
v=$?
check_eq "T5a step_1 追加 rc=0" "0" "$v"
check_in "T5b probe.step_1.json" "probe.step_1.json" "$(ls "$BUNDLE")"

# ============================================================ T6 排除非 devmm 段
v=$("$PY" - "$BUNDLE/probe.after_init.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(sum(1 for e in d["ranks"] for v in e["vmas"] if "other" in v["pathname"]))
PYEOF
)
check_eq "T6 非 devmm 段排除" "0" "$v"

# ============================================================ T7 退出码
"$PY" "$PROBE" --pid 99999 --tag x --out "$WORK/x" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T7a --pid 不存在 → 3" "3" "$v"

# ============================================================ T8 敏感自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SCRIPT_DIR/../SKILL.md" 2>/dev/null | sort -u)
if [ -z "$leak" ]; then ok "T8 无 32+ hex 泄露"; else bad "T8 泄露: $leak"; fi

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
