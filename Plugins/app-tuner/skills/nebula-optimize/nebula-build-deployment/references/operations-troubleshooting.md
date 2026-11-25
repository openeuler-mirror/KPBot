# 运行、故障处理与升级回滚

## 1. 启停

### 1.1 启动顺序

```
metad → graphd×N → ADD HOSTS（仅首次）→ storaged×M
```

```bash
cd "${NEBULA_HOME}"          # 必须：依赖 share/resources/date_time_zonespec.csv
rm -f cluster.id             # 清库/恢复后必须

# PID 由进程自写（模板 --pid_file=${PID_DIR}/nebula-*.pid）。注意：模板设
# --daemonize=true 时 shell 的 $! 是立即退出的父进程（ProcessUtils::daemonize 会
# fork 后父进程退出），不能用 $! 记 PID；停服/备份/恢复一律读 ${PID_DIR} 的 pid 文件。
mkdir -p "${PID_DIR}" "${LOG_DIR}"

numactl -C "${META_CPUS}" -m "${META_NODE}" \
  "${BUILD_DIR}/bin/nebula-metad" --flagfile="${CONF_DIR}/metad.conf" &
sleep 5

for i in 1 2 3 4; do
  numactl -C "${GRAPH_CPUS[$i]}" -m "${GRAPH_NODE[$i]}" \
    "${BUILD_DIR}/bin/nebula-graphd" --flagfile="${CONF_DIR}/graphd${i}.conf" &
done
sleep 5

# 首次部署才需要；必须与启动循环的 storaged 数一致（参考拓扑为 4）
"${CONSOLE_BIN}" -addr 127.0.0.1 -port "${GRAPH_PORT_1}" \
  -u "${NEBULA_USER}" \
  -e "ADD HOSTS 127.0.0.1:${STORAGE_PORT_1}, 127.0.0.1:${STORAGE_PORT_2}, 127.0.0.1:${STORAGE_PORT_3}, 127.0.0.1:${STORAGE_PORT_4};"

for i in 1 2 3 4; do
  numactl -C "${STORAGE_CPUS[$i]}" -m "${STORAGE_NODE[$i]}" \
    "${BUILD_DIR}/bin/nebula-storaged" --flagfile="${CONF_DIR}/storaged${i}.conf" &
done
sleep 20   # 等心跳与分区上线

# 启动后校验：SHOW HOSTS 应有 4 个 storaged host，且最终全部 ONLINE；
# 少于实例数或长期 OFFLINE 时，停止后续导入/压测并查 ADD HOSTS/心跳/端口。
"${CONSOLE_BIN}" -addr 127.0.0.1 -port "${GRAPH_PORT_1}" \
  -u "${NEBULA_USER}" -e "SHOW HOSTS;"
```

### 1.2 停止顺序（反序）

