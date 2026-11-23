#!/usr/bin/env bash
# kptools-thread-affinity-analyzer 回归测试 — 自造 samples fixture
# 不依赖 collector 脚本（独立成立）；依赖：bash 4+、python3（>=3.6）
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

# 文件内容变量（check_in 是内容匹配器）
SK_CONTENT=$(cat "$SCRIPT_DIR/SKILL.md")
DC_CONTENT=$(cat "$SCRIPT_DIR/references/data-contract.md")
TRP_CONTENT=$(cat "$SCRIPT_DIR/references/thread-role-patterns.md")
RJ_CONTENT=$(cat "$SCRIPT_DIR/references/roles.json")

# ------------------------------------------------------------ fixture 工具
# 生成 samples.jsonl（模拟 collector 的输出契约：meta/tick/end）
# 场景：4 NUMA×32 核（node0=0-31 node1=32-63）、NPU 在 node1
#   200 main(python3.11) busy psr=4(node0) allowed=32-63 → C2(main 落点 ∉ NPU)
#   201 mainThread busy psr 交替 0/40(跨 node0/node1) allowed=0-127 → C1 + C5
#   202/203/204 omp_worker busy psr=4 同核 allowed=0-127 → C3 + C5
#   205 worker busy psr=1 掩码变化 0-127→0-7→0-127→0-15 → C4 + C5
#   206 victim idle psr=64(node1) allowed=0-31(node0) → C6 violation
write_samples() { # dir
  local dir=$1
  mkdir -p "$dir"
  "$PY" - "$dir/samples.jsonl" <<'PYEOF'
import json, sys

def make_threads(seq):
    u = seq * 60  # 每 tick +60 → busy 100%
    threads = [
        {"tid":200,"comm":"python3.11","state":"S","psr":4,"utime":u,"stime":0,"allowed":"32-63"},
        {"tid":201,"comm":"mainThread","state":"R","psr":(0 if seq % 2 else 40),
         "utime":u,"stime":0,"allowed":"0-127"},
        {"tid":202,"comm":"omp_worker","state":"R","psr":4,"utime":u,"stime":0,"allowed":"0-127"},
        {"tid":203,"comm":"omp_worker","state":"R","psr":4,"utime":u,"stime":0,"allowed":"0-127"},
        {"tid":204,"comm":"omp_worker","state":"R","psr":4,"utime":u,"stime":0,"allowed":"0-127"},
        {"tid":206,"comm":"victim","state":"S","psr":64,"utime":1,"stime":0,"allowed":"0-31"},
    ]
    allowed_205 = {1:"0-127",2:"0-7",3:"0-127",4:"0-15",5:"0-15",6:"0-7"}.get(seq,"0-127")
    threads.append({"tid":205,"comm":"worker","state":"R","psr":1,
                    "utime":u,"stime":0,"allowed":allowed_205})
    return threads

meta = {"meta":{"skill":"kptools-thread-affinity-collector","version":"1.0.0",
        "pid":200,"interval":0.6,"clk_tck":100,"host":"test","kernel":"5.10",
        "started":1795405522.0,"proc_root":"/proc","auto":False,
        "target_comm":"python3.11"}}
lines = [json.dumps(meta, ensure_ascii=False)]
ts = 1795405522.0
for seq in range(1, 7):
    ts += 0.6
    lines.append(json.dumps({"ts":round(ts,6),"seq":seq,
                             "threads":make_threads(seq)}, ensure_ascii=False))
lines.append(json.dumps({"end":{"ts":round(ts+0.6,6),"reason":"timeout","ticks":6}},
                        ensure_ascii=False))
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$dir/topology_raw.txt" <<'EOF'
=== host
Linux test 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-31
1|32-63
=== l3_domains
=== npu_pci
0000:8f:00.0|1
=== npu_smi_info_l
(absent)
=== target_view
cpuset=/
EOF
}

# ============================================================ T1 frontmatter
for f in "name: kptools-thread-affinity-analyzer" "license: MulanPSL-2.0" \
         "skill_type: tool_assisted" "category: profiling" \
         "domain: thread_affinity" "version:"; do
  check_in "T1 frontmatter [$f]" "$f" "$SK_CONTENT"
done

# ============================================================ T2 章节
for s in "工作流程" "分析命令" "异常判定" "输出契约" "参数速查" "约束" \
         "和同类 skill 的分界" "错误处理" "参考资料"; do
  check_in "T2 章节 [$s]" "$s" "$SK_CONTENT"
done

# ============================================================ T3 数据契约文档存在
for s in "samples.jsonl" "topology_raw.txt" "summary.json" "absent_proc"; do
  check_in "T3 数据契约含 [$s]" "$s" "$DC_CONTENT"
done
check_in "T3b 角色表存在" "python3.11" "$TRP_CONTENT"
check_in "T3c 角色规则存在" "acl_thread" "$RJ_CONTENT"

