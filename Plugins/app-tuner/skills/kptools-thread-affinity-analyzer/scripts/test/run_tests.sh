#!/usr/bin/env bash
# kptools-thread-affinity-analyzer 回归测试 — 自造 samples fixture
# 不依赖 collector 脚本（独立成立）；依赖：bash 4+、python3（>=3.6）
# 不使用 GNU 专属语法（BSD sed 兼容）
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

# 文件内容变量（check_in 是内容匹配器）
SK_CONTENT=$(cat "$SCRIPT_DIR/SKILL.md")
DC_CONTENT=$(cat "$SCRIPT_DIR/references/data-contract.md")
TRP_CONTENT=$(cat "$SCRIPT_DIR/references/thread-role-patterns.md")
RJ_CONTENT=$(cat "$SCRIPT_DIR/references/roles.json")

# ------------------------------------------------------------ fixture 工具
# 生成 samples.jsonl（模拟 collector 1.1.0 的输出契约：meta/tick/end）
# 场景：2 NUMA（node0=0-31 node1=32-63）、NPU 在 node1
#   200 main(python3.11) busy psr=4(node0) allowed=32-63 → A2(main 落点 ∉ NPU)
#   201 mainThread busy psr 交替 0/40(跨 node0/node1) allowed=0-127 → A1
#   202/203/204 omp_worker busy psr=4 同核 allowed=0-127 → A3 + A5
#   205 worker busy psr=1 掩码变化 0-127→0-7→0-127→0-15 → A4 + A5
#   206 victim idle psr=64(node1) allowed=0-31(node0) → A6 violation
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

meta = {"meta":{"skill":"kptools-thread-affinity-collector","version":"1.1.0",
        "pid":200,"interval":0.6,"clk_tck":100,"host":"test","kernel":"5.10",
        "started":1795405522.0,"proc_root":"/proc","auto":False,
        "target_comm":"python3.11","starttime":1000}}
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
Linux 5.10.0-60.18.0
openEuler 22.03 LTS
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
=== target_env
OMP_NUM_THREADS=8
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
for s in "samples.jsonl" "topology_raw.txt" "summary.json" "absent_proc" \
         "npu_smi_info_l" "target_env" "interval_s" "migrations_lower_bound" \
         "data_ticks" "starttime"; do
  check_in "T3 数据契约含 [$s]" "$s" "$DC_CONTENT"
done
check_in "T3b 角色表存在" "python3.11" "$TRP_CONTENT"
check_in "T3c 角色规则存在" "acl_thread" "$RJ_CONTENT"

# ============================================================ T4 综合场景（A1-A6）
S4="$WORK/o4"; write_samples "$S4"
"$PY" "$ANALYZE" "$S4" >/dev/null 2>&1
v=$(jq_ "' '.join(sorted(f['id'] for f in d['findings']))" "$S4/summary.json")
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
# T4e host 两行契约：host=发行版名，kernel=版本串
v=$(jq_ "(d['environment']['host'], d['environment']['kernel'])" "$S4/summary.json")
check_eq "T4e host 取 PRETTY_NAME、kernel 取 uname -r" "('openEuler 22.03 LTS', '5.10.0-60.18.0')" "$v"

# ============================================================ T5 A7 线程翻动
S5="$WORK/o5"; mkdir -p "$S5"
"$PY" - "$S5/samples.jsonl" <<'PYEOF'
import json, sys
meta = {"meta":{"skill":"kptools-thread-affinity-collector","version":"1.1.0",
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
Linux 5.10.0
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
v=$(jq_ "' '.join(sorted(f['id'] for f in d['findings']))" "$S5/summary.json")
check_eq "T5 线程翻动只报 A7" "A7" "$v"

# ============================================================ T6 numa_node=-1 → A2 跳过
S6="$WORK/o6"; write_samples "$S6"
sed 's/^0000:8f:00.0|1$/0000:8f:00.0|-1/' "$S6/topology_raw.txt" \
  > "$S6/topology_raw.txt.new" && mv "$S6/topology_raw.txt.new" "$S6/topology_raw.txt"
"$PY" "$ANALYZE" "$S6" >/dev/null 2>&1
v=$(jq_ "' '.join(sorted(f['id'] for f in d['findings']))" "$S6/summary.json")
case "$v" in *A2*) bad "T6 NPU 归属未知(-1)时 A2 应跳过（got [$v]）";; *) ok "T6 NPU 节点未知时 A2 跳过";; esac

