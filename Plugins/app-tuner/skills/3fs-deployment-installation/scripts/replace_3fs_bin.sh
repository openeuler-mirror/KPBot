#!/usr/bin/env bash
#===============================================================
# replace_3fs_bin.sh — 3FS 集群二进制滚动替换
#
# 用法:   replace_3fs_bin.sh <二进制目录>
#         (如 ${BUILD_DIR}/bin，须含 admin_cli / mgmtd_main / meta_main /
#          storage_main / hf3fs_fuse_main)
# 执行位置: meta 节点（mgmtd/meta 所在节点，本机 hostname 须与 META_HOST 一致）
#
# 流程:
#   0. 预检查（参数目录、必需二进制、到各节点的 SSH）
#   1. 停止集群: 所有 FUSE（含挂载点释放）-> storage -> meta -> mgmtd
#   2. 各节点 ${INSTALL_PREFIX}/bin 备份为 bin.bak.<时间戳>，
#      覆盖复制新二进制并逐文件 md5 校验
#   3. 启动集群: mgmtd -> meta -> storage -> 所有 FUSE（每步之间等 2 秒）
#   4. 验证: 服务状态 + admin_cli list-nodes（期望节点数按清单动态计算）
#      + 各 FUSE 节点 ${MOUNT_POINT} 挂载
#
# 回滚: 用任一节点 ${INSTALL_PREFIX}/bin.bak.<时间戳> 覆盖回
#       ${INSTALL_PREFIX}/bin 后按同样顺序重启（详见
#       references/binary-replacement.md）
#
# 节点清单与路径全部参数化（环境变量可覆盖，或用 inventory 文件）:
#   INVENTORY_FILE   inventory 文件，每行 "<角色> <主机名>"，
#                    角色 ∈ {meta,storage,fuse}，# 开头为注释；
#                    指定后覆盖 META_HOST/STORAGE_HOSTS/FUSE_HOSTS
#   META_HOST        meta 节点主机名（默认: meta）
#   STORAGE_HOSTS    storage 节点列表，空格分隔（默认: "storage1 storage2"）
#   FUSE_HOSTS       FUSE 客户端列表，空格分隔（默认: "$META_HOST"）
#   SSH_USER         节点间 SSH 用户（默认: root）
#   INSTALL_PREFIX   线上安装前缀（默认: /opt/3fs）
#   MOUNT_POINT      FUSE 挂载点（默认: /3fs/stage）
#   SSH_OPTS         ssh/scp 选项（默认: -o StrictHostKeyChecking=no -o ConnectTimeout=5）
#
# 凭据（本脚本绝不包含明文密码，也不提供命令行传密码的途径）:
#   优先: SSH key 免密（ssh-copy-id ${SSH_USER}@<节点>）
#   密码认证: 仅接受 SSHPASS 环境变量并走 sshpass -e，建议交互注入:
#     read -s -p 'SSH password: ' SSHPASS; export SSHPASS
#
# 说明: 本脚本只替换 ${INSTALL_PREFIX}/bin 下的二进制；如需同时替换
#       /usr/lib(64) 下库文件或 monitor_collector_main，请走
#       references/binary-replacement.md 的手工 SOP。
#===============================================================
set -u

usage() {
    cat <<'EOF'
用法: replace_3fs_bin.sh <二进制目录>

在 meta 节点执行，将 <二进制目录> 中的 3FS 二进制滚动替换到全集群：
预检查 -> 顺序停服 -> 备份 -> 覆盖 + md5 校验 -> 顺序启动 -> 验证。

节点清单（inventory 文件优先，其次环境变量，均有默认值）:
  INVENTORY_FILE=<file>        每行 "<角色> <主机名>"，角色 ∈ {meta,storage,fuse}
  META_HOST=<host>             默认: meta
  STORAGE_HOSTS="<h1> <h2>"    默认: storage1 storage2
  FUSE_HOSTS="<h1> <h2>"       默认: $META_HOST

其他环境变量:
  SSH_USER（默认 root）、INSTALL_PREFIX（默认 /opt/3fs）、
  MOUNT_POINT（默认 /3fs/stage）、SSH_OPTS

凭据: SSH key 免密优先；密码认证仅支持 SSHPASS 环境变量（sshpass -e）:
  read -s -p 'SSH password: ' SSHPASS; export SSHPASS

示例:
  replace_3fs_bin.sh /opt/src/3FS/3fs/build/bin
  STORAGE_HOSTS="storage1 storage2 storage3" replace_3fs_bin.sh ./bin
  INVENTORY_FILE=./inventory.txt replace_3fs_bin.sh ./bin
EOF
}

