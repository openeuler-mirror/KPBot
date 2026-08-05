#!/usr/bin/env bash
set -euo pipefail

# apply_bios_change.sh
# BIOS 变更操作手册生成器 + Redfish PATCH 执行器
#
# 两种模式:
#   --generate      生成操作手册 + Redfish PATCH JSON + 验证脚本 + 变更前快照
#   --execute-patch 执行 Redfish PATCH（standalone 模式,用户确认后调用）
#
# 不直接修改 BIOS（subagent 模式）。standalone 模式经用户逐项确认后可执行 Redfish PATCH。
# BMC 凭据从环境变量 REDFISH_BMC_HOST/USER/PASS 读取,不传入命令行参数。

usage() {
  cat <<'EOF'
Usage:
  apply_bios_change.sh --generate --output-dir <dir> --candidate-actions <file> [options]
  apply_bios_change.sh --execute-patch --action-id <id> --candidate-actions <file> [options]
  apply_bios_change.sh --execute-patch --action-ids <id1,id2,...> --candidate-actions <file> [options]
  apply_bios_change.sh --rollback --action-id <id> --candidate-actions <file> [options]

Modes:
  --generate      Generate bios_change_plan.md, bios_redfish_patch.json,
                  post_reboot_verify.sh, pre_change_snapshot.json
  --execute-patch Execute Redfish PATCH for action(s) (standalone mode)
  --rollback      Generate rollback PATCH body from pre_change_snapshot.json

Options:
  --output-dir <dir>              Output directory for generated files
  --candidate-actions <file>      JSON file containing candidate_actions array
  --approved-action-ids <id,...>  Only generate/execute for approved action IDs
  --action-id <id>                Single action ID to execute (--execute-patch)
  --action-ids <id1,id2,...>      Multiple action IDs to execute (--execute-patch)
  --existing-backup-dir <dir>     Existing backup dir (for pre_change_snapshot)
  --dry-run                       Show PATCH body and target without executing (--execute-patch)
  --force                         Allow execution of high-risk actions (--execute-patch)
  --insecure                      Skip SSL certificate verification (--execute-patch)
  -h, --help                      Show help

Environment Variables (for --execute-patch):
  REDFISH_BMC_HOST    BMC host (required)
  REDFISH_BMC_USER    BMC username (required)
  REDFISH_BMC_PASS    BMC password (required)

Examples:
  # Generate all output files
  apply_bios_change.sh --generate \
    --output-dir ./bios-output \
    --candidate-actions ./candidate_actions.json

  # Generate only for approved actions
  apply_bios_change.sh --generate \
    --output-dir ./bios-output \
    --candidate-actions ./candidate_actions.json \
    --approved-action-ids bios-power-profile-001,bios-cstate-002

  # Execute Redfish PATCH for one action
  apply_bios_change.sh --execute-patch \
    --action-id bios-power-profile-001 \
    --candidate-actions ./candidate_actions.json
EOF
}

fail() { echo "ERROR: $*" >&2; exit 1; }

MODE=""
OUTPUT_DIR=""
CANDIDATE_ACTIONS_FILE=""
APPROVED_ACTION_IDS=""
ACTION_ID=""
ACTION_IDS=""
EXISTING_BACKUP_DIR=""
DRY_RUN=false
FORCE=false
INSECURE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --generate) MODE="generate"; shift ;;
    --execute-patch) MODE="execute_patch"; shift ;;
    --rollback) MODE="rollback"; shift ;;
    --output-dir) OUTPUT_DIR="${2:?}"; shift 2 ;;
    --candidate-actions) CANDIDATE_ACTIONS_FILE="${2:?}"; shift 2 ;;
    --approved-action-ids) APPROVED_ACTION_IDS="${2:?}"; shift 2 ;;
    --action-id) ACTION_ID="${2:?}"; shift 2 ;;
    --action-ids) ACTION_IDS="${2:?}"; shift 2 ;;
    --existing-backup-dir) EXISTING_BACKUP_DIR="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    --insecure) INSECURE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ -n "${MODE}" ]] || { usage >&2; fail "mode is required"; }
[[ -n "${CANDIDATE_ACTIONS_FILE}" ]] || fail "--candidate-actions is required"
[[ -f "${CANDIDATE_ACTIONS_FILE}" ]] || fail "candidate actions file not found: ${CANDIDATE_ACTIONS_FILE}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ── Generate mode ─────────────────────────────────────────────

