#!/bin/bash
# ============================================================================
# set_config_param.sh — 3FS 运行时配置单项热更新（段落感知，防同名误伤）
#
# 用法: set_config_param.sh <NODE_TYPE> <toml_section> <key> <value>
#   NODE_TYPE   : STORAGE | META | FUSE | MGMTD | CLIENT
#   toml_section: 不带方括号的段名, 如 server.targets.storage_target.kv_store
#   key         : 段内配置项名, 如 sync_when_write
#   value       : TOML 字面值, 如 false / 6 / 256 / "'CRC32C'"（字符串必须自带引号）
#
# 流程: get-config 导出运行时配置 → 备份 → awk 状态机段内定位 → python3 段落感知
#       替换(匹配数 != 1 即报错) → set-config 上传(带 --desc) → 回读校验 → 打印
#       ConfigVersion 变化
#
# env: ADMIN_CLI(默认 /opt/3fs/bin/admin_cli)
#      ADMIN_CLI_CONFIG(默认 /opt/3fs/etc/admin_cli.toml)
#      BACKUP_DIR(默认 /tmp)
#
# 示例:
#   ./set_config_param.sh STORAGE server.targets.storage_target.kv_store sync_when_write false
#   ./set_config_param.sh FUSE storage_io.write enableChecksum false
#   ./set_config_param.sh FUSE storage.traffic_control.write max_concurrent_requests 256
#
# 前置条件: 在部署了 admin_cli 的节点执行, 且 mgmtd_main 已启动、RDMA/IB 链路正常
#           (mgmtd 未运行或 IB 口 Down 时 admin_cli 会直接异常退出)。
#           脚本内不含任何密码/Token；如需 token 由 admin_cli 配置文件提供。
# ============================================================================

set -euo pipefail

ADMIN_CLI="${ADMIN_CLI:-/opt/3fs/bin/admin_cli}"
ADMIN_CLI_CONFIG="${ADMIN_CLI_CONFIG:-/opt/3fs/etc/admin_cli.toml}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

die() { echo "错误: $*" >&2; exit 1; }

usage() {
    cat <<EOF
用法: $0 <NODE_TYPE> <toml_section> <key> <value>

  NODE_TYPE   : STORAGE | META | FUSE | MGMTD | CLIENT
  toml_section: 不带方括号的段名, 如 server.targets.storage_target.kv_store
  key         : 段内配置项名, 如 sync_when_write
  value       : TOML 字面值, 如 false / 6 / 256 / "'CRC32C'"（字符串自带引号）

env: ADMIN_CLI=${ADMIN_CLI}
     ADMIN_CLI_CONFIG=${ADMIN_CLI_CONFIG}
     BACKUP_DIR=${BACKUP_DIR}

示例:
  $0 STORAGE server.targets.storage_target.kv_store sync_when_write false
  $0 FUSE storage_io.write enableChecksum false
  $0 FUSE storage.traffic_control.write max_concurrent_requests_per_server 64
EOF
}

case "${1:-}" in -h|--help|help) usage; exit 0 ;; esac
[ "$#" -eq 4 ] || { usage; exit 1; }

NODE_TYPE="$1"; SECTION="$2"; KEY="$3"; TARGET="$4"

case "$NODE_TYPE" in
    STORAGE|META|FUSE|MGMTD|CLIENT) ;;
    *) die "NODE_TYPE 非法: ${NODE_TYPE} (可选 STORAGE|META|FUSE|MGMTD|CLIENT)" ;;