```bash
STOP_TIMEOUT="${STOP_TIMEOUT:-120}"
PID_DIR="${PID_DIR:-/data/nebula/pids}"   # 与模板 --pid_file 一致（进程自写，daemonize 安全）

# 仅支持分布式三进程形态：standalone（单进程 nebula-standalone）下按 PID 停服会
# “无进程可停”而误判已停，后续数据目录操作会撞上仍在写入的实例，直接拒绝。
command -v pgrep >/dev/null 2>&1 || { echo "ERROR: 缺少 pgrep，无法确认部署形态" >&2; exit 1; }
# 用 -f 匹配完整命令行：进程 comm 上限 15 字符，"nebula-standalone"（17 字符）被截断，
# pgrep -x 永远匹配不到（形态守卫会变死代码）。
pgrep -f nebula-standalone >/dev/null 2>&1 && {
  echo "ERROR: 检测到 nebula-standalone 进程，本流程不适用于 standalone 形态，拒绝继续" >&2
  exit 1
}

# 只停本次部署的实例：从 ${PID_DIR}/${name}*.pid 定位（进程经 --pid_file 自写、
# 必然准确——daemonize 下 shell 的 $! 是已退出的父进程，不可用），核对
# /proc/PID/exe 属于本 BUILD_DIR 后才发 TERM。禁止按进程名 pkill —— 同机另一套 Nebula 会被误停。
stop_component() {
  local name="$1" timeout="${2:-120}" waited=0 pid exe pidfile alive=0
  local -a pids=()
  for pidfile in "${PID_DIR}/${name}"*.pid; do
    [ -e "$pidfile" ] || {
      echo "ERROR: 未找到 ${PID_DIR}/${name}*.pid（进程自写的 --pid_file），无法定位本次部署的 ${name}" >&2
      return 1
    }
    pid=$(cat "$pidfile")
    # kill -0 失败有两种含义：进程不存在（ESRCH，可清理 pidfile）或无信号权限（EPERM，
    # 进程仍在运行）。用 /proc/<pid> 是否存在来区分：无 /proc 条目才算已退出；
    # 进程存在但 kill -0 失败 = 权限不足——无法确认退出，保留 pidfile 并中止，
    # 防止备份/恢复在 RocksDB 仍在写时操作数据目录。
    if ! kill -0 "$pid" 2>/dev/null; then
      if [ ! -d "/proc/${pid}" ]; then
        rm -f "$pidfile"; continue
      fi
      echo "ERROR: PID ${pid}（${pidfile}）存在但无信号权限（kill -0 失败、/proc/${pid} 仍在），无法确认退出，中止——请核对运行身份后重试" >&2
      return 1
    fi
    exe=$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)
    case "$exe" in
      "${BUILD_DIR}/bin/${name}") : ;;
      *) echo "ERROR: ${pidfile} 中 PID ${pid} 的可执行文件(${exe:-未知})不属于本次部署，拒绝停止" >&2; return 1 ;;
    esac
    # 第二重核对：进程实际加载的 flagfile 必须属于本次 CONF_DIR——两套部署共用
    # BUILD_DIR 而使用不同 flagfile 时，仅凭 exe 归属无法排除另一实例的陈旧 PID
    cmdline=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    case "$cmdline" in
      *"--flagfile=${CONF_DIR}/"*|*"--flagfile ${CONF_DIR}/"*) pids+=("$pid") ;;
      "") pids+=("$pid") ;;  # cmdline 不可读（权限/已僵死）时保留 exe 侧判定
      *) echo "ERROR: PID ${pid} 的 flagfile 不在本次 CONF_DIR(${CONF_DIR})——疑似另一实例的进程，拒绝停止（cmdline: ${cmdline}）" >&2; return 1 ;;
    esac
  done
  [ "${#pids[@]}" -gt 0 ] || return 0
  kill -TERM "${pids[@]}" 2>/dev/null || true
  while :; do
    alive=0
    for pid in "${pids[@]}"; do [ -d "/proc/${pid}" ] && alive=1; done   # 按 /proc 存在性判活，避免 kill -0 权限失败误判为已退出
    [ "$alive" -eq 0 ] && break
    if [ "$waited" -ge "$timeout" ]; then
      echo "ERROR: ${name} 未在 ${timeout}s 内退出" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  rm -f "${PID_DIR}/${name}"*.pid
}

# 反序停止，并确认进程已退出后再做下一步
stop_component nebula-storaged "$STOP_TIMEOUT" || exit 1
stop_component nebula-graphd   "$STOP_TIMEOUT" || exit 1
stop_component nebula-metad    "$STOP_TIMEOUT" || exit 1

# 只有上面的等待超时、且确认需要强制终止时，才人工执行 kill -9；
# 不要作为默认收尾步骤。
```

要点：

- 先发 SIGTERM，让 RocksDB flush/WAL 收尾；按 `STOP_TIMEOUT` 等待进程真正退出，超时后才考虑 `kill -9`。
- 顺序反了（先杀 metad）可能导致 storaged 心跳失败、元数据不一致。
- 大 WAL 或后台 compaction 时停机更慢，不要过早强杀。

### 1.3 状态检查

