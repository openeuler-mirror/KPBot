# 3FS 二进制滚动替换与回滚 SOP

> **何时读我**：重新编译 3FS（含 folly 分支切换）后，需要把新二进制替换到运行中的集群，或替换失败需要回滚时。日常配置变更见 `cluster-operations.md`，重编译本身见 `build-notes.md`。
>
> 提炼自两份实录：手工替换 SOP（tester 集群，含库文件与 chain 恢复）与 folly 重编译流程（stage 集群）。命令引用集群变量表（见 `installation-guide.md` 开头）。

## 0. 工具选择

| 方式 | 适用 | 说明 |
|------|------|------|
| `scripts/replace_3fs_bin.sh` | 常规批量替换 `${INSTALL_PREFIX}/bin` 下二进制 | 自动完成预检查/顺序停服/备份/md5 校验/顺序启动/验证/ trap 报错快照；节点清单参数化 |
| 本手工 SOP | 需要同时替换 `/usr/lib(64)` 下库文件（libhf3fs_api_shared.so、hf3fs_usrbio.so、fio）、需要 chain 恢复、脚本失败后人工接管 | 步骤与脚本同构，粒度更细 |

两者共用同一套停启顺序与备份思想；先读 §2/§3 理解顺序约束再执行脚本。

## 1. 关键路径与约定

| 项 | 路径 |
|----|------|
| 新编译产物目录 | `${BUILD_DIR}/bin` |
| 3FS 源码目录 | `${SRC_ROOT}/3fs` |
| fio 源码/二进制目录 | `${SRC_ROOT}/fio` |
| 线上二进制目录 | `${INSTALL_PREFIX}/bin` |
| 线上配置目录 | `${INSTALL_PREFIX}/etc` |
| 备份目录 | `${INSTALL_PREFIX}/backup` |
| FUSE API 共享库线上路径 | `/usr/lib/libhf3fs_api_shared.so` |
| fio USRBIO 插件线上路径 | `/usr/lib64/hf3fs_usrbio.so` |
| 线上 fio 路径 `${FIO_BIN}` | 实录 stage 集群为 `/usr/local/bin/fio`，tester 集群为 `${SRC_ROOT}/fio/fio`，按集群实际 |

约定：

- 所有命令默认以 `${SSH_USER}` 在 meta 节点执行（远程操作经 SSH）。
- **不替换 `${INSTALL_PREFIX}/etc` 下的配置文件**（配置变更走 `cluster-operations.md` §3 范式）。
- **不重启 FoundationDB 和 ClickHouse**。
- 凭据：优先 SSH key；必须用密码时 `read -s` 注入 `SSHPASS` 并统一 `sshpass -e`，
  严禁 sshpass 的 `-p` 明文选项。

## 2. 前置：变量注入与产物检查（meta 执行）

```shell
yum install -y sshpass        # 仅密码认证需要；SSH key 免密可跳过

# 凭据交互注入（不进 history、不落盘）
read -s -p 'Password for cluster nodes: ' SSHPASS; export SSHPASS; printf '\n'

export TS=$(date +%Y%m%d_%H%M%S)
export SRC_BIN=${BUILD_DIR}/bin
export BACKUP_ROOT=${INSTALL_PREFIX}/backup/rebuild_${TS}
export SSH_OPTS='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
export RSYNC_RSH="sshpass -e ssh ${SSH_OPTS}"      # SSH key 免密时改为 "ssh ${SSH_OPTS}"
```

确认新编译产物齐全（**任一文件不存在则停止本流程，回到编译步骤修复**）：

```shell
ls -l ${SRC_BIN}/admin_cli \
      ${SRC_BIN}/monitor_collector_main \
      ${SRC_BIN}/mgmtd_main \
      ${SRC_BIN}/meta_main \
      ${SRC_BIN}/storage_main \
      ${SRC_BIN}/hf3fs_fuse_main
ls -l ${BUILD_DIR}/src/lib/api/libhf3fs_api_shared.so
ls -l ${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so
```

## 3. 替换前集群状态快照

