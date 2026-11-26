# 部署生命周期与配置模板

> 主动执行类。initdb/create 覆盖类操作必须显式确认；pg_ctl 停止用 fast 模式默认。

## 1. initdb（新实例）

```bash
# 目录必须为空或不存在；已存在非空目录直接拒绝（防覆盖既有数据）
if [ -e "${PGDATA}" ] && [ -n "$(ls -A "${PGDATA}")" ]; then
  echo "ERROR: ${PGDATA} 非空，拒绝 initdb（防覆盖），换目录或先归档" >&2
  exit 3
fi
# 初始口令经 --pwfile 传入——生命周期收口：mktemp 排他创建（0600，位于已存在且
# 受保护的目录）→ initdb → trap 兜底清理。不用 ${PGDATA}/../initdb_pw.$$：
# PGDATA 尚不存在时路径解析必须穿过不存在的目录，写文件直接失败，initdb 根本
# 走不到创建数据目录那一步
umask 077
pwfile="$(mktemp "${TMPDIR:-/tmp}/initdb_pw.XXXXXX")" \
  || { echo "ERROR: 无法创建口令临时文件（${TMPDIR:-/tmp} 可写？）" >&2; exit 1; }
trap 'rm -f "$pwfile"' EXIT INT TERM   # 退出或中断都清理——明文口令不留盘
# PGHOST 为 TCP/SCRAM 时不设口令会导致首次登录失败（交互式 -W 亦可，脚本推荐 pwfile）
if [ -z "${INITDB_PASSWORD:-}" ]; then
  read -rs -p "输入初始超级用户口令: " INITDB_PASSWORD; echo
fi
printf '%s\n' "$INITDB_PASSWORD" > "$pwfile"   # mktemp 已排他创建，重定向写不再有竞态
"${PG_BIN}/initdb" -D "${PGDATA}" -U "${PGUSER}" -E UTF8 --locale=C.UTF-8 \
  --pwfile="$pwfile" \
  || { echo "ERROR: initdb 失败" >&2; exit 1; }   # 清理由 trap 统一收口
unset INITDB_PASSWORD
```

- initdb 后 `${PGDATA}` 属主为运行用户；`pg_hba.conf` 按网络可达范围收紧
  （本机压测口径：`host ... 127.0.0.1/32 scram-sha-256`；对外服务的认证与来源
  限制在部署清单中显式评审）。
- 不要在模板里写示例密码/真实凭据；口令经 `PGPASSWORD`/`~/.pgpass` 注入。

## 2. pg_ctl 生命周期

```bash
"${PG_BIN}/pg_ctl" -D "${PGDATA}" -l "${PGDATA}/logfile" -o "-p ${PG_PORT}" start   # 非默认端口必须显式传给服务端
"${PG_BIN}/pg_ctl" -D "${PGDATA}" status        # 就绪判定：status + SELECT 1
"${PG_BIN}/pg_ctl" -D "${PGDATA}" stop -m fast   # 缺省 fast：断连接+回滚未结事务
```

- 就绪不是“进程在”就完：`pg_ctl status` 返回 0 后还要 `psql -c 'SELECT 1'`
  成功（恢复中实例会让前者通过而后者异常）。但 **SELECT 1 只证明「可查询」不证明
  「可写」**——可读热备与显式只读事务下它同样成功，仅靠该查询会把只读目标验收
  为可写。按目标角色声明验收期望：只读目标（热备/只读从库）到 SELECT 1 即可；
  需要**写入**的目标还须同时确认 `pg_is_in_recovery()` 为假且
  `current_setting('transaction_read_only') = 'off'`。
- stop 模式：fast（默认）断连回滚；immediate 仅崩溃模拟场景；smart 等连接自退
  （压测环境挂死概率高，不建议）。
- restart 承载 `shared_preload_libraries` 变更（reload 不生效，见
  `references/contrib-extensions-and-patches.md` §1）。

## 3. postgresql.conf 关键 GUC 模板（压测基线口径）

```ini
# ---- 连接与内存（按机器规格替换，勿抄数字当默认）----
listen_addresses = '*'                  # 对外可达时同步收紧 pg_hba.conf
max_connections = 300                   # 一连接一进程：过高连接数放大共享行竞争
shared_buffers = 96GB                   # 见 performance 侧“不是越大越好”口径
effective_cache_size = 200GB            # 规划器提示，非分配
work_mem = 16MB                         # 每排序/哈希节点，连接数×该值是隐性上限

# ---- WAL 与提交（写负载敏感项）----
wal_buffers = 128MB                     # 默认口径见架构参考；写重负载显式加大
synchronous_commit = on                 # 延迟换吞吐的开关在 performance 侧评审
commit_delay = 0                        # 组提交延迟，默认关

# ---- 后台刷脏节奏（读写混合负载的关键组）----
bgwriter_delay = 200ms
bgwriter_lru_maxpages = 1000
bgwriter_lru_multiplier = 2.0
checkpoint_completion_target = 0.9      # 摊平 checkpoint，避免末期刷脏风暴

# ---- 观测（A/B 两侧必须同开关）----
# 前提：顶层 make install 不安装 contrib！须先
#   make -C contrib/pg_stat_statements && make -C contrib/pg_stat_statements install
# 并在目标库 CREATE EXTENSION pg_stat_statements 后再启用预载（否则启动报缺共享库）
shared_preload_libraries = 'pg_stat_statements'
track_io_timing = on                    # 有小幅开销，基线与优化轮都要开
```

- 模板数字是**口径示例**（240 核/大内存压测环境），不是推荐值；逐项决策依据在
  `../../pgsql-performance-optimization/references/wal-and-plan-optimizations.md`。
- 每次改配置记 diff（`ALTER SYSTEM` 走 `pg_settings`，文件改走 `postgresql.conf`
  副本对比），A/B 轮次间除目标变量外配置必须完全一致。

## 4. 二进制备份与版本回滚

```bash
# 替换前整套备份（bin+lib+share 同套退回，避免 lib 版本错配）
ts=$(date +%Y%m%d_%H%M%S)
bdir="${BIN_BACKUP_DIR}/pre_swap_${ts}_$$"
if [ -e "$bdir" ]; then echo "ERROR: 备份目录已存在: $bdir" >&2; exit 3; fi
mkdir -p "$bdir"
cp -a --reflink=never "${PG_INSTALL}/bin" "${PG_INSTALL}/lib" \
      "${PG_INSTALL}/share" "$bdir/" || { echo "ERROR: 备份失败，停止替换" >&2; exit 1; }
[ -n "$(ls -A "$bdir/bin")" ] || { echo "ERROR: 备份为空，停止替换" >&2; exit 1; }

# 停服（pg_ctl stop -m fast 且 SELECT 1 不再成功）后替换、启动、冒烟
# 冒烟（../../pgsql-test-verification/scripts/smoke_test.sh）通过才放量

# 回滚：停服 → 删当前 bin/lib/share → 从 "$bdir" 成套 mv 回 → 启动 → 冒烟
# 回退路径各目录逐一 mv 回原名（与备份的成套目录一一对应），勿嵌套 mv
```

- 备份目录已存在即拒绝（时间戳+PID 命名，重跑不混轮）；
- 回滚后必须复跑冒烟与版本核对（`pg_config --version` 与 md5 对照备份时记录），
  通过才算回滚完成。
