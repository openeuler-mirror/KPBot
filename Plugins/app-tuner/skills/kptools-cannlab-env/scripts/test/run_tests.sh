#!/usr/bin/env bash
# kptools-cannlab-env 回归测试 — 文档结构断言 + 敏感信息自检 + 可选真环境 smoke
# 本 skill 无自带脚本（复用环境工具），测试以 SKILL.md/references 的完整性为主
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SK="$SKILL_DIR/SKILL.md"
OPS="$SKILL_DIR/references/cannlab-ops-guide.md"
PIT="$SKILL_DIR/references/pitfalls.md"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); echo "PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $*"; }
check_in() { # name needle file
  if grep -qF "$2" "$3"; then ok "$1"; else bad "$1 (missing [$2])"; fi
}

# ============================================================ T1 frontmatter 完整
for f in "name: kptools-cannlab-env" "license: MulanPSL-2.0" "compatibility:" \
         "skill_type: tool_assisted" "version:"; do
  check_in "T1 frontmatter 含 [$f]" "$f" "$SK"
done

# ============================================================ T2 核心章节
for s in "环境拓扑" "工作流程" "退出码契约" "关键机制与纪律" \
         "NPU 环境配方" "和同类 skill 的分界" "错误处理" "参考资料"; do
  check_in "T2 章节存在 [$s]" "$s" "$SK"
done

# ============================================================ T3 退出码表完整（0,2,3,4,5,6,7,8,10）
miss=0
for c in "| 0 |" "| 2 |" "| 3 |" "| 4 |" "| 5 |" "| 6 |" "| 7 |" "| 8 |" "| 10 |"; do
  grep -qF "$c" "$SK" || { bad "T3 退出码表缺 [$c]"; miss=1; }
done
[ "$miss" -eq 0 ] && ok "T3 退出码表 9 个码位齐全"

# ============================================================ T4 remote_tool.sh 子命令齐全
miss=0
for sub in "exec" "pull" "sync" "check" "config" "shutdown"; do
  grep -qF "\`$sub" "$SK" || { bad "T4 子命令缺 [$sub]"; miss=1; }
done
[ "$miss" -eq 0 ] && ok "T4 remote_tool.sh 六个子命令齐全"

# ============================================================ T5 纪律三件套
check_in "T5a 单引号规则" "双引号会被吞掉" "$SK"
check_in "T5b rsync --delete 破坏性声明" "rsync --delete" "$SK"
check_in "T5c pyfix312 禁令" "绝不要把 CPU 层预置的 pyfix312" "$SK"
check_in "T5d 禁止直连 NPU IP" "禁止直连 NPU IP" "$SK"
check_in "T5e CPU 唯一可信存储" "唯一可信存储" "$SK"

# ============================================================ T5f 引号规则自洽（检视意见 #1 修复）
check_in "T5f-1 外层一律单引号" "一律用单引号" "$SK"
check_in "T5f-2 外层禁止双引号" "外层禁止用双引号" "$SK"
check_in "T5f-3 PowerShell 双写转义" "''" "$SK"
if grep -qF "'''\\''" "$SK" 2>/dev/null || grep -qE "'\\\\''" "$SK"; then
  bad "T5f-4 POSIX 转义仍残留"
else
  ok "T5f-4 POSIX '\\'' 转义已移除"
fi
check_in "T5f-5 ops-guide 同口径" "一律单引号" "$OPS"

# ============================================================ T5g 路由真实存在（检视意见 #2 修复）
for r in "kptools-ascend-profiling-analysis" "kptools-ascend-smi" "kptools-os-collector"; do
  check_in "T5g 路由存在 [$r]" "$r" "$SK"
done
for ghost in "tool-perf-collect" "tool-perf-analysis" "train-metrics"; do
  if grep -qF "$ghost" "$SK" "$(dirname "$SK")/evals/eval_queries.json" 2>/dev/null; then
    bad "T5g 幽灵 skill 仍被引用: $ghost"
  else
    ok "T5g 幽灵 skill 已清除 [$ghost]"
  fi
done

# ============================================================ T6 敏感信息自检（token/长 hex 不得出现在文档）
leak=$(grep -hoE '[a-f0-9]{32,}' "$SK" "$OPS" "$PIT" 2>/dev/null | sort -u)
if [ -z "$leak" ]; then
  ok "T6 无 32+ 位 hex 泄露（id/token 用占位符表示）"
else
  bad "T6 疑似泄露标识/凭据: $leak"
fi

# ============================================================ T6b 机器特定路径清理（检视意见 #4）
for p in ".gitcode/private_token" "usr/local/bin/devenv_api" "opt/buildtools" \
         "~/pyfix312" "\$HOME/pylib"; do
  if grep -qF "$p" "$SK" "$OPS" "$PIT" 2>/dev/null; then
    bad "T6b 机器特定路径残留 [$p]"
  else
    ok "T6b 路径已清理 [$p]"
  fi
done

# ============================================================ T7 pitfalls 分组与条目数
n=$(grep -c '^### ' "$PIT")
if [ "$n" -ge 14 ]; then
  ok "T7a pitfalls 条目数 $n >= 14"
else
  bad "T7a pitfalls 条目不足: $n"
fi
for g in "一、引号与命令传递" "二、文件与脚本" "三、Python 环境" \
         "四、同步与文件生命周期" "五、NPU Pod 系统层"; do
  check_in "T7b 分组存在 [$g]" "$g" "$PIT"
done

# ============================================================ T8 ops-guide 关键节
for s in "文件生命周期" "长任务模式" "超时与重试" "收尾与报告"; do
  check_in "T8 ops-guide 章节 [$s]" "$s" "$OPS"
done

# ============================================================ T9 示例引号模式（防回归）
if grep -qF -- "--command '" "$SK" && grep -qF -- "--pull-dir '" "$OPS"; then
  ok "T9 示例使用单引号包裹命令值（防引号吞坑回归）"
else
  bad "T9 示例引号模式缺失"
fi

# ============================================================ T10 可选真环境 smoke
# 本机 ssh 配置（~/.ssh/config 或其 Include 的 ~/.atomgitdevenv/.ssh/config）
# 存在 devenvc 别名时才试连（不触发 NPU 开机，纯 CPU 容器连通性）
alias=""
for cfg in "$HOME/.ssh/config" "$HOME/.atomgitdevenv/.ssh/config"; do
  if [ -r "$cfg" ]; then
    alias=$(grep -iE '^Host ' "$cfg" | awk '{print $2}' \
            | grep -E '^devenvc' | head -1)
    [ -n "$alias" ] && break
  fi
done
if [ -z "$alias" ]; then
  skip "T10 本机无 devenvc 别名（非 cannlab 配置机，跳过真环境连通）"
elif ! command -v ssh >/dev/null 2>&1; then
  skip "T10 无 ssh 客户端"
elif timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 "$alias" \
       'echo cannlab_smoke_ok' >"$WORK/smoke.out" 2>"$WORK/smoke.err"; then
  grep -q cannlab_smoke_ok "$WORK/smoke.out" \
    && ok "T10 CPU 容器连通 smoke（alias=$alias）" \
    || bad "T10 连通但回显异常"
else
  rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "T10 ssh 超时（隧道/容器暂不可达，环境性问题而非 skill 问题）"
  else
    skip "T10 环境暂不可达（rc=$rc；smoke 为 best-effort 验证，不阻塞）"
  fi
fi

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed, $SKIP skipped ====="
[ "$FAIL" -eq 0 ]