```shell
# meta 执行
$ADMIN_CLI "list-nodes"
systemctl status monitor_collector_main --no-pager -l
systemctl status mgmtd_main --no-pager -l
systemctl status meta_main --no-pager -l
systemctl status hf3fs_fuse_main --no-pager -l

# 各 storage 节点（远程）
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl status storage_main --no-pager -l'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl status storage_main --no-pager -l'
```

（`$ADMIN_CLI` 简写定义见 `cluster-operations.md` §0。）

## 4. 停止服务（顺序：FUSE → storage → meta → mgmtd → monitor_collector）

```shell
# meta：停 FUSE 和 meta service
systemctl stop hf3fs_fuse_main
systemctl stop meta_main

# 各 storage 节点：停 storage service
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl stop storage_main'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl stop storage_main'

# meta：停 mgmtd 和 monitor_collector
systemctl stop mgmtd_main
systemctl stop monitor_collector_main

# 确认全部已停止
systemctl is-active hf3fs_fuse_main || true
systemctl is-active meta_main || true
systemctl is-active mgmtd_main || true
systemctl is-active monitor_collector_main || true
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl is-active storage_main || true'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl is-active storage_main || true'
```

多台 FUSE 客户端时逐台停止并确认挂载点已释放（`fusermount3 -u ${MOUNT_POINT}`，
兜底 `umount -l`）。

## 5. 备份旧二进制

备份结构：`${BACKUP_ROOT}/<节点名>/{opt_3fs_bin,usr_lib,usr_lib64}`。

```shell
# meta 本机
mkdir -p ${BACKUP_ROOT}/meta/opt_3fs_bin ${BACKUP_ROOT}/meta/usr_lib ${BACKUP_ROOT}/meta/usr_lib64
cp -a ${INSTALL_PREFIX}/bin/admin_cli ${BACKUP_ROOT}/meta/opt_3fs_bin/ 2>/dev/null || true
cp -a ${INSTALL_PREFIX}/bin/monitor_collector_main ${BACKUP_ROOT}/meta/opt_3fs_bin/ 2>/dev/null || true
cp -a ${INSTALL_PREFIX}/bin/mgmtd_main ${BACKUP_ROOT}/meta/opt_3fs_bin/ 2>/dev/null || true
cp -a ${INSTALL_PREFIX}/bin/meta_main ${BACKUP_ROOT}/meta/opt_3fs_bin/ 2>/dev/null || true
cp -a ${INSTALL_PREFIX}/bin/hf3fs_fuse_main ${BACKUP_ROOT}/meta/opt_3fs_bin/ 2>/dev/null || true
cp -a /usr/lib/libhf3fs_api_shared.so ${BACKUP_ROOT}/meta/usr_lib/ 2>/dev/null || true
cp -a /usr/lib64/hf3fs_usrbio.so ${BACKUP_ROOT}/meta/usr_lib64/ 2>/dev/null || true

# storage 节点（远程，逐台）
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "mkdir -p ${BACKUP_ROOT}/storage1/opt_3fs_bin ${BACKUP_ROOT}/storage1/usr_lib64"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "cp -a ${INSTALL_PREFIX}/bin/admin_cli ${BACKUP_ROOT}/storage1/opt_3fs_bin/ 2>/dev/null || true"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "cp -a ${INSTALL_PREFIX}/bin/storage_main ${BACKUP_ROOT}/storage1/opt_3fs_bin/ 2>/dev/null || true"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "cp -a /usr/lib64/hf3fs_usrbio.so ${BACKUP_ROOT}/storage1/usr_lib64/ 2>/dev/null || true"
# storage2 同上，主机名替换为 storage2

# 确认备份
find ${BACKUP_ROOT} -type f -ls
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "find ${BACKUP_ROOT} -type f -ls"
```

> 脚本 `replace_3fs_bin.sh` 的备份方式略有不同：整目录 `cp -a ${INSTALL_PREFIX}/bin ${INSTALL_PREFIX}/bin.bak.<时间戳>`，回滚时整目录覆盖回去。

## 6. 替换（install / rsync + chmod + ldconfig）