# ============================================================ T4 综合场景（A1-A6）
S4="$WORK/o4"; write_samples "$S4"
"$PY" "$ANALYZE" "$S4" >/dev/null 2>&1
v=$("$PY" - "$S4/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted(f["id"] for f in d["findings"])))
PYEOF
)
check_eq "T4a findings = A1..A6（A7 不触发）" "A1 A2 A3 A4 A5 A6" "$v"
[ -s "$S4/绑核分析报告.html" ] && ok "T4b HTML 报告非空" || bad "T4b HTML 报告为空"
# T4c A3/A5 的 dict 套 list-of-dicts evidence 不得渲染成 Python repr
if grep -qF "[{'" "$S4/绑核分析报告.html"; then
  bad "T4c evidence 渲染成 Python repr（应为表格）"
else
  ok "T4c evidence 表格化渲染（无 Python repr）"
fi
# T4d A3 挤核证据表格头
if grep -qF "<th>cpu</th>" "$S4/绑核分析报告.html"; then
  ok "T4d A3 挤核证据表格头"
else
  bad "T4d A3 挤核证据表格头 (missing [<th>cpu</th>])"
fi

# ============================================================ T5 A7 线程翻动
S5="$WORK/o5"; mkdir -p "$S5"
"$PY" - "$S5/samples.jsonl" <<'PYEOF'
import json, sys
meta = {"meta":{"skill":"kptools-thread-affinity-collector","version":"1.0.0",
        "pid":400,"interval":0.5,"clk_tck":100,"host":"t","kernel":"5.10",
        "started":1795405522.0,"proc_root":"/proc","auto":False,
        "target_comm":"python3"}}
lines = [json.dumps(meta, ensure_ascii=False)]
ts = 1795405522.0
for k in range(1, 13):
    ts += 0.5
    t = 400 + k
    lines.append(json.dumps({"ts":round(ts,6),"seq":k,"threads":[
        {"tid":t,"comm":"ephemeral","state":"S","psr":0,"utime":1,"stime":0,
         "allowed":"0-127"}]}, ensure_ascii=False))
lines.append(json.dumps({"end":{"ts":round(ts+0.5,6),"reason":"timeout","ticks":12}},
                        ensure_ascii=False))
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$S5/topology_raw.txt" <<'EOF'
=== host
Linux test 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-127
=== l3_domains
=== npu_pci
=== target_view
cpuset=/
EOF
"$PY" "$ANALYZE" "$S5" >/dev/null 2>&1
v=$("$PY" - "$S5/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted(f["id"] for f in d["findings"])))
PYEOF
)
check_eq "T5 线程翻动只报 A7" "A7" "$v"

# ============================================================ T6 numa_node=-1 → C2 跳过
S6="$WORK/o6"; write_samples "$S6"
sed -i 's/^0000:8f:00.0|1$/0000:8f:00.0|-1/' "$S6/topology_raw.txt"
"$PY" "$ANALYZE" "$S6" >/dev/null 2>&1
v=$(findings_ids() { :; }; "$PY" - "$S6/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted(f["id"] for f in d["findings"])))
PYEOF
)
case "$v" in *A2*) bad "T6 NPU 归属未知(-1)时 A2 应跳过（got [$v]）";; *) ok "T6 NPU 节点未知时 A2 跳过";; esac

# ============================================================ T7 退出码
write_samples "$WORK/o7"; "$PY" "$ANALYZE" "$WORK/o7" --out "$WORK/o7" >/dev/null 2>&1
check_eq "T7a 正常退出码 0" "0" "$?"
"$PY" "$ANALYZE" "$WORK/empty_dir_xyz" >/dev/null 2>&1
check_eq "T7b 目录不存在 → 1" "1" "$?"
mkdir -p "$WORK/o7c"; echo "{}" > "$WORK/o7c/samples.jsonl"
"$PY" "$ANALYZE" "$WORK/o7c" >/dev/null 2>&1
check_eq "T7c 无 meta/数据 → 2" "2" "$?"

# ============================================================ T8 evals 引用一致性（幽灵 skill 清除）
for ghost in "kptools-affinity-trace" "train-metrics" "tool-perf-collect"; do
  found=0
  for f in "$SK_CONTENT" "$SCRIPT_DIR/references"/*.md \
           "$SCRIPT_DIR/evals"/*.json; do
    grep -qF "$ghost" "$f" 2>/dev/null && found=1
  done
  if [ "$found" -eq 0 ]; then
    ok "T8 幽灵引用已清除 [$ghost]"
  else
    bad "T8 幽灵引用残留 [$ghost]"
  fi
done
check_in "T8b 分界指向 collector" "kptools-thread-affinity-collector" "$SK_CONTENT"

# ============================================================ T9 敏感信息自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SCRIPT_DIR/SKILL.md" \
       "$SCRIPT_DIR/references"/*.md 2>/dev/null | sort -u)
[ -z "$leak" ] && ok "T9 无 32+ 位 hex 泄露" || bad "T9 疑似泄露: $leak"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