# ---------- 日志 ----------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_RST=""
fi
log()  { echo "[$(date '+%F %T')] $*"; }
ok()   { log "${C_GRN}[ OK ]${C_RST} $*"; }
warn() { log "${C_YEL}[WARN]${C_RST} $*"; }
die()  { log "${C_RED}[FAIL]${C_RST} $*" >&2; exit 1; }

# ---------- 参数 ----------
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac
[ $# -eq 1 ] || { usage >&2; die "参数个数不符（需要且仅需要 <二进制目录>）"; }

# ---------- 节点清单 ----------
META_HOST="${META_HOST:-meta}"
STORAGE_HOSTS="${STORAGE_HOSTS:-storage1 storage2}"
FUSE_HOSTS="${FUSE_HOSTS:-$META_HOST}"
SSH_USER="${SSH_USER:-root}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/3fs}"
MOUNT_POINT="${MOUNT_POINT:-/3fs/stage}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o ConnectTimeout=5}"

if [ -n "${INVENTORY_FILE:-}" ]; then
    [ -f "${INVENTORY_FILE}" ] || die "inventory 文件不存在: ${INVENTORY_FILE}"
    META_HOST=""; STORAGE_HOSTS=""; FUSE_HOSTS=""
    while read -r role host _rest; do
        [ -z "${role:-}" ] && continue
        case "$role" in \#*) continue ;; esac
        [ -n "${host:-}" ] || die "inventory 行缺少主机名: $role"
        case "$role" in
            meta)    [ -z "$META_HOST" ] || die "inventory 中 meta 角色重复: $host"; META_HOST="$host" ;;
            storage) STORAGE_HOSTS="$STORAGE_HOSTS $host" ;;
            fuse)    FUSE_HOSTS="$FUSE_HOSTS $host" ;;
            *)       die "inventory 中未知角色 '$role'（应为 meta/storage/fuse）" ;;
        esac
    done < "$INVENTORY_FILE"
    [ -n "$META_HOST" ] || die "inventory 中缺少 meta 角色行"
    STORAGE_HOSTS="${STORAGE_HOSTS# }"
    FUSE_HOSTS="${FUSE_HOSTS# }"; [ -n "$FUSE_HOSTS" ] || FUSE_HOSTS="$META_HOST"
fi

read -r -a STORAGE_ARR <<< "$STORAGE_HOSTS"
read -r -a FUSE_ARR <<< "$FUSE_HOSTS"
for h in "${STORAGE_ARR[@]}"; do [ -n "$h" ] || die "storage 节点列表含空项: '$STORAGE_HOSTS'"; done
for h in "${FUSE_ARR[@]}"; do [ -n "$h" ] || die "FUSE 节点列表含空项: '$FUSE_HOSTS'"; done
[ "${#STORAGE_ARR[@]}" -ge 1 ] || die "storage 节点列表为空（3FS 集群至少 1 台 storage）"

# 全部涉及节点（去重，meta 可能兼任 FUSE 客户端）
ALL_HOSTS=("$META_HOST")
add_host() {
    local x
    for x in "${ALL_HOSTS[@]}"; do [ "$x" = "$1" ] && return 0; done
    ALL_HOSTS+=("$1")
}
for h in "${STORAGE_ARR[@]}"; do add_host "$h"; done
for h in "${FUSE_ARR[@]}"; do add_host "$h"; done

TS=$(date +%Y%m%d_%H%M%S)
BACKUP_SUFFIX="bin.bak.$TS"