run_generate() {
  [[ -n "${OUTPUT_DIR}" ]] || fail "--output-dir is required for --generate"
  mkdir -p "${OUTPUT_DIR}"

  local SCRIPT_DIR
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  log "=== Generate mode: creating operation manual + PATCH JSON + validation script ==="

  # Delegate to Python for JSON parsing and file generation
  OUTPUT_DIR="${OUTPUT_DIR}" \
  CANDIDATE_ACTIONS_FILE="${CANDIDATE_ACTIONS_FILE}" \
  APPROVED_ACTION_IDS="${APPROVED_ACTION_IDS}" \
  EXISTING_BACKUP_DIR="${EXISTING_BACKUP_DIR}" \
  APPLY_SCRIPT_DIR="${SCRIPT_DIR}" \
  python3 <<'PYEOF'
import json, os, sys, datetime

output_dir = os.environ["OUTPUT_DIR"]
ca_file = os.environ["CANDIDATE_ACTIONS_FILE"]
approved_ids_str = os.environ.get("APPROVED_ACTION_IDS", "")
existing_backup_dir = os.environ.get("EXISTING_BACKUP_DIR", "")

# Load candidate actions
with open(ca_file) as f:
    ca_data = json.load(f)

# Handle both {"candidate_actions": [...]} and [...] formats
if isinstance(ca_data, dict):
    all_actions = ca_data.get("candidate_actions", [])
else:
    all_actions = ca_data

# Filter to approved actions if specified
if approved_ids_str:
    approved_set = set(approved_ids_str.split(","))
    actions = [a for a in all_actions if a.get("action_id") in approved_set]
else:
    actions = all_actions

# Separate by risk level (critical actions don't get Redfish PATCH)
non_critical = [a for a in actions if a.get("risk") != "critical"]
critical = [a for a in actions if a.get("risk") == "critical"]

# ── Generate pre_change_snapshot.json ──────────────────────

snapshot = {
    "snapshot_time": datetime.datetime.now().isoformat(),
    "platform": {},
    "bios_settings": {},
    "rollback_target": "恢复到以上配置"
}

# Try to read platform info from existing backup
if existing_backup_dir and os.path.isdir(existing_backup_dir):
    bios_info_path = os.path.join(existing_backup_dir, "bios-info.txt")
    if os.path.isfile(bios_info_path):
        with open(bios_info_path) as f:
            bios_info_text = f.read()
        # Parse basic info
        for line in bios_info_text.split("\n"):
            if "BIOS Version:" in line:
                snapshot["platform"]["bios_version"] = line.split(":", 1)[1].strip()
            if "Manufacturer:" in line:
                snapshot["platform"]["bios_vendor"] = line.split(":", 1)[1].strip()
            if "Product Name:" in line:
                snapshot["platform"]["server_model"] = line.split(":", 1)[1].strip()

    # Read Redfish normalized data if available
    redfish_path = os.path.join(existing_backup_dir, "bios-redfish-normalized.json")
    if not os.path.isfile(redfish_path):
        redfish_path = os.path.join(output_dir, "bios-redfish-normalized.json")
    if os.path.isfile(redfish_path):
        with open(redfish_path) as f:
            redfish_data = json.load(f)
        for key, val in redfish_data.items():
            snapshot["bios_settings"][key] = {
                "value": val.get("value"),
                "source": val.get("source", "unknown"),
                "matched_property": val.get("matched_property_name", "")
            }

snapshot_path = os.path.join(output_dir, "pre_change_snapshot.json")
with open(snapshot_path, "w") as f:
    json.dump(snapshot, f, indent=2, ensure_ascii=False)
print(f"  ✅ pre_change_snapshot.json")

# ── Generate bios_change_plan.md ───────────────────────────

plan_lines = [
    "# BIOS 变更操作手册",
    "",
    f"> 生成时间: {datetime.datetime.now().isoformat()}",
    "",
    "## 变更前准备",
    "- [ ] 确认业务可中断窗口",
    "- [ ] 确认远程管理通道（iBMC/IPMI/Redfish）可用",
    "- [ ] 确认回退方案（已保存 pre_change_snapshot.json）",
    "- [ ] 确认 critical 操作的硬件恢复方案（如需）",
    "",
    "## 变更项",
    "",
]

for i, action in enumerate(actions, 1):
    title = action.get("title", action.get("action_id", "unknown"))
    risk = action.get("risk", "unknown")
    change_mode = action.get("change_mode", "system_reboot")
    impl = action.get("implementation_plan", "")
    rollback = action.get("rollback", "")
    evidence_refs = action.get("evidence_refs", [])

    plan_lines.append(f"### {i}. {title}")
    plan_lines.append(f"- 风险: {risk}")
    plan_lines.append(f"- 生效方式: {change_mode}")

    if "bios_password_required" in action and action["bios_password_required"]:
        plan_lines.append("- ⚠️ 需要 BIOS 密码,Redfish PATCH 不可用")
    if "locked_by_vendor" in action and action["locked_by_vendor"]:
        plan_lines.append("- ⚠️ 该参数被厂商锁定,不可修改")

    plan_lines.append(f"- 操作方式: {impl}")
    plan_lines.append(f"- 回退: {rollback}")

    # Cross-skill impact
    impact_notes = []
    if action.get("dependent_action_ids"):
        impact_notes.append(f"联动影响: {', '.join(action['dependent_action_ids'])}")
    if "requires_cpu_affinity_rerun" in str(action):
        impact_notes.append("此变更后需重跑 cpu-affinity-optimization")
    if "requires_os_optimization_rerun" in str(action):
        impact_notes.append("此变更后需重跑 os-optimization")
    if impact_notes:
        plan_lines.append(f"- {'; '.join(impact_notes)}")

    plan_lines.append("")

# Critical operations section
if critical:
    plan_lines.append("## critical 操作（可能无法启动）")
    plan_lines.append("")
    for action in critical:
        title = action.get("title", action.get("action_id", "unknown"))
        rollback = action.get("rollback", "")
        plan_lines.append(f"### {title}")
        plan_lines.append(f"- 风险: Critical（可能无法启动）")
        plan_lines.append(f"- 操作方式: {action.get('implementation_plan', '')}")
        plan_lines.append(f"- 无法启动时的恢复:")
        plan_lines.append("  1. CMOS 清除: 主板 Clear CMOS 跳线 → 恢复出厂默认")
        plan_lines.append("  2. iBMC 远程恢复: 登录 iBMC Web → BIOS 恢复")
        plan_lines.append("  3. 备用 BIOS 芯片: 切换跳线到 Backup BIOS")
        plan_lines.append("")

# Post-reboot validation section
plan_lines.append("## 重启后验证")
plan_lines.append("重启后登录服务器,执行:")
plan_lines.append("  bash ./post_reboot_verify.sh pre_change_snapshot.json")
plan_lines.append("将输出结果回传给主SKLL 或自行与 pre_change_snapshot.json 比对。")
plan_lines.append("")

plan_path = os.path.join(output_dir, "bios_change_plan.md")
with open(plan_path, "w") as f:
    f.write("\n".join(plan_lines))
print(f"  ✅ bios_change_plan.md ({len(actions)} actions)")

# ── Generate bios_redfish_patch.json ───────────────────────

patch_body = {}
for action in non_critical:
    # Parse implementation_plan for Redfish PATCH body
    impl = action.get("implementation_plan", "")
    # Look for JSON-like patch body in implementation_plan
    if "Redfish PATCH:" in impl:
        patch_part = impl.split("Redfish PATCH:")[1].strip()
        try:
            patch_dict = json.loads(patch_part)
            patch_body.update(patch_dict)
        except json.JSONDecodeError:
            # Not JSON, skip
            pass

# Check requires_apply_step (from candidate action metadata or default false)
requires_apply = any(a.get("requires_apply_step", False) for a in non_critical)

patch_json = {
    "target": "https://{bmc_host}{system_endpoint}/Bios/Settings",
    "method": "PATCH",
    "headers": {"Content-Type": "application/json"},
    "body": patch_body,
    "requires_apply_step": requires_apply,
    "bmc_credentials_source": "environment_variable REDFISH_BMC_PASS",
    "note": "执行前需确认 BMC 凭据和 system_endpoint;凭据从环境变量读取,不传入命令行参数"
}

patch_path = os.path.join(output_dir, "bios_redfish_patch.json")
with open(patch_path, "w") as f:
    json.dump(patch_json, f, indent=2, ensure_ascii=False)
print(f"  ✅ bios_redfish_patch.json ({len(patch_body)} properties)")

# ── Copy post_reboot_verify.sh to output_dir ───────────────

import shutil
script_dir = os.environ.get("APPLY_SCRIPT_DIR", "")
verify_src = os.path.join(script_dir, "post_reboot_verify.sh")
verify_dst = os.path.join(output_dir, "post_reboot_verify.sh")

if script_dir and os.path.isfile(verify_src):
    shutil.copy2(verify_src, verify_dst)
    os.chmod(verify_dst, 0o755)
    print(f"  ✅ post_reboot_verify.sh")
else:
    print(f"  ⚠️ post_reboot_verify.sh not found (APPLY_SCRIPT_DIR={script_dir}), skipped")

# Summary
print(f"\nGenerated files in {output_dir}:")
print(f"  - pre_change_snapshot.json (变更前配置快照)")
print(f"  - bios_change_plan.md (操作手册, {len(actions)} actions)")
print(f"  - bios_redfish_patch.json ({len(patch_body)} properties, {len(critical)} critical excluded)")
print(f"  - post_reboot_verify.sh (重启后验证脚本)")

if critical:
    print(f"\n⚠️  {len(critical)} critical action(s) excluded from Redfish PATCH:")
    for c in critical:
        print(f"  - {c.get('title', c.get('action_id'))}")
PYEOF

  log "Generate mode complete."
}

