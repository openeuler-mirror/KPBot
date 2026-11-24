#!/usr/bin/env bash
# kptools-cpuoffload-analyzer 回归测试 — 自造 bundle fixture
# 依赖：bash 4+、python3（>=3.6）。不使用 GNU 专属语法（BSD sed 兼容）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANALYZE="$SCRIPT_DIR/scripts/analyze.py"

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

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

jq_() { # jq_ <py-expr on d> <json file>
  "$PY" - "$2" "$1" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {"d": d}))
PYEOF
}

# ---------------------------------------------------------------- fixture
mk_bundle() { # dir
  local d=$1
  mkdir -p "$d"
  # probe.after_init.json: rank0 2.0 GiB, rank1 1.5 GiB → 总 3.5
  "$PY" - "$d/probe.after_init.json" <<'PYEOF'
import json, sys
d = {
  "skill": "kptools-cpuoffload-collector", "version": "1.1.0",
  "tag": "after_init", "ts": 1795400000, "pfn_readable": True,
  "sample_ratio": 64, "partial": False, "missing": [],
  "ranks": [
    {"pid": 1000, "rank": 0, "ts": 1795400000.1, "mode": "sampled_1_64",
     "total_present_kb": 2*1048576, "total_va_kb": 4*1048576,
     "node_kb": {"0": 1.6*1048576, "1": 0.4*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*2*1048576, "present_kb": 2*1048576,
               "node_kb": {"0": 1.6*1048576, "1": 0.4*1048576},
               "sampled": {"samples": 32, "step": 64}}]},
    {"pid": 1001, "rank": 1, "ts": 1795400000.2, "mode": "sampled_1_64",
     "total_present_kb": 1.5*1048576, "total_va_kb": 2*1048576,
     "node_kb": {"1": 1.5*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*1048576, "present_kb": 1.5*1048576,
               "node_kb": {"1": 1.5*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
  # probe.step_1.json: +1.0 GiB（nova slab 首次）
  "$PY" - "$d/probe.step_1.json" <<'PYEOF'
import json, sys
d = {
  "skill": "kptools-cpuoffload-collector", "version": "1.1.0",
  "tag": "step_1", "ts": 1795400100, "pfn_readable": True,
  "sample_ratio": 64, "partial": False, "missing": [],
  "ranks": [
    {"pid": 1000, "rank": 0, "ts": 1795400100.1, "mode": "sampled_1_64",
     "total_present_kb": 3*1048576, "total_va_kb": 5*1048576,
     "node_kb": {"0": 2.4*1048576, "1": 0.6*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1400000, "pathname": "/dev/devmm_svm",
               "size_kb": 4*1048576, "present_kb": 3*1048576,
               "node_kb": {"0": 2.4*1048576, "1": 0.6*1048576}}]},
    {"pid": 1001, "rank": 1, "ts": 1795400100.2, "mode": "sampled_1_64",
     "total_present_kb": 1.5*1048576, "total_va_kb": 2*1048576,
     "node_kb": {"1": 1.5*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*1048576, "present_kb": 1.5*1048576,
               "node_kb": {"1": 1.5*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
}

# ============================================================ T1 基本分析
B1="$WORK/b1"; mk_bundle "$B1"
"$PY" "$ANALYZE" "$B1" >"$WORK/t1.md" 2>/dev/null
v=$?
check_eq "T1a 基本分析 rc=0" "0" "$v"
R1="$WORK/b1_report"
[ -f "$R1/summary.json" ] && ok "T1b summary.json 生成（bundle 同级 _report）" || bad "T1b summary.json 缺失"
[ ! -f "$B1/summary.json" ] && ok "T1b2 bundle 只读（无 summary 写入）" || bad "T1b2 bundle 被写入"
v=$(jq_ "d['probes'], d['self_checks']['passed'], len(d['findings']), round(d['timeline'][-1]['total_gib'], 1)" "$R1/summary.json")
check_eq "T1c 2 快照/自检通过/findings/总量" "(['after_init', 'step_1'], True, 1, 4.5)" "$v"

# ============================================================ T2 时间演化
v=$(jq_ "round(d['timeline'][0]['total_gib'], 1), round(d['timeline'][1]['total_gib'], 1)" "$R1/summary.json")
check_eq "T2 时间演化 after_init=3.5 step_1=4.5" "(3.5, 4.5)" "$v"

# ============================================================ T3 nova 重建检测（共同 rank 对齐）
v=$(jq_ "len([f for f in d['findings'] if 'nova' in f or '增量' in f])" "$R1/summary.json")
check_eq "T3 nova 增量检测（+1 GiB）" "1" "$v"

# ============================================================ T4 自检失败场景 → rc=3
B4="$WORK/b4"; mkdir -p "$B4"
"$PY" - "$B4/probe.bad.json" <<'PYEOF'
import json, sys
d = {
  "tag": "bad", "ts": 1795400000, "pfn_readable": True,
  "ranks": [
    {"pid": 1000, "rank": 0, "total_present_kb": 2*1048576,
     "total_va_kb": 4*1048576,
     "node_kb": {"0": 1*1048576},   # 故意只给 1.0 GiB，与 total 2.0 不匹配
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 6*1048576, "present_kb": 2*1048576,
               "node_kb": {"0": 1*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
"$PY" "$ANALYZE" "$B4" >"$WORK/t4.md" 2>/dev/null
v=$?
check_eq "T4a 自检失败 → rc=3" "3" "$v"
v=$(jq_ "(d['self_checks']['passed'], len(d['self_checks']['issues']))" "$WORK/b4_report/summary.json")
check_eq "T4b 自检失败（node 不自洽，rank 级+VMA 级各触发）" "(False, 2)" "$v"
check_in "T4c 报告仍标注不作数" "不作数" "$(cat "$WORK/t4.md")"

# ============================================================ T5 退出码
"$PY" "$ANALYZE" "$WORK/empty" >/dev/null 2>&1
v=$?
check_eq "T5a 目录不存在 → 1" "1" "$v"
mkdir -p "$WORK/empty2"
"$PY" "$ANALYZE" "$WORK/empty2" >/dev/null 2>&1
v=$?
check_eq "T5b 空 bundle → 2" "2" "$v"
"$PY" "$ANALYZE" >/dev/null 2>&1
v=$?
check_eq "T5c 缺 bundle 参数 → 1（不与 2 撞码）" "1" "$v"
"$PY" "$ANALYZE" "$B1" --no-such-flag >/dev/null 2>&1
v=$?
check_eq "T5d 未知 flag → 1" "1" "$v"

# ============================================================ T7 快照按 ts 排序（非文件名字典序）
B7="$WORK/b7"; mkdir -p "$B7"
"$PY" - "$B7" <<'PYEOF'
import json, sys
base = {"skill": "x", "version": "1.1.0", "pfn_readable": True,
        "partial": False, "missing": [],
        "ranks": [{"pid": 1000, "rank": 0, "total_present_kb": 1048576,
                   "total_va_kb": 1048576, "node_kb": {"0": 1048576},
                   "vmas": [{"start": 1, "end": 2, "pathname": "/dev/devmm_svm",
                             "size_kb": 1048576, "present_kb": 1048576,
                             "node_kb": {"0": 1048576}}]}]}
# 文件名字典序 step_1 < step_10 < step_2，但 ts 故意 step_2 早于 step_10
for tag, ts in (("step_1", 100), ("step_10", 300), ("step_2", 200)):
    d = dict(base); d["tag"] = tag; d["ts"] = ts
    with open(sys.argv[1] + "/probe.%s.json" % tag, "w") as fh:
        json.dump(d, fh)
PYEOF
"$PY" "$ANALYZE" "$B7" >/dev/null 2>&1
v=$(jq_ "d['probes']" "$WORK/b7_report/summary.json")
check_eq "T7 快照按 ts 排序（step_2 先于 step_10）" "['step_1', 'step_2', 'step_10']" "$v"

# ============================================================ T8 L1 存活配对交叉核对（正常不误报）
B8="$WORK/b8"; mk_bundle "$B8"
"$PY" - "$B8/alloc.1000.jsonl" <<'PYEOF'
import json, sys
# 分配 2GiB（存活）+ 分配 1GiB 后 free（不存活）——累计 3GiB，存活 2GiB
# + after_init→step_1 期间 nova 首次分配 1GiB（step_1 快照 3GiB 对齐）
recs = [
  {"kind": "alloc", "source": "PinnedFlatPool", "method": "allocate",
   "size": 2*1024**3, "result_ptr": "0x1000", "t_ns": 1795399000*10**9, "pid": 1000},
  {"kind": "alloc", "source": "CpuMemoryPool", "method": "allocate",
   "size": 1*1024**3, "result_ptr": "0x2000", "t_ns": 1795399500*10**9, "pid": 1000},
  {"kind": "free", "source": "CpuMemoryPool", "method": "free",
   "size": None, "arg_ptr": "0x2000", "t_ns": 1795399999*10**9, "pid": 1000},
  {"kind": "alloc", "source": "CpuMemoryPool", "method": "_allocate_slab",
   "size": 1*1024**3, "result_ptr": "0x3000", "t_ns": 1795400050*10**9, "pid": 1000},
]
with open(sys.argv[1], "w") as fh:
    for r in recs:
        fh.write(json.dumps(r) + "\n")
PYEOF
# marks：3 个 opt_step
"$PY" - "$B8/marks.1000.jsonl" <<'PYEOF'
import json, sys
with open(sys.argv[1], "w") as fh:
    for i in range(3):
        fh.write(json.dumps({"kind": "opt_step_begin", "step": i+1,
                             "t_ns": 1795399800*10**9 + i, "pid": 1000}) + "\n")
PYEOF
"$PY" "$ANALYZE" "$B8" >/dev/null 2>&1
v=$?
check_eq "T8a L1 配对自检过 rc=0" "0" "$v"
v=$(jq_ "d['self_checks']['passed']" "$WORK/b8_report/summary.json")
check_eq "T8b 存活 2GiB vs pagemap 2GiB → 自检通过（累计 3GiB 不误报）" "True" "$v"
v=$(jq_ "(d['l1_steps'].get('1000'), sorted(d['l1_pools'].keys()))" "$WORK/b8_report/summary.json")
check_eq "T8c marks 步数 + 池拆分" "(3, ['CpuMemoryPool', 'PinnedFlatPool'])" "$v"

# ============================================================ T9 L1 存活偏差 → 自检 2 issue
B9="$WORK/b9"; mk_bundle "$B9"
"$PY" - "$B9/alloc.1000.jsonl" <<'PYEOF'
import json, sys
# 存活 3GiB vs pagemap 2GiB → 偏差 50% → issue
recs = [
  {"kind": "alloc", "source": "PinnedFlatPool", "method": "allocate",
   "size": 3*1024**3, "result_ptr": "0x1000", "t_ns": 1795399000*10**9, "pid": 1000},
]
with open(sys.argv[1], "w") as fh:
    for r in recs:
        fh.write(json.dumps(r) + "\n")
PYEOF
"$PY" "$ANALYZE" "$B9" >/dev/null 2>&1
v=$?
check_eq "T9a L1 偏差 rc=3" "3" "$v"
v=$(jq_ "len([i for i in d['self_checks']['issues'] if 'alloc' in i])" "$WORK/b9_report/summary.json")
check_eq "T9b 自检 2 报存活偏差" "1" "$v"

# ============================================================ T10 --out 显式目录
"$PY" "$ANALYZE" "$B1" --out "$WORK/myreport" >/dev/null 2>&1
v=$?
check_eq "T10a --out rc=0" "0" "$v"
[ -f "$WORK/myreport/summary.json" ] && ok "T10b --out 生成 summary" || bad "T10b --out 缺 summary"

# ============================================================ T11 markdown 正文断言
check_in "T11a 报告含三自检节" "## 三自检" "$(cat "$WORK/t1.md")"
check_in "T11b 报告含每 rank node 分布节" "## 每 rank node 分布" "$(cat "$WORK/t1.md")"
check_in "T11c node 分布 GiB 口径（非 %%）" "node 分布 (GiB)" "$(cat "$WORK/t1.md")"
check_in "T11d 快照列存在" "| 快照 |" "$(cat "$WORK/t1.md")"
check_in "T11e 口径声明" "## 口径声明" "$(cat "$WORK/t1.md")"

# ============================================================ T12 解析缺口披露（jsonl 尾部半行）
# 用未参与交叉核对的 pid（9999 不在 probe ranks 里），只测解析警告路径
B12="$WORK/b12"; mk_bundle "$B12"
printf '{"kind": "alloc", "source": "PinnedFlatPool", "method": "allocate", "size": 524288, "result_ptr": "0x3000", "t_ns": 1795399000000000000, "pid": 9999}\n{"kind": "alloc", "size": 1048576, "t_ns": 1795399' > "$B12/alloc.9999.jsonl"
"$PY" "$ANALYZE" "$B12" >/dev/null 2>&1
v=$?
check_eq "T12a 尾部半行不致 rc!=0" "0" "$v"
v=$(jq_ "len([w for w in d['parse_warnings'] if '半行' in w or '无法解析' in w])" "$WORK/b12_report/summary.json")
check_eq "T12b parse_warnings 披露坏行" "1" "$v"

# ============================================================ T13 rank 集合变化不误报驱逐
B13="$WORK/b13"; mkdir -p "$B13"
"$PY" - "$B13" <<'PYEOF'
import json, sys
base_rank = lambda pid, rank, kb: {"pid": pid, "rank": rank,
    "total_present_kb": kb, "total_va_kb": kb,
    "node_kb": {"0": kb},
    "vmas": [{"start": 1, "end": 2, "pathname": "/dev/devmm_svm",
              "size_kb": kb, "present_kb": kb, "node_kb": {"0": kb}}]}
hdr = {"skill": "x", "version": "1.1.0", "pfn_readable": True,
       "partial": False, "missing": []}
# 快照 A：2 rank 共 3GiB；快照 B：只采到 rank0（同为 2GiB，rank1 消失）
dA = dict(hdr); dA["tag"]="a"; dA["ts"]=1
dA["ranks"] = [base_rank(1000, 0, 2*1048576), base_rank(1001, 1, 1*1048576)]
dB = dict(hdr); dB["tag"]="b"; dB["ts"]=2
dB["ranks"] = [base_rank(1000, 0, 2*1048576)]
for name, d in (("probe.a.json", dA), ("probe.b.json", dB)):
    with open(sys.argv[1] + "/" + name, "w") as fh:
        json.dump(d, fh)
PYEOF
"$PY" "$ANALYZE" "$B13" >/dev/null 2>&1
v=$(jq_ "[f for f in d['findings'] if '骤降' in f]" "$WORK/b13_report/summary.json")
check_eq "T13a rank1 消失不误报'骤降/驱逐'" "[]" "$v"
v=$(jq_ "len([f for f in d['findings'] if 'rank 集合变化' in f])" "$WORK/b13_report/summary.json")
check_eq "T13b 集合变化单独披露" "1" "$v"

# ============================================================ T14 显式零值 → 无法验证
B14="$WORK/b14"; mkdir -p "$B14"
"$PY" - "$B14" <<'PYEOF'
import json, sys
d = {"skill": "x", "version": "1.1.0", "tag": "z", "ts": 1,
     "pfn_readable": False,
     "ranks": [{"pid": 1000, "rank": 0, "total_present_kb": 1048576,
                "total_va_kb": 2*1048576,
                "node_kb": {},       # 显式零：present>0 但 node 全零
                "vmas": [{"start": 1, "end": 2, "pathname": "/dev/devmm_svm",
                          "size_kb": 2*1048576, "present_kb": 1048576,
                          "node_kb": {}}]}]}
with open(sys.argv[1] + "/probe.z.json", "w") as fh:
    json.dump(d, fh)
PYEOF
"$PY" "$ANALYZE" "$B14" >/dev/null 2>&1
v=$?
check_eq "T14a node 全零 → rc=3（无法验证）" "3" "$v"
v=$(jq_ "len([i for i in d['self_checks']['issues'] if '无法验证' in i])" "$WORK/b14_report/summary.json")
check_eq "T14b 无法验证单列（rank 级+VMA 级）" "2" "$v"

# ============================================================ T15 自检 2 无 L1 → 跳过（不显示全通过）
v=$(jq_ "d['self_checks']['skipped']" "$R1/summary.json")
check_eq "T15a 无 L1 时自检 2 标跳过" "['自检 2 跳过：无 L1 埋点（alloc.*.jsonl）']" "$v"

# ============================================================ T16 敏感自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SCRIPT_DIR/SKILL.md" 2>/dev/null | sort -u)
[ -z "$leak" ] && ok "T16 无 32+ hex 泄露" || bad "T16 泄露: $leak"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