# ---------- 凭据（SSH key 或 SSHPASS + sshpass -e，绝无明文密码） ----------
if [ -n "${SSHPASS:-}" ]; then
    command -v sshpass >/dev/null 2>&1 || die "已设置 SSHPASS 但未安装 sshpass: yum install -y sshpass"
    SSH_CMD=(sshpass -e ssh $SSH_OPTS)
    SCP_CMD=(sshpass -e scp -q $SSH_OPTS)
    log "凭据方式: sshpass -e（SSHPASS 环境变量）"
else
    SSH_CMD=(ssh $SSH_OPTS)
    SCP_CMD=(scp -q $SSH_OPTS)
    log "凭据方式: SSH key（未设置 SSHPASS）"
fi

run_on() { # <host> <command...>
    local host=$1; shift
    if [ "$host" = "$META_HOST" ]; then
        bash -c "$*"
    else
        "${SSH_CMD[@]}" "${SSH_USER}@${host}" "$*"
    fi
}

copy_to() { # <host> <src> <dst>
    local host=$1 src=$2 dst=$3
    if [ "$host" = "$META_HOST" ]; then
        cp -f "$src" "$dst"
    else
        "${SCP_CMD[@]}" "$src" "${SSH_USER}@${host}:$dst"
    fi
}

show_status() {
    local h u st
    for h in "${FUSE_ARR[@]}"; do
        st=$(run_on "$h" "systemctl is-active hf3fs_fuse_main" 2>/dev/null | tr -d '[:space:]')
        printf '    %-12s %-22s %s\n' "$h" "hf3fs_fuse_main" "${st:-unknown}"
    done
    for h in "${STORAGE_ARR[@]}"; do
        st=$(run_on "$h" "systemctl is-active storage_main" 2>/dev/null | tr -d '[:space:]')
        printf '    %-12s %-22s %s\n' "$h" "storage_main" "${st:-unknown}"
    done
    for u in meta_main mgmtd_main; do
        st=$(run_on "$META_HOST" "systemctl is-active $u" 2>/dev/null | tr -d '[:space:]')
        printf '    %-12s %-22s %s\n' "$META_HOST" "$u" "${st:-unknown}"
    done
}

trap 'rc=$?; if [ "$rc" -ne 0 ]; then log "脚本异常退出 (rc=$rc), 当前各服务状态:"; show_status; fi' EXIT

service_stop() { # <host> <unit>
    local host=$1 unit=$2 st
    log "停止 $host:$unit ..."
    run_on "$host" "systemctl stop $unit" >/dev/null || {
        run_on "$host" "systemctl status $unit --no-pager" >&2 || true
        die "停止 $host:$unit 失败"
    }
    st=$(run_on "$host" "systemctl is-active $unit" 2>/dev/null | tr -d '[:space:]')
    [ "$st" = "inactive" ] || [ "$st" = "failed" ] || die "$host:$unit 未停止 (状态: $st)"
    ok "$host:$unit 已停止"
}

service_start() { # <host> <unit>
    local host=$1 unit=$2 st
    log "启动 $host:$unit ..."
    run_on "$host" "systemctl start $unit" >/dev/null || {
        run_on "$host" "journalctl -u $unit -n 30 --no-pager" >&2 || true
        die "启动 $host:$unit 失败"
    }
    st=$(run_on "$host" "systemctl is-active $unit" 2>/dev/null | tr -d '[:space:]')
    [ "$st" = "active" ] || die "$host:$unit 启动失败 (状态: $st)"
    ok "$host:$unit 已启动"
}

release_mount() { # <host> 释放该节点 FUSE 挂载
    local host=$1
    if run_on "$host" "mountpoint -q ${MOUNT_POINT}" 2>/dev/null; then
        log "$host:${MOUNT_POINT} 仍挂载, 卸载 ..."
        # 3FS FUSE 基于 libfuse3, 优先用 fusermount3
        run_on "$host" "timeout 15 fusermount3 -u ${MOUNT_POINT} 2>/dev/null || timeout 15 fusermount -u ${MOUNT_POINT} 2>/dev/null || umount -l ${MOUNT_POINT} 2>/dev/null || true" >/dev/null
        sleep 1
        run_on "$host" "mountpoint -q ${MOUNT_POINT}" 2>/dev/null && die "无法释放 $host:${MOUNT_POINT} 挂载"
        ok "$host:${MOUNT_POINT} 挂载已释放"
    fi
}