```bash
# 进程与线程数
ps -eo pid,nlwp,comm,args | grep [n]ebula

# 端口
ss -lntp | grep -E '9559|967[0-9]|977[0-9]|978[0-9]'

# 节点/分区
"${CONSOLE_BIN}" -addr 127.0.0.1 -port "${GRAPH_PORT_1}" \
  -u "${NEBULA_USER}" -e "SHOW HOSTS; SHOW SPACES;"

# 日志
tail -n 100 "${LOG_DIR}"/*stderr.log
# 排障前先把 --minloglevel 降到 0/1 并重启，否则 ERROR/INFO 根本不落盘（=3 等于 FATAL only）
grep -E 'SIGSEGV|Address already in use|Machine not existed|OFFLINE|Leader|Invalid timezone' "${LOG_DIR}"/*.log | tail
```

## 2. 故障速查

| 症状 | 根因 | 处置 |
|------|------|------|
| 启动即退出（退出码 1，日志含 `Invalid timezone file`） | 不在 `${NEBULA_HOME}` 下启动，找不到时区文件 | `cd ${NEBULA_HOME}` 后启动；检查 `share/resources/date_time_zonespec.csv` |
| 启动即 SIGSEGV/SIGBUS | F14/jemalloc/`-march` 与 third-party 不匹配 | 核对二进制 md5、third-party 与编译参数 |
| `Invalid timezone file` | 时区文件缺失/路径错 | 复制 resources 到 `share/resources` |
| `Address already in use` | storaged 端口间隔 <3；旧进程未退 | 重排端口（间隔 ≥3）；`ss` 确认占用；等待旧进程退出 |
| storaged `Machine not existed!` | 机器未在 metad 注册（ADD HOSTS 未执行/元数据被清） | 重新执行 `ADD HOSTS`（与 cluster.id 无关） |
| `Reject wrong cluster host` | `cluster.id` 不匹配/meta 与 storage 不配对 | `rm -f cluster.id`；从同一备份成对恢复 |
| `SHOW HOSTS` OFFLINE | 启动顺序错、端口不通、cluster.id、meta 地址错 | 按顺序重启；检查 `--meta_server_addrs`、端口、日志 |
| 分区数少于预期 | `ADD HOSTS` 未完成；storaged 未全部上线 | 确认所有 storaged ONLINE；等待心跳；重新 `SHOW HOSTS` |
| 导入报 CSV 错误 | 表头/分隔符/日期格式不对 | 去表头、核对 `delimiter` 与 `withHeader` |
| `Text file busy` | 覆盖运行中的二进制 | 先停进程，再替换 |
| 编译链接 `-lstdc++` | 缺 `libstdc++-static` | 安装后重编 |
| 优化版启动崩溃/链接失败 | F14 ABI、jemalloc 路径、`-march` 不兼容 | 回到 native 基线；按 build 参考逐项核对 |
| metad 线程数异常多 | standalone 未设置 meta 线程池 | 用补丁/flag 限制 `meta_num_*`，核对 `ps -o nlwp` |
| 写入长尾/卡顿 | WAL fsync、compaction、快照、Raft 选举 | 看磁盘 await/%util、storaged 日志、compaction 指标 |
| 读延迟突然升高 | 数据被淘汰、block cache 不足、NUMA 漂移 | 预热、调 cache、检查绑核/内存绑定 |

## 3. 日志与证据

| 文件 | 内容 |
|------|------|
| `metad-stderr.log` / `graphd*-stderr.log` / `storaged*-stderr.log` | 启动失败、崩溃、断言 |
| `*-stdout.log` | 标准输出/部分初始化信息 |
| `nebula-metad.INFO` / `nebula-graphd.INFO` / `nebula-storaged.INFO` | 运行日志（glog） |
| `import.log` / `failed/*.csv` | 导入成功/失败明细 |
| `dmesg` | OOM、磁盘错误、内核异常 |

