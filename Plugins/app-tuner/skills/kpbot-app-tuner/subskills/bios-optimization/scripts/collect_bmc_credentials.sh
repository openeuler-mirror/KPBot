#!/usr/bin/env bash
set -euo pipefail

# collect_bmc_credentials.sh
# 交互式采集 BMC 凭据并执行 Redfish 采集
# 密码通过 read -s 隐藏输入,不落盘、不进 shell 历史、不进 LLM 对话
#
# 用法: bash collect_bmc_credentials.sh --output-dir <dir>
# 用户在终端执行此脚本,输入 BMC IP/用户名/密码,脚本采集 Redfish JSON
#
# 注意: 此脚本可能被拷贝到 /tmp 执行,backup_environment.sh 路径通过以下顺序查找:
#   1. 环境变量 BACKUP_SCRIPT_PATH（Agent 拷贝脚本时可显式设置,最可靠）
#   2. /tmp/.kpbot_backup_script_path 文件（Agent 拷贝脚本时预写,解决跨终端环境变量不传递问题）
#   3. 相对路径 ../../../scripts/backup_environment.sh（原始位置）
#   4. git 仓库根下的 Plugins/app-tuner/skills/kpbot-app-tuner/scripts/backup_environment.sh（动态查找）

OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:?}"; shift 2 ;;
    -h|--help)
      echo "Usage: collect_bmc_credentials.sh --output-dir <dir>"
      echo "交互式采集 BMC 凭据 + Redfish JSON,密码不回显、不落盘"
      exit 0 ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

[[ -n "${OUTPUT_DIR}" ]] || { echo "ERROR: --output-dir required" >&2; exit 1; }
mkdir -p "${OUTPUT_DIR}"

rm -f "${OUTPUT_DIR}"/.redfish_last_http_code \
      "${OUTPUT_DIR}"/bios-redfish-*.json \
      "${OUTPUT_DIR}"/bios-redfish-*.txt 2>/dev/null || true

# 查找 backup_environment.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 读取 Agent 预置的路径文件（拷贝脚本到 /tmp 时同步写入,解决跨终端环境变量不传递问题）
PRESET_PATH_FILE="/tmp/.kpbot_backup_script_path"
PRESET_PATH=""
[[ -f "${PRESET_PATH_FILE}" ]] && PRESET_PATH="$(cat "${PRESET_PATH_FILE}" 2>/dev/null | tr -d '[:space:]')"

# 动态获取 git 仓库根路径（脚本在 git 仓库内时有效,在 /tmp 等非 git 目录时为空）
GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"

BACKUP_SCRIPT=""
for candidate in \
  "${BACKUP_SCRIPT_PATH:-}" \
  "${PRESET_PATH}" \
  "${SCRIPT_DIR}/../../../scripts/backup_environment.sh" \
  "${GIT_ROOT}/Plugins/app-tuner/skills/kpbot-app-tuner/scripts/backup_environment.sh"; do
  if [[ -n "${candidate}" && -f "${candidate}" ]]; then
    BACKUP_SCRIPT="${candidate}"
    break
  fi
done

[[ -n "${BACKUP_SCRIPT}" ]] || { echo "ERROR: backup_environment.sh not found" >&2; exit 1; }

echo "=== BMC 凭据采集（密码不回显、不落盘）==="
echo ""

read -r -p "BMC IP 地址: " BMC_HOST
read -r -p "BMC 用户名: " BMC_USER
read -s -r -p "BMC 密码: " BMC_PASS
echo ""
echo ""

echo "正在采集 Redfish 数据..."
echo ""

export BMC_HOST BMC_USER BMC_PASS

bash "${BACKUP_SCRIPT}" --output-dir "${OUTPUT_DIR}" --non-interactive 2>&1

unset BMC_HOST BMC_USER BMC_PASS

echo ""
echo "=== 采集完成 ==="
echo "Redfish JSON 已保存到: ${OUTPUT_DIR}"
echo "BMC 密码已从内存清除"
echo ""
echo "请回到 SKILL 对话,回复 'done' 继续。"