# ---------- 0. 预检查 ----------
BIN_DIR=$(readlink -f "$1" 2>/dev/null) || die "无法解析目录: $1"
[ -d "$BIN_DIR" ] || die "目录不存在: $1"

META_BINARIES="meta_main mgmtd_main hf3fs_fuse_main admin_cli"
STORAGE_BINARIES="storage_main admin_cli"
FUSE_BINARIES="hf3fs_fuse_main admin_cli"
for f in $META_BINARIES $STORAGE_BINARIES; do
    [ -x "$BIN_DIR/$f" ] || die "$BIN_DIR 中缺少可执行文件: $f"
done
log "二进制目录: $BIN_DIR"
log "版本: $(strings "$BIN_DIR/meta_main" 2>/dev/null | grep -m1 '^Version: v' || echo unknown)"
log "节点清单: meta=$META_HOST storage=[${STORAGE_HOSTS}] fuse=[${FUSE_HOSTS}]"
log "安装前缀: $INSTALL_PREFIX  挂载点: $MOUNT_POINT  SSH 用户: $SSH_USER"

{ [ "$(hostname)" = "$META_HOST" ] || [ "$(hostname -s)" = "$META_HOST" ]; } \
    || die "请在 meta 节点执行 (META_HOST=$META_HOST, 当前 hostname=$(hostname))"

for h in "${ALL_HOSTS[@]}"; do
    [ "$h" = "$META_HOST" ] && continue
    run_on "$h" "true" 2>/dev/null \
        || die "无法 SSH 到 $h (${SSH_USER}@$h)；请配置 SSH key，或 read -s 注入 SSHPASS 后重试（需安装 sshpass）"
done
ok "预检查通过（涉及节点: ${ALL_HOSTS[*]}）"

# ---------- 1. 停止集群: 所有 FUSE -> storage -> meta -> mgmtd ----------
log "===== 步骤1: 停止集群 ====="
for h in "${FUSE_ARR[@]}"; do
    service_stop "$h" hf3fs_fuse_main
    # 确保挂载点已释放 (FUSE 进程被杀后挂载可能残留)
    release_mount "$h"
done
for h in "${STORAGE_ARR[@]}"; do
    service_stop "$h" storage_main
done
service_stop "$META_HOST" meta_main
service_stop "$META_HOST" mgmtd_main
ok "集群已停止"

# ---------- 2. 备份并覆盖二进制 ----------
log "===== 步骤2: 备份并覆盖 ${INSTALL_PREFIX}/bin ====="
for h in "${ALL_HOSTS[@]}"; do
    run_on "$h" "cp -a ${INSTALL_PREFIX}/bin ${INSTALL_PREFIX}/${BACKUP_SUFFIX}" \
        || die "备份 $h:${INSTALL_PREFIX}/bin 失败"
done
ok "旧二进制已备份: 各节点 ${INSTALL_PREFIX}/${BACKUP_SUFFIX}"

# meta 节点: 本地复制
for f in $META_BINARIES; do
    copy_to "$META_HOST" "$BIN_DIR/$f" "${INSTALL_PREFIX}/bin/$f" || die "复制 $f 到 $META_HOST 失败"
done
ok "$META_HOST 已覆盖: $META_BINARIES"

# storage 节点
for h in "${STORAGE_ARR[@]}"; do
    for f in $STORAGE_BINARIES; do
        copy_to "$h" "$BIN_DIR/$f" "${INSTALL_PREFIX}/bin/$f" || die "scp $f -> $h 失败"
    done
    ok "$h 已覆盖: $STORAGE_BINARIES"
done

