#!/usr/bin/env bash
# kptools-torchtitan-deploy 回归测试 — 文档结构断言 + 关键事实完整性
# 本 skill 无自带可执行脚本（指导型），测试以部署事实的准确性为主
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SK="$SKILL_DIR/SKILL.md"
INS="$SKILL_DIR/references/install-guide.md"
LCH="$SKILL_DIR/references/launch-reference.md"
PIT="$SKILL_DIR/references/pitfalls.md"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_in() { # name needle file
  # -- 终结符：防止以 - 开头的 needle 被 grep 当作选项（如 --module）
  if grep -qF -- "$2" "$3"; then ok "$1"; else bad "$1 (missing [$2] in $(basename $3))"; fi
}

# ============================================================ T1 frontmatter
for f in "name: kptools-torchtitan-deploy" "license: MulanPSL-2.0" "compatibility:" \
         "skill_type: debugging" "category: deployment" "domain: torchtitan_npu" \
         "version:"; do
  check_in "T1 frontmatter [$f]" "$f" "$SK"
done

# ============================================================ T2 章节
for s in "版本配套表" "部署工作流" "启动速查" "常见错误路由" \
         "和同类 skill 的分界" "错误处理" "参考资料"; do
  check_in "T2 章节 [$s]" "$s" "$SK"
done

# ============================================================ T3 版本表关键数字（两行配套全存在）
miss=0
for v in "2.14.0(daily)" "9.2.0" "3.12.x" "2.10.0" "9.0.0" "3.11.x" "3.2.1"; do
  grep -qF "$v" "$SK" || { bad "T3 版本表缺 [$v]"; miss=1; }
done
[ "$miss" -eq 0 ] && ok "T3 版本配套表两行齐全（master + v0.2.2-dev）"

# ============================================================ T4 启动环境变量（launch-reference）
miss=0
for v in "TORCHINDUCTOR_NPU_BACKEND" "PYTORCH_NPU_ALLOC_CONF" \
         "CPU_AFFINITY_CONF" "TASK_QUEUE_ENABLE" "HCCL_CONNECT_TIMEOUT" \
         "STREAMS_PER_DEVICE" "MULTI_STREAM_MEMORY_RESERVE" "LOG_RANK"; do
  grep -qF "$v" "$LCH" || { bad "T4 环境变量缺 [$v]"; miss=1; }
done
[ "$miss" -eq 0 ] && ok "T4 run_train.sh 八个环境变量逐项存在"
for v in "NODE_IPS" "HCCL_SOCKET_IFNAME" "HCCL_IF_BASE_PORT" "GLOO_SOCKET_IFNAME" \
         "HCCL_EXEC_TIMEOUT" "docker0"; do
  check_in "T4b 多机要素 [$v]" "$v" "$LCH"
done

# ============================================================ T5 配置系统重构事实
check_in "T5a 新配置系统 --module" "--module" "$LCH"
check_in "T5b 新配置系统 --config" "--config" "$LCH"
check_in "T5c 旧 TOML 弃用标注" "--job.config_file" "$SK"
check_in "T5d 扩展字段挂载" "extension.<group>.*" "$LCH"

# ============================================================ T6 安装关键事实
check_in "T6a NNAL source 顺序警示" "source /usr/local/Ascend/cann/set_env.sh" "$INS"
check_in "T6b 三步安装" "pip install -e ." "$INS"
check_in "T6c torchao-npu" "experiments/torchao-npu" "$INS"
check_in "T6d 冒烟默认资产覆盖变量" "HF_ASSETS_PATH" "$INS"
check_in "T6e 低精度仅 A5/950 门槛" "Ascend 950" "$SK"

# ============================================================ T7 pitfalls 分组与条目
na=$(grep -c '^### A-' "$PIT")
nb=$(grep -c '^### B-' "$PIT")
if [ "$na" -ge 10 ] && [ "$nb" -ge 8 ]; then
  ok "T7a pitfalls 分组条目 A=$na B=$nb（≥10+8）"
else
  bad "T7a pitfalls 条目不足 A=$na B=$nb"
fi
check_in "T7b A 组定义存在" "源码补丁即证据" "$PIT"
check_in "T7c B 组定义存在" "实战环境组" "$PIT"

# ============================================================ T8 关键坑事实（可溯源）
for k in "DT_INT64" "gather_lowering_dtypes" "HCCL_DETERMINISTIC" \
         "CLOSE_MATMUL_K_SHIFT" "sync-start" "policy_overrides"; do
  check_in "T8 坑事实 [$k]" "$k" "$PIT"
done
check_in "T8b B-16 确诊方法" "require_sync_start" "$PIT"

# ============================================================ T9 分界路由
for r in "kptools-cannlab-env" "train-metrics" "tool-perf-collect"; do
  check_in "T9 路由 [$r]" "$r" "$SK"
done

# ============================================================ T10 敏感信息自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SK" "$INS" "$LCH" "$PIT" 2>/dev/null | sort -u)
if [ -z "$leak" ]; then
  ok "T10 无 32+ 位 hex 泄露"
else
  bad "T10 疑似泄露: $leak"
fi

# ============================================================ T11 启动脚本事实
check_in "T11a set_env 探测链变量" "ASCEND_SET_ENV_PATH" "$LCH"
check_in "T11b 多机脚本名" "run_train_multinodes.sh" "$SK"
check_in "T11c 单进程调试开关" "COMM_MODE" "$LCH"
check_in "T11d 8 卡示例脚本" "deepseek_v4_flash_8p_cpt_4k_a3.sh" "$SK"
check_in "T11e TorchFT store prefix" "store prefix" "$LCH"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
