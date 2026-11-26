#!/bin/bash
# test_find_optimal_queues.sh — network-optimization 寻优 UT（skill-ut-supplement）
# 对象：find_optimal_queues.sh（队列二分寻优 + max_combined 解析 + 饱和度/收敛决策）
# 方式：mock bin（ethtool/mpstat）+ echo 型 bench；假 iface 无 msi_irqs → 无 /proc/irq 写入，
#       systemctl no-op；pgrep 输出 PID 触发 taskset 调用，taskset 模拟真机 stdout 并落盘参数
#       （污染被脚本重定向拦截 + Phase2 应用绑核恢复可断言，锁住 PR#194 修复与 mxj1314 检视 #3）；
#       FQ-05 用会打印 stdout 的 systemctl mock 复现外部命令泄漏 → 整行契约拒收（mxj1314 检视 #2）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d /tmp/fq-ut.XXXXXX)"
trap 'kill ${APP_PID:-} 2>/dev/null; rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_fail()    { if [ "$1" -ne 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (期望非零)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_not_contains(){ if echo "$1" | grep -qF -- "$2"; then red "  [FAIL] $3 (不应包含: $2)"; TEST_FAILED=$((TEST_FAILED+1)); else green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

command -v bc >/dev/null || { echo "需要 bc"; exit 1; }

# —— mock bin ——
MOCK="$WORK/bin"; mkdir -p "$MOCK"
cat > "$MOCK/ethtool" <<'EOF'
#!/bin/bash
if [ "$1" = "-l" ]; then
  # 有状态 mock：-l 报告末次 -L 设定的当前值（模型化真实 ethtool，供脚本 Phase2 恢复核验消费）
  cur=4
  [ -s "${ETHTOOL_STATE}.cur" ] && cur=$(cat "${ETHTOOL_STATE}.cur")
  printf 'Current settings:\nCombined:  %s\nPre-set maximums:\nRX: 0\nTX: 0\nCombined: 63\n' "$cur"
else  # -L combined N：记录完整参数（$* 已含 -L，勿重复前缀）
  echo "$*" >> "$ETHTOOL_STATE"
  echo "$4" > "${ETHTOOL_STATE}.cur"
  exit 0
fi
EOF
cat > "$MOCK/mpstat" <<'EOF'
#!/bin/bash
# 忽略 -P 参数；输出饱和小样本（idle<1 且 soft+irq>30 → sat 命中）
echo "Linux mock (384 CPU)"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:01" "CPU" "%usr" "%nice" "%sys" "%iowait" "%irq" "%soft" "%steal" "%guest" "%gnice" "%idle"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:02" "5" "0.00" "0.00" "1.00" "0.00" "5.00" "40.00" "0.00" "0.00" "0.00" "0.50"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:03" "5" "0.00" "0.00" "1.00" "0.00" "5.00" "40.00" "0.00" "0.00" "0.00" "0.50"
EOF
printf '#!/bin/bash\nexit 0\n' > "$MOCK/systemctl"
# 真实存活 PID：脚本的读回核验先经 /proc 判存活（mxj1314 16:07 复核），mock PID 必须真实存在
setsid sleep 300 >/dev/null 2>&1 &
APP_PID=$!
# pgrep 输出 PID：保证 taskset -pc 真实被调用，污染场景才成立
printf '#!/bin/bash\necho %s\nexit 0\n' "$APP_PID" > "$MOCK/pgrep"
# taskset mock：模型化 util-linux 双模式——
#   读模式 taskset -pc <pid> → "pid N's current affinity list: <cur>"（复核 #3 的读回核验消费此输出）；
#   设模式 taskset -pc <range> <pid> → current+new 两行（stdout 污染场景复现，被脚本重定向拦截）。
# 落盘调用参数 + $TASKSET_CUR 记录当前掩码
cat > "$MOCK/taskset" <<'EOF'
#!/bin/bash
echo "$*" >> "$TASKSET_STATE"
cur="0-383"
[ -s "$TASKSET_CUR" ] && cur=$(cat "$TASKSET_CUR")
if [ "$#" -eq 2 ]; then
  echo "pid $2's current affinity list: $cur"
else
  echo "pid $3's current affinity list: $cur"
  echo "$2" > "$TASKSET_CUR"
  echo "pid $3's new affinity list: $2"