# ── Execute-patch mode ────────────────────────────────────────

run_execute_patch() {
  # Support both --action-id (single) and --action-ids (batch)
  if [[ -n "${ACTION_IDS}" ]]; then
    IFS=',' read -ra ID_LIST <<< "${ACTION_IDS}"
  elif [[ -n "${ACTION_ID}" ]]; then
    ID_LIST=("${ACTION_ID}")
  else
    fail "--action-id or --action-ids is required for --execute-patch"
  fi

  # Check BMC credentials
  [[ -n "${REDFISH_BMC_HOST:-}" ]] || fail "REDFISH_BMC_HOST environment variable is required"
  [[ -n "${REDFISH_BMC_USER:-}" ]] || fail "REDFISH_BMC_USER environment variable is required"
  [[ -n "${REDFISH_BMC_PASS:-}" ]] || fail "REDFISH_BMC_PASS environment variable is required"

  has_cmd curl || fail "curl is required for Redfish PATCH"
  has_cmd python3 || fail "python3 is required"

  log "=== Execute-patch mode: ${#ID_LIST[@]} action(s) ==="

  # SSL option (BMC usually has self-signed cert, default -k)
  local SSL_OPT="-k"

  # Parse all actions: collect PATCH body + check risk levels
  ALL_PARSED=$(python3 - "${CANDIDATE_ACTIONS_FILE}" "${ID_LIST[@]}" <<'PYEOF'
import json, sys

ca_file = sys.argv[1]
action_ids = sys.argv[2:]

with open(ca_file) as f:
    ca_data = json.load(f)

if isinstance(ca_data, dict):
    all_actions = ca_data.get("candidate_actions", [])
else:
    all_actions = ca_data

results = []
combined_body = {}
max_risk = "low"
risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

for aid in action_ids:
    action = None
    for a in all_actions:
        if a.get("action_id") == aid:
            action = a
            break
    if not action:
        print(f"ERROR: action_id '{aid}' not found", file=sys.stderr)
        sys.exit(1)

    risk = action.get("risk", "unknown")
    if risk == "critical":
        print(f"ERROR: action '{aid}' is critical risk, cannot execute via Redfish PATCH", file=sys.stderr)
        sys.exit(1)

    impl = action.get("implementation_plan", "")
    patch_body = {}
    if "Redfish PATCH:" in impl:
        patch_part = impl.split("Redfish PATCH:")[1].strip()
        try:
            patch_body = json.loads(patch_part)
        except json.JSONDecodeError:
            pass

    if not patch_body:
        print(f"ERROR: no Redfish PATCH body in action '{aid}'", file=sys.stderr)
        sys.exit(1)

    combined_body.update(patch_body)
    results.append({"action_id": aid, "risk": risk, "body": patch_body})
    if risk_order.get(risk, 0) > risk_order.get(max_risk, 0):
        max_risk = risk

# Output: max_risk|combined_body_json|details_json
print(f"{max_risk}|{json.dumps(combined_body)}|{json.dumps(results)}")
PYEOF
  ) || fail "failed to parse actions"

  ACTION_RISK="${ALL_PARSED%%|*}"
  REMAINDER="${ALL_PARSED#*|}"
  PATCH_BODY="${REMAINDER%%|*}"

  # Dry-run: show what would be executed, then exit
  if [[ "${DRY_RUN}" == true ]]; then
    log "=== DRY RUN (不实际执行) ==="
    echo ""
    echo "Action IDs: ${ID_LIST[*]}"
    echo "Max Risk: ${ACTION_RISK}"
    echo "Combined PATCH body: ${PATCH_BODY}"
    echo "Target: https://${REDFISH_BMC_HOST}/redfish/v1/Systems/<auto>/Bios/Settings"
    echo ""
    if [[ "${ACTION_RISK}" == "high" ]]; then
      echo "⚠️  含高风险操作! 执行时需加 --force 参数"
    fi
    local force_opt=""
    [[ "${ACTION_RISK}" == "high" ]] && force_opt="--force"
    if [[ ${#ID_LIST[@]} -gt 1 ]]; then
      echo "确认执行请运行:"
      echo "  apply_bios_change.sh --execute-patch --action-ids $(IFS=,; echo "${ID_LIST[*]}") ${force_opt}"
    else
      echo "确认执行请运行:"
      echo "  apply_bios_change.sh --execute-patch --action-id ${ID_LIST[0]} ${force_opt}"
    fi
    return
  fi

  # Risk check: high requires --force
  if [[ "${ACTION_RISK}" == "high" && "${FORCE}" != true ]]; then
    fail "含高风险操作 (risk=high),需加 --force 参数确认后执行"
  fi

  log "Max Risk: ${ACTION_RISK}, Force: ${FORCE}"
  log "Combined PATCH body: ${PATCH_BODY}"

  # Discover system endpoint
  log "Discovering Redfish system endpoint..."
  SYSTEMS_JSON=$(curl -s ${SSL_OPT} -u "${REDFISH_BMC_USER}:${REDFISH_BMC_PASS}" \
    "https://${REDFISH_BMC_HOST}/redfish/v1/Systems" 2>/dev/null || echo "")

  SYSTEM_ENDPOINT=$(echo "${SYSTEMS_JSON}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    members = data.get('Members', [])
    if members:
        ref = members[0].get('@odata.id', '')
        print(ref)
except Exception:
    pass
" 2>/dev/null)

  [[ -n "${SYSTEM_ENDPOINT}" ]] || fail "failed to discover Redfish system endpoint"

  log "System endpoint: ${SYSTEM_ENDPOINT}"
  log "Sending PATCH to ${SYSTEM_ENDPOINT}/Bios/Settings..."

  # Execute PATCH (single request with combined body)
  HTTP_RESPONSE=$(curl -s ${SSL_OPT} -o /tmp/opencode/patch_response.json -w "%{http_code}" \
    -X PATCH \
    -u "${REDFISH_BMC_USER}:${REDFISH_BMC_PASS}" \
    -H "Content-Type: application/json" \
    -d "${PATCH_BODY}" \
    "https://${REDFISH_BMC_HOST}${SYSTEM_ENDPOINT}/Bios/Settings" 2>/dev/null || echo "000")

  log "HTTP response: ${HTTP_RESPONSE}"

  # Check response
  case "${HTTP_RESPONSE}" in
    200|202)
      log "✅ PATCH successful (HTTP ${HTTP_RESPONSE})"

      # Output result for all actions
      python3 - "${ID_LIST[@]}" "${HTTP_RESPONSE}" <<'PYEOF'
import json, sys, datetime
action_ids = sys.argv[1:-1]
http_status = int(sys.argv[-1])
for aid in action_ids:
    result = {
        "action_id": aid,
        "method": "redfish_patch",
        "result": "success" if 200 <= http_status < 300 else "failed",
        "http_status": http_status,
        "timestamp": datetime.datetime.now().isoformat()
    }
    print(json.dumps(result, indent=2))
PYEOF
      ;;
    4*)
      log "❌ Client error (HTTP ${HTTP_RESPONSE})"
      cat /tmp/opencode/patch_response.json 2>/dev/null || true
      fail "Redfish PATCH failed: HTTP ${HTTP_RESPONSE} (client error, not retrying)"
      ;;
    5*)
      log "⚠️ Server error (HTTP ${HTTP_RESPONSE}), retrying once..."
      sleep 2
      HTTP_RESPONSE=$(curl -s ${SSL_OPT} -o /tmp/opencode/patch_response.json -w "%{http_code}" \
        -X PATCH \
        -u "${REDFISH_BMC_USER}:${REDFISH_BMC_PASS}" \
        -H "Content-Type: application/json" \
        -d "${PATCH_BODY}" \
        "https://${REDFISH_BMC_HOST}${SYSTEM_ENDPOINT}/Bios/Settings" 2>/dev/null || echo "000")
      if [[ "${HTTP_RESPONSE}" == 200 || "${HTTP_RESPONSE}" == 202 ]]; then
        log "✅ Retry successful (HTTP ${HTTP_RESPONSE})"
      else
        log "❌ Retry failed (HTTP ${HTTP_RESPONSE})"
        fail "Redfish PATCH failed after retry: HTTP ${HTTP_RESPONSE}"
      fi
      ;;
    *)
      fail "Redfish PATCH failed: HTTP ${HTTP_RESPONSE}"
      ;;
  esac

  log "Execute-patch mode complete."
}