```shell
# meta 本机：install 保留权限位
install -m 0755 ${SRC_BIN}/admin_cli ${INSTALL_PREFIX}/bin/admin_cli
install -m 0755 ${SRC_BIN}/monitor_collector_main ${INSTALL_PREFIX}/bin/monitor_collector_main
install -m 0755 ${SRC_BIN}/mgmtd_main ${INSTALL_PREFIX}/bin/mgmtd_main
install -m 0755 ${SRC_BIN}/meta_main ${INSTALL_PREFIX}/bin/meta_main
install -m 0755 ${SRC_BIN}/hf3fs_fuse_main ${INSTALL_PREFIX}/bin/hf3fs_fuse_main
install -m 0755 ${BUILD_DIR}/src/lib/api/libhf3fs_api_shared.so /usr/lib/libhf3fs_api_shared.so
install -m 0755 ${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so /usr/lib64/hf3fs_usrbio.so
ldconfig

# 可选：本次也重编了 fio 时（${FIO_BIN} 见 §1 路径表）
install -m 0755 ${SRC_ROOT}/fio/fio ${FIO_BIN}

# storage 节点（远程，逐台推送）
rsync -avz -e "${RSYNC_RSH}" ${SRC_BIN}/admin_cli storage1:${INSTALL_PREFIX}/bin/admin_cli
rsync -avz -e "${RSYNC_RSH}" ${SRC_BIN}/storage_main storage1:${INSTALL_PREFIX}/bin/storage_main
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so storage1:/usr/lib64/hf3fs_usrbio.so
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "chmod 0755 ${INSTALL_PREFIX}/bin/admin_cli ${INSTALL_PREFIX}/bin/storage_main /usr/lib64/hf3fs_usrbio.so && ldconfig"
# storage2 同上

# 可选：推送重编的 fio 到 storage 节点
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/fio/fio storage1:${FIO_BIN}
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "chmod 0755 ${FIO_BIN}"
```

多台 FUSE 客户端时，向每台推送 hf3fs_fuse_main、admin_cli、libhf3fs_api_shared.so、
hf3fs_usrbio.so（清单同 `fuse-client-expansion.md` §7）。

## 7. ldd 检查（有缺失绝不启动）

```shell
# meta 本机
for b in admin_cli monitor_collector_main mgmtd_main meta_main hf3fs_fuse_main; do
  ldd ${INSTALL_PREFIX}/bin/$b | grep 'not found' || true
done
ldd /usr/lib/libhf3fs_api_shared.so | grep 'not found' || true

# 远程 storage 节点
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "ldd ${INSTALL_PREFIX}/bin/storage_main | grep 'not found' || true"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 "ldd ${INSTALL_PREFIX}/bin/storage_main | grep 'not found' || true"
```

**任何输出 `not found`：先修复缺失库（补装依赖或从编译节点拷贝 so + ldconfig），绝不启动服务。**

## 8. 按顺序启动（monitor_collector → mgmtd → meta → storage → FUSE）

```shell
# meta：monitor_collector 与 mgmtd
systemctl daemon-reload
systemctl start monitor_collector_main
systemctl status monitor_collector_main --no-pager -l
systemctl start mgmtd_main
systemctl status mgmtd_main --no-pager -l

# meta：meta service
systemctl start meta_main
systemctl status meta_main --no-pager -l

# 各 storage 节点（远程，逐台）
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl daemon-reload && systemctl start storage_main && systemctl status storage_main --no-pager -l'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl daemon-reload && systemctl start storage_main && systemctl status storage_main --no-pager -l'

# meta（及各 FUSE 客户端）：FUSE
systemctl start hf3fs_fuse_main
systemctl status hf3fs_fuse_main --no-pager -l
```

实录 folly 重编译流程在每步之间留 2~5 秒间隔（mgmtd/meta 后 sleep 3，storage 全部拉起后 sleep 5）。

## 9. 启动后验证

```shell
$ADMIN_CLI "list-nodes"
$ADMIN_CLI "list-chains"
df -h | grep ${MOUNT_POINT}
```

确认：MGMTD 为 `PRIMARY_MGMTD`；META 与全部 STORAGE 为 `HEARTBEAT_CONNECTED`；
FUSE 挂载点 `${MOUNT_POINT}` 存在；chain 全部 `SERVING`。