fi
exit 0
EOF
chmod +x "$MOCK"/*
IFACE="fqut-no-such-iface"   # 假接口：无 device/msi_irqs → check_irq_balance 零写入

run_fq() { # $1=bench-cmd 其余透传
    local bench="$1"; shift
    export ETHTOOL_STATE="$WORK/ethtool.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
    export TASKSET_STATE="$WORK/taskset.state"; : > "$TASKSET_STATE"
    export TASKSET_CUR="$WORK/taskset.cur"; rm -f "$TASKSET_CUR"
    PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
        --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "$bench" "$@" 2>&1
}

echo "===== find_optimal_queues UT ====="

echo "FQ-01: 参数校验——缺必填参数"
PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" --iface x >/dev/null 2>&1
assert_fail $? "FQ-01: 缺参 exit 1"

echo "FQ-02: 饱和首发 → ADD → 收敛决策链（固定 QPS）→ 最优=首个队列"
OUT=$(run_fq "echo 'queries: 1000 (500.00 per sec.)'" --diag-time 1 --verify-time 1)
RC=$?
assert_success $RC "FQ-02: rc=0"
assert_contains "$OUT" "Max Combined: 63" "FQ-02: get_max_combined 解析（-l 输出）"
assert_contains "$OUT" "Decision: ADD" "FQ-02: 首测饱和 → ADD"
assert_contains "$OUT" "Decision: CONVERGED" "FQ-02: 次测 QPS 持平 → 收敛"
assert_contains "$OUT" "最优队列数: Combined=31" "FQ-02: best_q=31（192/4 与 63/2 取小，等 QPS 保留首个）"
LAST_L=$(tail -1 "$WORK/ethtool.state")
assert_eq "$LAST_L" "-L fqut-no-such-iface combined 31" "FQ-02: Phase2 恢复到最优队列数（末次 -L=31）"
assert_not_contains "$OUT" "current affinity list" "FQ-02: taskset stdout 污染被拦截（PR#194 重定向修复锁定）"
RESTORE_SET=$(tail -2 "$WORK/taskset.state" | head -1)
assert_eq "$RESTORE_SET" "-pc 33-383 $APP_PID" "FQ-02: Phase2 恢复获胜轮应用绑核（app_start=2+31=33，锁 mxj1314 检视 #3）"
LAST_TASKSET=$(tail -1 "$WORK/taskset.state")
assert_eq "$LAST_TASKSET" "-pc $APP_PID" "FQ-02: 恢复后读回核验亲和性（读模式调用，锁 mxj1314 复核 #3）"

echo "FQ-03: 不饱和首发 → REDUCE 分支"
MOCK2="$WORK/bin2"; mkdir -p "$MOCK2"; cp "$MOCK"/* "$MOCK2"/
cat > "$MOCK2/mpstat" <<'EOF'
#!/bin/bash
echo "Linux mock (384 CPU)"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:01" "CPU" "%usr" "%nice" "%sys" "%iowait" "%irq" "%soft" "%steal" "%guest" "%gnice" "%idle"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:02" "5" "0.00" "0.00" "1.00" "0.00" "1.00" "2.00" "0.00" "0.00" "0.00" "95.00"
EOF
chmod +x "$MOCK2/mpstat"
export ETHTOOL_STATE="$WORK/ethtool3.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset3.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset3.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK2:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1000 (500.00 per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1)
assert_contains "$OUT" "Decision: REDUCE" "FQ-03: 首测不饱和 → REDUCE"

echo "FQ-05: 外部命令 stdout 泄漏进 run_diag → 整行契约拒收 → 停止寻优（锁 mxj1314 检视 #2）"
MOCK3="$WORK/bin3"; mkdir -p "$MOCK3"; cp "$MOCK"/* "$MOCK3"/
# systemctl stop 在真机会打印 Removed/提示行（此前 mock 静默掩盖）——泄漏进样本通道复现污染
printf '#!/bin/bash\necho "Removed /etc/systemd/system/multi-user.target.wants/irqbalance.service."\nexit 0\n' > "$MOCK3/systemctl"
chmod +x "$MOCK3/systemctl"
export ETHTOOL_STATE="$WORK/ethtool5.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset5.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset5.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK3:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1000 (500.00 per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_fail $RC "FQ-05: 污染样本 → 非零退出"
assert_contains "$OUT" "样本无效" "FQ-05: 明确报样本无效（整行契约校验命中）"
assert_not_contains "$OUT" "Phase 2" "FQ-05: 拒绝进入 Phase2（旧实现污染字段被当零值 → 伪收敛，回归锁定）"

echo "FQ-06: 伪数值样本（qps='.' 可过宽松正则 [\d.]+）→ 严格数值契约拒收（锁 mxj1314 复核 #2）"
export ETHTOOL_STATE="$WORK/ethtool6.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset6.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset6.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1 (. per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_fail $RC "FQ-06: 伪数值 qps='.' → 非零退出"
assert_contains "$OUT" "样本无效" "FQ-06: 严格数值契约命中（\d+(\.\d+)? 单小数点约束）"
assert_not_contains "$OUT" "最优队列数: Combined=" "FQ-06: 不进入最终压测汇总（旧 [\d.]+ 会放行 '.' 并作为 qps 参与计算）"

echo "FQ-07: 应用绑核恢复静默失效（set 不落掩码）→ 读回核验拦截 → 拒绝最终压测（锁 mxj1314 复核 #3）"
MOCK4="$WORK/bin4"; mkdir -p "$MOCK4"; cp "$MOCK"/* "$MOCK4"/
# 故障注入：taskset set 模式正常返回但从不更新掩码——复现"恢复失败被 || true 吞掉"的路径
cat > "$MOCK4/taskset" <<'EOF'
#!/bin/bash
echo "$*" >> "$TASKSET_STATE"
cur="0-383"
[ -s "$TASKSET_CUR" ] && cur=$(cat "$TASKSET_CUR")
if [ "$#" -eq 2 ]; then
  echo "pid $2's current affinity list: $cur"
else
  echo "pid $3's current affinity list: $cur"
  echo "pid $3's new affinity list: $2"
fi
exit 0
EOF
chmod +x "$MOCK4/taskset"
export ETHTOOL_STATE="$WORK/ethtool7.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset7.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset7.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK4:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1000 (500.00 per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_fail $RC "FQ-07: 存活 PID 绑核未恢复 → 非零退出"
assert_contains "$OUT" "应用绑核核验失败" "FQ-07: 读回核验命中失败（亲和性仍为旧范围）"
assert_not_contains "$OUT" "最优队列数: Combined=" "FQ-07: 拒绝在旧 CPU 范围上出最终数据"

echo "FQ-08: 存活 PID 读回失败（读模式 rc=1，复现判活后 ESRCH 竞态）→ 停止复验，不得在未确认时压测（锁 mxj1314 16:07 复核 + huangsibeier 检视）"
MOCK5="$WORK/bin5"; mkdir -p "$MOCK5"; cp "$MOCK"/* "$MOCK5"/
# 故障注入：set 正常落掩码（恢复本身成功），读模式 rc=1 无输出——真实 taskset 对已退出 PID 的
# 失败签名（"成功但无输出"在现实中不存在；无 || true 时此路径会触发 set -e 静默中止）
cat > "$MOCK5/taskset" <<'EOF'
#!/bin/bash
echo "$*" >> "$TASKSET_STATE"
if [ "$#" -eq 2 ]; then
  exit 1
fi
cur="0-383"
[ -s "$TASKSET_CUR" ] && cur=$(cat "$TASKSET_CUR")
echo "pid $3's current affinity list: $cur"
echo "$2" > "$TASKSET_CUR"
echo "pid $3's new affinity list: $2"
exit 0
EOF
chmod +x "$MOCK5/taskset"
export ETHTOOL_STATE="$WORK/ethtool8.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset8.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset8.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK5:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1000 (500.00 per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_fail $RC "FQ-08: 存活 PID 亲和性不可读 → 非零退出"
assert_contains "$OUT" "亲和性未确认" "FQ-08: 区分'已退出跳过'与'存活读取失败停止'"
assert_contains "$OUT" "绑核核验失败" "FQ-08: 走到 ERROR 门控而非 set -e 静默中止（锁 huangsibeier 检视）"
assert_not_contains "$OUT" "最优队列数: Combined=" "FQ-08: 亲和性未经确认不得压测"

echo "FQ-09: 非英文 locale 读回输出（掩码仍在行尾）→ 行尾提取通过核验（锁 mxj1314 16:07 复核）"
MOCK6="$WORK/bin6"; mkdir -p "$MOCK6"; cp "$MOCK"/* "$MOCK6"/
# 读模式输出中文标签：复现非英文 locale 下 "affinity list" 文案不匹配的失效路径
cat > "$MOCK6/taskset" <<'EOF'
#!/bin/bash
echo "$*" >> "$TASKSET_STATE"
cur="0-383"
[ -s "$TASKSET_CUR" ] && cur=$(cat "$TASKSET_CUR")
if [ "$#" -eq 2 ]; then
  echo "pid $2 当前亲和性列表: $cur"
else
  echo "pid $3's current affinity list: $cur"
  echo "$2" > "$TASKSET_CUR"
  echo "pid $3's new affinity list: $2"
fi
exit 0
EOF
chmod +x "$MOCK6/taskset"
export ETHTOOL_STATE="$WORK/ethtool9.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset9.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset9.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK6:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "echo 'queries: 1000 (500.00 per sec.)'" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_success $RC "FQ-09: 非英文文案不影响核验（行尾掩码提取）"
assert_contains "$OUT" "最优队列数: Combined=31" "FQ-09: 核验通过 → 正常完成最终压测"

echo "FQ-10: 方向翻转复访已测队列数 → state_history 判重守卫触发（锁 huangsibeier 检视——旧数组实现此守卫恒死）"
# 确定性构造：递增 QPS（避免 CONVERGED/peak_between）+ 交替饱和度 mpstat → ADD/REDUCE 来回翻转：
# 31(ADD)→47(REDUCE)→26(ADD)→44(REDUCE)→24(ADD)→43(REDUCE)→NEXT=24 已测 → 守卫触发
MOCK7="$WORK/bin7"; mkdir -p "$MOCK7"; cp "$MOCK"/* "$MOCK7"/
export MPSTAT_CALLS="$WORK/mpstat_calls"; rm -f "$MPSTAT_CALLS"
cat > "$MOCK7/mpstat" <<'EOF'
#!/bin/bash
# 有状态 mock：奇数轮饱和（sat 命中 → ADD），偶数轮不饱和（→ REDUCE），驱动方向翻转
n=$(cat "$MPSTAT_CALLS" 2>/dev/null || echo 0)
n=$((n+1)); echo $n > "$MPSTAT_CALLS"
echo "Linux mock (384 CPU)"
printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:01" "CPU" "%usr" "%nice" "%sys" "%iowait" "%irq" "%soft" "%steal" "%guest" "%gnice" "%idle"
if [ $((n % 2)) -eq 1 ]; then
  printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:02" "5" "0.00" "0.00" "1.00" "0.00" "5.00" "40.00" "0.00" "0.00" "0.00" "0.50"
  printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:03" "5" "0.00" "0.00" "1.00" "0.00" "5.00" "40.00" "0.00" "0.00" "0.00" "0.50"
else
  printf '%-10s %-4s %s %s %s %s %s %s %s %s %s %s\n' "10:00:02" "5" "0.00" "0.00" "1.00" "0.00" "1.00" "2.00" "0.00" "0.00" "0.00" "95.00"
fi
exit 0
EOF
chmod +x "$MOCK7/mpstat"
cat > "$WORK/fq10_bench.sh" <<EOF
#!/bin/bash
# 递增 QPS：500.00, 550.00, 600.00 ...（每轮 >1% 增幅，避免收敛）
n=\$(cat "$WORK/fq10_bench.cnt" 2>/dev/null || echo 0)
n=\$((n+1)); echo \$n > "$WORK/fq10_bench.cnt"
echo "queries: 1000 (\$((450 + 50 * n)).00 per sec.)"
EOF
chmod +x "$WORK/fq10_bench.sh"; rm -f "$WORK/fq10_bench.cnt"
export ETHTOOL_STATE="$WORK/ethtool10.state"; : > "$ETHTOOL_STATE"; rm -f "${ETHTOOL_STATE}.cur"
export TASKSET_STATE="$WORK/taskset10.state"; : > "$TASKSET_STATE"
export TASKSET_CUR="$WORK/taskset10.cur"; rm -f "$TASKSET_CUR"
OUT=$(PATH="$MOCK7:$PATH" bash "$SCRIPT_DIR/find_optimal_queues.sh" \
      --iface "$IFACE" --irq-cpu-base 2 --bench-cmd "bash $WORK/fq10_bench.sh" \
      --diag-time 1 --verify-time 1 2>&1); RC=$?
assert_success $RC "FQ-10: 守卫触发后正常收尾（rc=0）"
assert_contains "$OUT" "已测试过 24, 停止" "FQ-10: state_history 判重命中（方向翻转复访被拦截——旧数组实现恒不触发）"
assert_contains "$OUT" "最优队列数: Combined=43" "FQ-10: best=末轮最优 43（守卫停止而非崩溃）"

skip "FQ-04: RSS 塌缩（ratio>50 → STOP:rss_collapse）——依赖 /proc/interrupts 前后差分，脚本读绝对路径不可 mock；留真机 A/B（journal r4 已验证）"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

mkdir -p "$CASE_DIR/results"
python3 - "$CASE_DIR/results/result.json" "$TEST_PASSED" "$TEST_FAILED" "$TEST_SKIPPED" <<'RESULTPY'
import json, sys
p, passed, failed, skipped = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
json.dump({"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
           "checks_failures": failed, "skipped": skipped},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sys.exit(0 if failed == 0 else 1)
RESULTPY
exit $?