# ── Rollback mode ─────────────────────────────────────────────

run_rollback() {
  [[ -n "${ACTION_ID}" ]] || fail "--action-id is required for --rollback"
  [[ -n "${EXISTING_BACKUP_DIR}" ]] || fail "--existing-backup-dir is required for --rollback (to read pre_change_snapshot.json)"

  local SNAPSHOT_FILE="${EXISTING_BACKUP_DIR}/pre_change_snapshot.json"
  [[ -f "${SNAPSHOT_FILE}" ]] || fail "pre_change_snapshot.json not found at ${SNAPSHOT_FILE}"

  log "=== Rollback mode: action_id=${ACTION_ID} ==="

  # Generate rollback PATCH body from pre_change_snapshot.json
  python3 - "${SNAPSHOT_FILE}" "${ACTION_ID}" "${CANDIDATE_ACTIONS_FILE}" <<'PYEOF'
import json, sys

snapshot_file = sys.argv[1]
action_id = sys.argv[2]
ca_file = sys.argv[3]

# Load snapshot
with open(snapshot_file) as f:
    snapshot = json.load(f)

bios_settings = snapshot.get("bios_settings", {})

# Load candidate action to get category (maps to snapshot key)
with open(ca_file) as f:
    ca_data = json.load(f)

actions = ca_data.get("candidate_actions", ca_data) if isinstance(ca_data, dict) else ca_data
action = None
for a in actions:
    if a.get("action_id") == action_id:
        action = a
        break

if not action:
    print(f"ERROR: action_id '{action_id}' not found", file=sys.stderr)
    sys.exit(1)

category = action.get("category", "")
# Map category to snapshot key
key_map = {
    "power_profile": "power_profile",
    "smt": "smt",
    "numa": "numa_interleaving",
    "cstate": "cstate",
    "turbo": "turbo",
    "ddr_speed": "ddr_speed",
    "prefetcher": "hardware_prefetcher",
    "pcie_aspm": "pcie_aspm",
}

snapshot_key = key_map.get(category, category)
setting = bios_settings.get(snapshot_key, {})

if not setting:
    print(f"ERROR: no snapshot data for '{snapshot_key}'", file=sys.stderr)
    sys.exit(1)

old_value = setting.get("value")
if old_value is None:
    print(f"ERROR: snapshot value is None for '{snapshot_key}'", file=sys.stderr)
    sys.exit(1)

# Generate rollback PATCH body
# Try to find the Redfish property name from snapshot
matched_property = setting.get("matched_property", "")
if matched_property:
    rollback_body = {matched_property: old_value}
else:
    # Use category as fallback key
    rollback_body = {snapshot_key: old_value}

result = {
    "action_id": action_id,
    "mode": "rollback",
    "snapshot_key": snapshot_key,
    "old_value": old_value,
    "rollback_patch_body": rollback_body,
    "target": "https://{bmc_host}{system_endpoint}/Bios/Settings",
    "note": "使用此 PATCH body 恢复变更前的配置"
}
print(json.dumps(result, indent=2, ensure_ascii=False))
PYEOF
}

# ── Helper ─────────────────────────────────────────────────────

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ── Main ──────────────────────────────────────────────────────

log "apply_bios_change.sh starting (mode=${MODE})"

case "${MODE}" in
  generate)      run_generate ;;
  execute_patch) run_execute_patch ;;
  rollback)      run_rollback ;;
  *) fail "unknown mode: ${MODE}" ;;
esac

log "Done."