# ============================================================ T7 退出码
write_samples "$WORK/o7"; "$PY" "$ANALYZE" "$WORK/o7" --out "$WORK/o7" >/dev/null 2>&1
check_eq "T7a 正常退出码 0" "0" "$?"
"$PY" "$ANALYZE" "$WORK/empty_dir_xyz" >/dev/null 2>&1
check_eq "T7b 目录不存在 → 1" "1" "$?"
mkdir -p "$WORK/o7c"; echo "{}" > "$WORK/o7c/samples.jsonl"
"$PY" "$ANALYZE" "$WORK/o7c" >/dev/null 2>&1
check_eq "T7c 无 meta/数据 → 2" "2" "$?"
"$PY" "$ANALYZE" >/dev/null 2>&1
check_eq "T7d 缺采样目录参数 → 1（argparse 不与 2 撞码）" "1" "$?"
"$PY" "$ANALYZE" "$WORK/o7" --no-such-flag >/dev/null 2>&1
check_eq "T7e 未知 flag → 1" "1" "$?"

# ============================================================ T8 幽灵引用检查（stdin 匹配，非文件名操作数）
for ghost in "kptools-affinity-trace" "affinity_trace.sh" "train-metrics" \
             "tool-perf-collect" "build_report"; do
  found=0
  # SK_CONTENT 是内容字符串——必须经 stdin 匹配（当文件名 grep 是 no-op）
  printf '%s' "$SK_CONTENT" | grep -qF "$ghost" && found=1
  for f in "$SCRIPT_DIR/references"/*.md "$SCRIPT_DIR/references"/*.json; do
    grep -qF "$ghost" "$f" 2>/dev/null && found=1
  done
  grep -qF "$ghost" "$SCRIPT_DIR/scripts/analyze.py" 2>/dev/null && found=1
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

# ============================================================ T10 topology 缺失 → 3（默认不读分析机 /sys）
S10="$WORK/o10"; mkdir -p "$S10"
"$PY" - "$S10/samples.jsonl" <<'PYEOF'
import json, sys
meta = {"meta":{"pid":500,"interval":0.5,"clk_tck":100,"target_comm":"p",
                "started":1795405522.0}}
lines = [json.dumps(meta)]
ts = 1795405522.0
for seq in range(1, 4):
    ts += 0.5
    lines.append(json.dumps({"ts":ts,"seq":seq,"threads":[
        {"tid":500,"comm":"p","state":"S","psr":0,"utime":seq*50,"stime":0,
         "allowed":"0-127"}]}))
with open(sys.argv[1], "w") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
"$PY" "$ANALYZE" "$S10" >/dev/null 2>&1
check_eq "T10a topology_raw.txt 缺失 → 3（不静默补采）" "3" "$?"
# 显式 opt-in 才允许本机 /sys（dev env 有 /sys，应成功且 source=sys）
"$PY" "$ANALYZE" "$S10" --allow-sys-fallback >/dev/null 2>&1
v=$?
check_eq "T10b --allow-sys-fallback 同机补采成功" "0" "$v"
if [ "$v" = "0" ]; then
  v=$(jq_ "d['environment']['topology_source']" "$S10/summary.json")
  check_eq "T10c 拓扑来源标注 sys" "sys" "$v"
fi
# topology 存在但 node_cpulist 垃圾 → 不再静默：summary 披露 numa_mapping=false
S10d="$WORK/o10d"; mkdir -p "$S10d"
cp "$S10/samples.jsonl" "$S10d/"
printf '=== host\nLinux 5.10.0\n=== node_cpulist\n garbage\n' > "$S10d/topology_raw.txt"
"$PY" "$ANALYZE" "$S10d" >/dev/null 2>&1
v=$?
check_eq "T10d 垃圾 node_cpulist → 0（A3-A7 仍可判）" "0" "$v"
v=$(jq_ "(d['data_quality']['numa_mapping'], d['data_quality']['topology_gap'] is not None)" "$S10d/summary.json")
check_eq "T10e 拓扑缺口披露（不再静默跳过）" "(False, True)" "$v"

# ============================================================ T11 roles.json 结构非法 → 回退不崩溃
S11="$WORK/o11"; write_samples "$S11"
printf '[{"match":"exact","pattern":"x","role":"y"}]' > "$WORK/bad_roles.json"
"$PY" "$ANALYZE" "$S11" --roles "$WORK/bad_roles.json" >/dev/null 2>&1
v=$?
check_eq "T11a 顶层数组的 roles.json → 回退不崩溃 rc=0" "0" "$v"
v=$(jq_ "d['role_config']['source']" "$S11/summary.json")
check_eq "T11b 回退来源标注 fallback" "fallback" "$v"

# ============================================================ T12 A2 按 NUMA 节点汇总取主（防单核计数误判）
S12="$WORK/o12"; mkdir -p "$S12"
"$PY" - "$S12/samples.jsonl" <<'PYEOF'
import json, sys
# acl_thread：node0 两核各 30 活跃样本（60）vs node1 单核 40
# NPU 在 node0 → 主要节点=node0（多数样本）→ 不应报 A2
meta = {"meta":{"pid":900,"interval":0.5,"clk_tck":100,"target_comm":"p",
                "started":1795405522.0}}
lines = [json.dumps(meta)]
ts = 1795405522.0
seq = 0
# 100 个 tick：psr 0/1 各 30 次忙，psr 32 40 次忙
for i in range(100):
    ts += 0.5
    seq += 1
    if i < 30:
        psr = 0
    elif i < 60:
        psr = 1
    else:
        psr = 32
    lines.append(json.dumps({"ts":ts,"seq":seq,"threads":[
        {"tid":901,"comm":"acl_thread","state":"R","psr":psr,
         "utime":seq*60,"stime":0,"allowed":"0-63"}]}))
with open(sys.argv[1], "w") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$S12/topology_raw.txt" <<'EOF'
=== host
Linux 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-31
1|32-63
=== npu_pci
0000:8f:00.0|0
EOF
"$PY" "$ANALYZE" "$S12" >/dev/null 2>&1
v=$(jq_ "[f['id'] for f in d['findings'] if f['id'] == 'A2']" "$S12/summary.json")
check_eq "T12 节点汇总取主（60 vs 40）→ 不误报 A2" "[]" "$v"

# ============================================================ T13 A1u：掩码不可读的跨 node 忙线程
S13="$WORK/o13"; mkdir -p "$S13"
"$PY" - "$S13/samples.jsonl" <<'PYEOF'
import json, sys
meta = {"meta":{"pid":800,"interval":0.5,"clk_tck":100,"target_comm":"p",
                "started":1795405522.0}}
lines = [json.dumps(meta)]
ts = 1795405522.0
for seq in range(1, 9):
    ts += 0.5
    lines.append(json.dumps({"ts":ts,"seq":seq,"threads":[
        # 忙线程掩码 "?"（不可读），psr 交替 node0/node1
        {"tid":801,"comm":"mystery","state":"R","psr":(0 if seq % 2 else 40),
         "utime":seq*60,"stime":0,"allowed":"?"},
        # 主线程占位（不参与 A1u）
        {"tid":800,"comm":"p","state":"S","psr":0,"utime":seq*5,"stime":0,
         "allowed":"0-63"}]}))
with open(sys.argv[1], "w") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$S13/topology_raw.txt" <<'EOF'
=== host
Linux 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-31
1|32-63
=== npu_pci
EOF
"$PY" "$ANALYZE" "$S13" >/dev/null 2>&1
v=$(jq_ "' '.join(sorted(f['id'] for f in d['findings']))" "$S13/summary.json")
check_eq "T13 掩码不可读跨 node → A1u（非 A1）" "A1u" "$v"

# ============================================================ T14 A5：线程先退出后主线程才收紧 → 不误报
S14="$WORK/o14"; mkdir -p "$S14"
"$PY" - "$S14/samples.jsonl" <<'PYEOF'
import json, sys
# worker(801) 在 tick 1-3 在场（掩码 0-127），tick 4 起消失；
# 主线程(800) 在 tick 5 才收紧到 0-7 —— 拿各自"最终掩码"硬比会误报 A5
meta = {"meta":{"pid":800,"interval":0.5,"clk_tck":100,"target_comm":"p",
                "started":1795405522.0}}
lines = [json.dumps(meta)]
ts = 1795405522.0
for seq in range(1, 7):
    ts += 0.5
    threads = [{"tid":800,"comm":"p","state":"S","psr":0,
                "utime":seq*60,"stime":0,
                "allowed":("0-7" if seq >= 5 else "0-127")}]
    if seq <= 3:
        threads.append({"tid":801,"comm":"hccl_worker","state":"R","psr":1,
                        "utime":seq*60,"stime":0,"allowed":"0-127"})
    lines.append(json.dumps({"ts":ts,"seq":seq,"threads":threads}))
with open(sys.argv[1], "w") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$S14/topology_raw.txt" <<'EOF'
=== host
Linux 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-127
=== npu_pci
EOF
"$PY" "$ANALYZE" "$S14" >/dev/null 2>&1
v=$(jq_ "[f['id'] for f in d['findings'] if f['id'] == 'A5']" "$S14/summary.json")
check_eq "T14 线程先退出后主线程收紧 → 不误报 A5" "[]" "$v"

# ============================================================ T15 A3：三个分时线程（<忙阈值）不判挤核
S15="$WORK/o15"; mkdir -p "$S15"
"$PY" - "$S15/samples.jsonl" <<'PYEOF'
import json, sys
# 三个线程各 ~40% 活跃（≥20% 计活跃样本）但 <50% 忙阈值，
# 同核分时——旧按计数口径会误判 A3，新口径要求忙实例 ≥3
meta = {"meta":{"pid":700,"interval":0.5,"clk_tck":100,"target_comm":"p",
                "started":1795405522.0}}
lines = [json.dumps(meta)]
ts = 1795405522.0
for seq in range(1, 11):
    ts += 0.5
    threads = [{"tid":700,"comm":"p","state":"S","psr":0,
                "utime":seq*5,"stime":0,"allowed":"0-127"}]
    for t in (701, 702, 703):
        threads.append({"tid":t,"comm":"share","state":"R","psr":4,
                        "utime":seq*20,"stime":0,"allowed":"0-127"})
    lines.append(json.dumps({"ts":ts,"seq":seq,"threads":threads}))
with open(sys.argv[1], "w") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
cat > "$S15/topology_raw.txt" <<'EOF'
=== host
Linux 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-127
=== npu_pci
EOF
"$PY" "$ANALYZE" "$S15" >/dev/null 2>&1
v=$(jq_ "[f['id'] for f in d['findings'] if f['id'] == 'A3']" "$S15/summary.json")
check_eq "T15 三个 ~42% 分时线程 → 不误判 A3 挤核" "[]" "$v"

# ============================================================ T16 原子写入 + 符号链接不截断
S16="$WORK/o16"; write_samples "$S16"
echo "PRECIOUS" > "$WORK/precious.txt"
ln -s "$WORK/precious.txt" "$S16/绑核分析报告.html"
"$PY" "$ANALYZE" "$S16" >/dev/null 2>&1
v=$?
check_eq "T16a 预置 symlink 时报告生成 rc=0" "0" "$v"
check_eq "T16b symlink 指向的文件不被截断" "PRECIOUS" "$(cat "$WORK/precious.txt")"
if [ -L "$S16/绑核分析报告.html" ]; then
  bad "T16c 报告仍为符号链接（应已替换为真实文件）"
else
  ok "T16c 符号链接已被真实报告替换"
fi
if ls "$S16"/*.tmp >/dev/null 2>&1; then
  bad "T16d 残留 .tmp 文件"
else
  ok "T16d 无 .tmp 残留（原子替换）"
fi

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