# 远程 FUSE 节点
for h in "${FUSE_ARR[@]}"; do
    [ "$h" = "$META_HOST" ] && continue
    for f in $FUSE_BINARIES; do
        copy_to "$h" "$BIN_DIR/$f" "${INSTALL_PREFIX}/bin/$f" || die "scp $f -> $h 失败"
    done
    ok "$h 已覆盖: $FUSE_BINARIES"
done

# md5 校验
for h in "${ALL_HOSTS[@]}"; do
    if [ "$h" = "$META_HOST" ]; then
        for f in $META_BINARIES; do
            a=$(md5sum "$BIN_DIR/$f" | awk '{print $1}')
            b=$(md5sum "${INSTALL_PREFIX}/bin/$f" | awk '{print $1}')
            [ "$a" = "$b" ] || die "md5 不一致: $h:$f"
        done
    elif [[ " ${STORAGE_HOSTS} " == *" $h "* ]]; then
        for f in $STORAGE_BINARIES; do
            a=$(md5sum "$BIN_DIR/$f" | awk '{print $1}')
            b=$(run_on "$h" "md5sum ${INSTALL_PREFIX}/bin/$f" | awk '{print $1}')
            [ "$a" = "$b" ] || die "md5 不一致: $h:$f"
        done
    else
        for f in $FUSE_BINARIES; do
            a=$(md5sum "$BIN_DIR/$f" | awk '{print $1}')
            b=$(run_on "$h" "md5sum ${INSTALL_PREFIX}/bin/$f" | awk '{print $1}')
            [ "$a" = "$b" ] || die "md5 不一致: $h:$f"
        done
    fi
done
ok "二进制已复制并校验 (md5)"

# ---------- 3. 启动集群: mgmtd -> meta -> storage -> 所有 FUSE ----------
log "===== 步骤3: 启动集群 ====="
service_start "$META_HOST" mgmtd_main
sleep 2
service_start "$META_HOST" meta_main
sleep 2
for h in "${STORAGE_ARR[@]}"; do
    service_start "$h" storage_main
    sleep 2
done
for h in "${FUSE_ARR[@]}"; do
    service_start "$h" hf3fs_fuse_main
done

# ---------- 4. 验证 ----------
log "===== 步骤4: 验证 ====="
for h in "${FUSE_ARR[@]}"; do
    m_ok=0
    for _ in $(seq 1 6); do
        run_on "$h" "mountpoint -q ${MOUNT_POINT}" 2>/dev/null && { m_ok=1; break; }
        sleep 5
    done
    [ "$m_ok" -eq 1 ] || die "$h 的 FUSE 挂载 ${MOUNT_POINT} 未出现"
    ok "$h 挂载正常: $(run_on "$h" "df -h ${MOUNT_POINT} 2>/dev/null | tail -1" | awk '{print $1, $2, $3, $5, $6}')"
done

ADMIN_CLI="${INSTALL_PREFIX}/bin/admin_cli"
ADM_CFG="${INSTALL_PREFIX}/etc/admin_cli.toml"
# 期望上报节点数按清单动态计算: mgmtd + meta + 各 storage（FUSE 节点若上报则多于该值）
EXPECTED_NODES=$(( 2 + ${#STORAGE_ARR[@]} ))
n_ok=0
out=""
for _ in $(seq 1 12); do
    out=$("$ADMIN_CLI" -cfg "$ADM_CFG" "list-nodes" 2>/dev/null)
    cnt=$(echo "$out" | grep -cE 'PRIMARY_MGMTD|HEARTBEAT_CONNECTED')
    [ "$cnt" -ge "$EXPECTED_NODES" ] && { n_ok=1; break; }
    sleep 5
done
echo "$out"
[ "$n_ok" -eq 1 ] || die "60 秒内未等到全部 ${EXPECTED_NODES} 个节点上报 (mgmtd/meta/storage×${#STORAGE_ARR[@]})"

ok "二进制替换完成"
log "回滚方式: 各节点 ${INSTALL_PREFIX}/${BACKUP_SUFFIX} 覆盖回 ${INSTALL_PREFIX}/bin 后按序重启"
log "如 list-chains 出现 UNAVAILABLE，按 references/binary-replacement.md §10 执行 rotate-lastsrv 恢复"
exit 0