采集故障时同时记录：时间、命令、配置文件、二进制 md5/commit、`SHOW HOSTS` 输出、
相关日志片段、`ss`/`ps` 快照。不要只贴一句报错。

## 4. 版本切换与升级

### 4.1 切换 native / 优化二进制

```bash
# 1) 停服（反序，见 1.2 节；确认进程退出后才操作数据/二进制）
# 2) 备份当前二进制：目录缺失或备份失败都不得继续替换
mkdir -p "${BACKUP_BIN}" || exit 1
BIN_BAK="${BACKUP_BIN}/nebula-graphd.$(date +%Y%m%d_%H%M%S)"
cp -a "${BUILD_DIR}/bin/nebula-graphd" "${BIN_BAK}" || {
  echo "ERROR: 备份当前二进制失败，终止替换" >&2; exit 1;
}
[ -s "${BIN_BAK}" ] || { echo "ERROR: 备份文件为空，终止替换" >&2; exit 1; }
echo "已备份旧二进制: ${BIN_BAK}（回滚用它）"
# 3) 替换（备份成功后才执行）
cp -f "${NEW_BIN_DIR}/nebula-graphd" "${BUILD_DIR}/bin/nebula-graphd" || {
  echo "ERROR: 新二进制拷贝失败" >&2; exit 1;
}
md5sum "${BUILD_DIR}/bin/nebula-graphd"
# 4) 启动并验收；失败时回滚：cp -f "${BIN_BAK}" "${BUILD_DIR}/bin/nebula-graphd" 后复验
```

- 数据格式一致时可直接复用数据；仍建议重置到干净备份再对比性能。
- 编译参数不同（LTO/LSE/jemalloc）不影响数据格式，但会影响性能结论，必须记录。
- 替换后如果启动失败，立即用上面记录的 `${BIN_BAK}` 回滚并复验。

### 4.2 升级 third-party / 大版本

```bash
# 1) 备份数据与二进制
# 2) 记录旧版本：third-party 版本、jemalloc、RocksDB、commit
# 3) 新建构建目录（不要覆盖旧 build）
# 4) 应用适配补丁，编译新二进制
# 5) 用新二进制启动旧数据（先小数据/测试 Space）
# 6) 功能验收 + 性能对比
# 7) 不达标或异常 → 回滚二进制与数据
```

注意：

- RocksDB 只向后兼容；新版本数据旧二进制可能打不开。
- 全量第三方升级对读路径收益可能接近 0，写入路径可能受 RocksDB 版本显著影响。
- 没有明确收益时，优先保持已验证的 3.3 依赖栈，避免长周期升级风险。

## 5. 变更回滚清单

| 变更 | 备份 | 回滚 |
|------|------|------|
| 二进制替换 | `bin.bak.<ts>` + md5 | 停服 → 恢复 bak → 启动 → 验收 |
| 配置修改 | 原配置文件 `.bak` | 停服/热重载（视参数）→ 恢复 → 重启 → 验收 |
| 数据导入 | 导入前备份 | 停服 → 清空 → 恢复备份 → 删 `cluster.id` → 启动 |
| third-party 升级 | 旧 build/二进制 + 数据备份 | 用旧二进制启动旧数据；必要时恢复备份 |
| 端口/绑核调整 | 旧配置 + 启动命令 | 恢复配置/命令，按顺序重启 |

回滚后必须复验：

- `SHOW HOSTS` 全部 ONLINE；
- 关键查询返回正确；
- 性能回到基线噪声范围内；
- 日志无新异常。

## 6. 维护建议

- 固定并记录版本：Nebula commit、third-party 版本、jemalloc、GCC/glibc、编译参数。
- 配置、启动命令、绑核布局纳入版本管理；不要只存在 shell history。
- 每次部署/升级/回滚都留一份“变更记录 + 验收结果 + 回滚命令”。
- 定期检查磁盘空间、inode、日志增长、compaction 状态；磁盘满是生产常见故障。
- 生产变更安排维护窗口；涉及端口/绑核/重启的变更提前准备 BMC/串口通道。
