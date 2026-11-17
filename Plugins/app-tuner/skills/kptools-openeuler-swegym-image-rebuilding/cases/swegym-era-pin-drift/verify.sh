#!/usr/bin/env bash
# verify.sh —— swegym-era-pin-drift 的产物判定（全部 fail-closed）
#
# 判定 agent 是否真正完成"依赖漂移定位 + 时代锚定修复 + 验收翻案"：
#   1) tamper_ok：受保护文件（官方测试 / run_gold.sh / build_env.sh /
#      instance.json）sha256 与镜像构建时固化的清单一致——改判据=作弊
#   2) pin_ok：env/requirements.txt 含精确时代 pin（pyarrow==13.0.0）。
#      只认配置文件本身——直接 pip install 到 venv 绕过配置的修法不算数
#   3) gold_ok：verify 亲自重跑 run_gold.sh 并读取其结果。
#      **不接受 agent 自述 gold_final 作为判据**（防"自述即通过"）
#   4) report_ok：opt/build_report.json 必填字段非空，且 base_commit
#      必须与镜像固化的真实锚点一致（防编造）、era_pin 必须等于实际生效 pin
#
# 输出：results/result.json（success / skill_mode / tamper_ok / pin_ok /
#       gold_ok / report_ok / era_pin_found / elapsed_seconds / reason）
# 退出码：0 表示全部成立，非 0 表示未成立。
# 说明：WORKSPACE_DIR 默认 /workspace（容器契约不变）。
set -uo pipefail

cd "${WORKSPACE_DIR:-/workspace}"
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}"
export SKILL_MODE

python3 - <<'PY'
import json, os, subprocess, sys

skill_mode = os.environ.get("SKILL_MODE", "with")

elapsed = None
try:
    for line in open("results/elapsed.txt"):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "build_elapsed":
            elapsed = int(parts[1])
except Exception:
    pass

def write(reason, success, tamper_ok, pin_ok, gold_ok, report_ok, era_pin_found, extra=None):
    d = {"success": success, "reason": reason, "skill_mode": skill_mode,
         "tamper_ok": tamper_ok, "pin_ok": pin_ok, "gold_ok": gold_ok,
         "report_ok": report_ok, "era_pin_found": era_pin_found,
         "elapsed_seconds": elapsed}
    if extra:
        d.update(extra)
    json.dump(d, open("results/result.json", "w"), ensure_ascii=False, indent=2)
    print("verify:", "PASS" if success else "FAIL", "reason=" + reason)
    raise SystemExit(0 if success else 1)

# ---- 1) tamper_ok：受保护文件未被改动 ----
tamper = 0
r = subprocess.run(["sha256sum", "-c", ".fixture.sha256"],
                   capture_output=True, text=True)
if r.returncode == 0 and "OK" in r.stdout:
    tamper = 1
else:
    write("protected files modified (tamper)", False, 0, 0, 0, 0, "")

# ---- 2) pin_ok：env/requirements.txt 含精确时代 pin ----
ERA_PIN = "pyarrow==13.0.0"
pin_ok = 0
era_pin_found = ""
try:
    for line in open("env/requirements.txt"):
        s = line.strip()
        if s == ERA_PIN:
            pin_ok = 1
            era_pin_found = s
except Exception:
    pass
if not pin_ok:
    write("exact era pin missing in env/requirements.txt", False, tamper, 0, 0, 0, era_pin_found)

# ---- 3) gold_ok：亲自重跑验收（不接受自述） ----
gold = 0
g = subprocess.run(["bash", "run_gold.sh"], capture_output=True, text=True)
try:
    gold = int(open("results/gold.json").read().strip())
except Exception:
    gold = 0
if gold != 1:
    write("official gold still 0 on verify re-run", False, tamper, pin_ok, 0, 0, era_pin_found)

# ---- 4) report_ok：报告完整且锚点真实 ----
report_ok = 0
try:
    report = json.load(open("opt/build_report.json"))
except Exception:
    report = {}
true_base = open("base_commit.txt").read().strip() if os.path.exists("base_commit.txt") else ""
fields = [report.get("drift_source"), report.get("fix_description"),
          report.get("era_pin"), report.get("base_commit")]
base_match = 1 if true_base and report.get("base_commit") == true_base else 0
era_match = 1 if report.get("era_pin") == ERA_PIN else 0
if all(isinstance(x, str) and x.strip() for x in fields) and base_match and era_match:
    report_ok = 1
if not report_ok:
    write("build_report incomplete or base_commit/era_pin not truthful",
          False, tamper, pin_ok, gold, 0, era_pin_found)

write("era pin restored and gold flipped to 1",
      True, tamper, pin_ok, gold, report_ok, era_pin_found)
PY