esac
[[ "$SECTION" =~ ^[A-Za-z0-9_.]+$ ]] || die "段名非法: ${SECTION} (不要带方括号/空格)"
[[ "$KEY" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "配置项名非法: ${KEY}"
[ -n "$TARGET" ] || die "value 不能为空"
if [[ "$TARGET" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] && [[ "$TARGET" != "true" && "$TARGET" != "false" ]]; then
    echo "警告: value '${TARGET}' 看起来是裸字符串, TOML 需要引号, 如 \"'${TARGET}'\"" >&2
fi

[ -x "$ADMIN_CLI" ] || die "未找到 admin_cli: ${ADMIN_CLI} (请在部署了 admin_cli 的节点执行, 或用 ADMIN_CLI= 指定)"
[ -f "$ADMIN_CLI_CONFIG" ] || die "未找到 admin_cli 配置文件: ${ADMIN_CLI_CONFIG} (用 ADMIN_CLI_CONFIG= 指定)"

WORK="$(mktemp "/tmp/${NODE_TYPE}_config_current_XXXXXX.toml")"
VERIFY="$(mktemp "/tmp/${NODE_TYPE}_config_verify_XXXXXX.toml")"
trap 'rm -f "$WORK" "$VERIFY"' EXIT

# 段内定位: awk 状态机 —— 只在 [SECTION] 段内匹配 KEY, 遇到下一个段头即退出该段,
# 避免同名配置项(如 enableChecksum 同时在 [storage_io.read] 与 [storage_io.write])被误伤
get_val() {
    awk -v SEC="$SECTION" -v KEY="$KEY" '
        /^[[:space:]]*\[/ {
            hdr = $0
            gsub(/^[[:space:]]*\[+/, "", hdr)
            gsub(/\]+[[:space:]]*$/, "", hdr)
            insec = (hdr == SEC)
            next
        }
        insec && match($0, /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/) {
            k = substr($0, RSTART, RLENGTH)
            gsub(/[[:space:]=]/, "", k)
            if (k == KEY) {
                line = $0
                sub(/^[^=]*=[[:space:]]*/, "", line)
                sub(/[[:space:]]*$/, "", line)
                print line
                exit
            }
        }
    ' "$1"
}

get_config_version() {  # 从 admin_cli 输出表格里取 ConfigVersion
    grep -E '^ConfigVersion' "$1" 2>/dev/null | awk '{print $NF}' | head -1
}

echo "########## 第 1 步: get-config 导出 ${NODE_TYPE} 运行时配置 ##########"
"$ADMIN_CLI" -cfg "$ADMIN_CLI_CONFIG" "get-config --node-type ${NODE_TYPE} --output-file ${WORK}" > "${WORK}.meta" 2>&1 \
    || { cat "${WORK}.meta" >&2; rm -f "${WORK}.meta"; die "get-config 失败: 请确认 mgmtd_main 已启动且 RDMA/IB 链路正常"; }
BEFORE_VER="$(get_config_version "${WORK}.meta")"
rm -f "${WORK}.meta"

OLD="$(get_val "$WORK")"
echo ""
echo "========== 修改前 (mgmtd 当前配置, ConfigVersion=${BEFORE_VER:-未知}) =========="
echo "  [${SECTION}] ${KEY} = ${OLD:-<未找到>}"
echo ""
[ -n "$OLD" ] || die "当前配置中未找到 [${SECTION}] ${KEY}, 中止 (段名/配置项名是否与 get-config 导出一致?)"

if [ "$OLD" = "$TARGET" ]; then
    echo "当前值已经是 ${TARGET}, 无需修改 (幂等退出)"
    exit 0
fi

mkdir -p "$BACKUP_DIR" 2>/dev/null || true
BACKUP="${BACKUP_DIR}/${NODE_TYPE}_config_v${BEFORE_VER:-NA}_$(date '+%Y%m%d_%H%M%S').toml"
cp "$WORK" "$BACKUP"
echo "########## 第 2 步: 备份当前配置 ##########"
echo "  备份文件: ${BACKUP}"

echo "########## 第 3 步: 段落感知替换 (仅限 [${SECTION}] 段) ##########"
python3 - "$WORK" "$SECTION" "$KEY" "$TARGET" <<'PYEOF'
import re, sys
path, sec, key, target = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path) as f:
    lines = f.readlines()
insec, changed = False, 0
for i, ln in enumerate(lines):
    if re.match(r'^\s*\[', ln):
        hdr = re.sub(r'^\s*\[+', '', ln).rstrip()
        hdr = re.sub(r'\]+\s*$', '', hdr)
        insec = (hdr == sec)
        continue
    if insec and re.match(r'^\s*' + re.escape(key) + r'\s*=', ln):
        lines[i] = re.sub(r'^(\s*' + re.escape(key) + r'\s*=).*$',
                          lambda m: m.group(1) + ' ' + target, ln)
        changed += 1
if changed != 1:
    print("错误: 在 [%s] 段匹配到 %d 处 %s (期望 1 处), 文件未修改" % (sec, changed, key))
    sys.exit(1)
with open(path, 'w') as f:
    f.writelines(lines)
print("已修改: [%s] %s -> %s" % (sec, key, target))
PYEOF

NEW="$(get_val "$WORK")"
echo ""
echo "========== 修改后 (本地待上传文件) =========="
echo "  [${SECTION}] ${KEY} = ${NEW:-<未找到>}"
echo ""
[ "$NEW" = "$TARGET" ] || die "本地替换结果与目标值不一致 (${NEW} != ${TARGET}), 未上传"

echo "########## 第 4 步: set-config 上传到 mgmtd ##########"
"$ADMIN_CLI" -cfg "$ADMIN_CLI_CONFIG" \
    "set-config --type ${NODE_TYPE} --file ${WORK} --desc \"set ${SECTION}.${KEY} to ${TARGET}\"" > "${WORK}.set" 2>&1 \
    || { cat "${WORK}.set" >&2; rm -f "${WORK}.set"; die "set-config 失败 (回滚: 用备份 ${BACKUP} 重新 set-config)"; }
cat "${WORK}.set"
NEW_VER="$(get_config_version "${WORK}.set")"
rm -f "${WORK}.set"

echo ""
echo "########## 第 5 步: 回读校验 ##########"
sleep 1
"$ADMIN_CLI" -cfg "$ADMIN_CLI_CONFIG" "get-config --node-type ${NODE_TYPE} --output-file ${VERIFY}" > "${VERIFY}.meta" 2>&1 \
    || { cat "${VERIFY}.meta" >&2; rm -f "${VERIFY}.meta"; die "校验用 get-config 失败"; }
FINAL_VER="$(get_config_version "${VERIFY}.meta")"
rm -f "${VERIFY}.meta"
FINAL="$(get_val "$VERIFY")"
echo ""
echo "========== 修改后 (mgmtd 生效配置) =========="
echo "  [${SECTION}] ${KEY} = ${FINAL:-<未找到>}"
echo "  ConfigVersion: ${BEFORE_VER:-未知} -> ${FINAL_VER:-${NEW_VER:-未知}}"
echo ""
[ "$FINAL" = "$TARGET" ] || die "mgmtd 生效值 ${FINAL:-<未找到>} 与目标值 ${TARGET} 不一致, 请检查!"

echo "完成: [${SECTION}] ${KEY}: ${OLD} -> ${TARGET} (ConfigVersion ${BEFORE_VER:-?} -> ${FINAL_VER:-${NEW_VER:-?}})"
echo "回滚: $ADMIN_CLI -cfg $ADMIN_CLI_CONFIG \"set-config --type ${NODE_TYPE} --file ${BACKUP} --desc 'rollback ${KEY}'\""
echo "提示: 1) 热更新能力因组件而异 —— FUSE(hf3fs_fuse_main) 声明配置不可热更新, 必须"
echo "         systemctl restart hf3fs_fuse_main; storage 的部分项(如 sync_when_write)实测也需重启。"
echo "         详见 references/config-mechanics.md 的热更新能力表。"
echo "      2) 各节点 /opt/3fs/etc/ 下的同名 toml 需同步为相同内容, 否则重启后会用旧文件覆盖预期配置。"
echo "      3) 重启顺序: 先全部 storage 节点, 再所有 FUSE 客户端; FUSE 重启前若挂载点残留需先 umount。"