如服务失败，优先看日志：

```shell
journalctl -u monitor_collector_main -n 100 --no-pager
journalctl -u mgmtd_main -n 100 --no-pager
journalctl -u meta_main -n 100 --no-pager
journalctl -u hf3fs_fuse_main -n 100 --no-pager
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'journalctl -u storage_main -n 100 --no-pager'
```

## 10. UNAVAILABLE chain 恢复（rotate-lastsrv）

整机重启后 chain 可能变为 `UNAVAILABLE`，需要对每条受影响 chain 执行 `rotate-lastsrv`。
**chain 列表必须从 list-chains 动态获取，不要硬编码 chain ID**（原 SOP 硬编码了 24 条
900100001~900400006——示例编号方案，换集群/重建表后即失效）：

```shell
sleep 10        # 等服务与心跳稳定
$ADMIN_CLI "list-chains" | awk '$4=="UNAVAILABLE" {print $1}' | while read -r chain; do
  $ADMIN_CLI "rotate-lastsrv $chain" 2>&1 | head -1
done

# 等待 chain 同步（约 30-60 秒）后复查状态统计
sleep 30
$ADMIN_CLI "list-chains" | awk '{print $4}' | sort | uniq -c
# 预期全部 SERVING；仍有 UNAVAILABLE 则重复上面的 rotate 循环或按 §9 查日志
```

（list-chains 输出第 1 列为 ChainId、第 4 列为 Status，回显样例见 `installation-guide.md` §5.8。）

## 11. （可选）同步 folly 源码到调试节点

用于在 FUSE 客户端上 GDB 调试时源码对齐：

```shell
FOLLY_DIR=${SRC_ROOT}/3fs/third_party/folly
sshpass -e rsync -az --delete ${FOLLY_DIR}/ ${SSH_USER}@<调试节点>:${FOLLY_DIR}/

# 验证分支一致
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@<调试节点> "cd ${FOLLY_DIR} && git branch --show-current && git log --oneline -3"
```

## 12. 回滚流程（新二进制启动失败时）

```shell
# 1) 停止服务（顺序同 §4）
systemctl stop hf3fs_fuse_main
systemctl stop meta_main
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl stop storage_main'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl stop storage_main'
systemctl stop mgmtd_main
systemctl stop monitor_collector_main

# 2) 回滚 meta 本机文件
cp -a ${BACKUP_ROOT}/meta/opt_3fs_bin/* ${INSTALL_PREFIX}/bin/ 2>/dev/null || true
cp -a ${BACKUP_ROOT}/meta/usr_lib/libhf3fs_api_shared.so /usr/lib/ 2>/dev/null || true
cp -a ${BACKUP_ROOT}/meta/usr_lib64/hf3fs_usrbio.so /usr/lib64/ 2>/dev/null || true
ldconfig

# 3) 回滚 storage 节点文件（逐台）
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "cp -a ${BACKUP_ROOT}/storage1/opt_3fs_bin/* ${INSTALL_PREFIX}/bin/ 2>/dev/null || true"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 "cp -a ${BACKUP_ROOT}/storage1/usr_lib64/hf3fs_usrbio.so /usr/lib64/ 2>/dev/null || true"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'ldconfig'
# storage2 同上

# 4) 按原顺序重新启动（§8），随后按 §9 验证、必要时按 §10 恢复 chain
systemctl start monitor_collector_main
systemctl start mgmtd_main
systemctl start meta_main
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage1 'systemctl start storage_main'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@storage2 'systemctl start storage_main'
systemctl start hf3fs_fuse_main
$ADMIN_CLI "list-nodes"
```

使用 `replace_3fs_bin.sh` 时的回滚：各节点 `${INSTALL_PREFIX}/bin.bak.<时间戳>` 整目录覆盖回
`${INSTALL_PREFIX}/bin`，再按 §8 顺序重启（脚本结束日志会打印备份目录名）。

## 13. 清理环境变量

```shell
unset SSHPASS
unset RSYNC_RSH
```
