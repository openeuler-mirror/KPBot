# Redis 部署执行手册

> 本文件是 `redis-deploy` SKILL.md 的执行细节层：SKILL.md 负责触发、版本策略与总体流程，
> 此处给出环境矩阵、离线部署、多实例、升级回滚、卸载、验收命令与排障细节。
> 性能调优（绑核、网卡队列、ntuple、redis.conf 深度参数）不在本手册范围，见 skill `redis-optimize`。

## 1. 支持范围与前置条件

| 项 | 要求 |
|----|------|
| 操作系统 | openEuler 24.03 LTS（已验证）、RHEL/CentOS/Rocky/Alma 8+、Debian 11+/Ubuntu 20.04+ |
| CPU | aarch64（鲲鹏）或 x86_64 |
| 权限 | root（创建用户、写 `/etc`、安装 systemd 单元） |
| 构建 | gcc、make、tar；脚本可自动安装（`--skip-deps` 时需自备） |
| 下载 | curl 或 wget；离线场景使用 `--tarball` |
| 磁盘 | 安装前缀 + 构建缓存至少 1 GiB 余量 |
| 服务管理 | systemd（无 systemd 时用 `--no-systemd`，手工前台启动） |

默认路径布局（单实例 `redis`，端口 6379）：

```text
/opt/redis-7.0.15/bin/redis-server
/opt/redis-7.0.15/bin/redis-cli
/etc/redis/redis.conf                  # 0640 root:redis
/etc/systemd/system/redis.service      # 0644
/var/lib/redis/redis/                  # 数据目录，redis:redis 0750
/var/log/redis/redis.log               # 日志
/run/redis/redis.pid                   # pid 目录占位
```

部署产物：

```text
$RESULT_DIR/env/deploy.env             # 部署契约，不含凭据
$RESULT_DIR/logs/install.log           # 主流程日志
$RESULT_DIR/logs/deps.log              # 依赖安装日志
$RESULT_DIR/logs/build.log             # 编译/安装日志
```

## 2. 版本与校验策略

- **未指定版本时固定 Redis 7.0.15**，不自动选择发行版仓库里的 redis 包（版本不可控）。
- `assets/checksums.txt` 已收录 7.0.15 的官方 sha256（来源 `redis/redis-hashes`）。
- 指定其他版本时：

  ```bash
  bash scripts/install_redis.sh --version 7.2.4 --sha256 <64位sha256>
  ```

  校验值从官方 release 页或 `https://github.com/redis/redis-hashes` 获取，核对后再填写。
- `--allow-unverified` 仅用于无法获取校验值的封闭环境，会把实际 sha256 写入 `deploy.env` 并保留告警。
- 离线部署用 `--tarball` 指向本地 `redis-<version>.tar.gz`，脚本仍执行 sha256 校验。

新增内置版本：在 `assets/checksums.txt` 追加 `版本 sha256` 一行，并在
`evals/evals.json` 补一条对应版本用例。

## 3. 标准部署流程

```bash
cd <skill-dir>

# 1) 只读前置检查
bash scripts/preflight_check.sh --version 7.0.15 --port 6379

# 2) 部署（默认 7.0.15 / 单实例 / 127.0.0.1:6379）
bash scripts/install_redis.sh

# 3) 验收
/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 ping
systemctl status redis --no-pager
cat /var/log/redis-deploy/<run-id>/env/deploy.env
```

执行顺序（脚本内部）：

1. `install_deps.sh`：按 dnf/yum/apt/zypper 安装 gcc、make、tar、下载工具与 CA 证书。
2. 下载或复用 `$BUILD_ROOT/download/redis-<version>.tar.gz` → sha256 校验 → 解压到独立构建目录。
3. `make -j$(nproc)` → `make PREFIX=<prefix> install`；同版本已存在时默认复用，`--force` 强制重编译。
4. 创建系统用户 `redis`（已存在则复用）、数据/日志/pid 目录。
5. 渲染 `redis.conf`（旧文件先备份为 `.bak.<时间戳>`）与 systemd 单元。
6. `systemctl enable --now`（实际为 daemon-reload + enable + restart）→ PING / INFO 验收。
7. 写 `deploy.env`（`DEPLOY_STATUS=ok` 表示成功；失败时写 `failed` 与 `FAILED_PHASE`）。

常用参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--version` | 7.0.15 | Redis 版本 |
| `--instance-name` | redis | 配置/服务/数据目录名 |
| `--port` | 6379 | 监听端口 |
| `--bind` | 127.0.0.1 | 可多地址；非回环必须配密码 |
| `--prefix` | /opt/redis-<version> | 安装前缀 |
| `--password-file` | 无 | requirepass 来源，要求 600 |
| `--tarball` | 无 | 离线源码包 |
| `--sha256` | 无 | 非内置版本的校验值 |
| `--build-tls` | 关闭 | 需要 OpenSSL 开发包 |
| `--skip-deps` | 关闭 | 跳过依赖安装 |
| `--no-systemd` | 关闭 | 只落盘，不启动服务 |
| `--keep-conf` | 关闭 | 保留现有配置（不写入密码） |
| `--dry-run` | 关闭 | 只打印计划 |

## 4. 同机多实例

多实例通过 `--instance-name` 与 `--port` 组合隔离，路径与单元自动带实例名：

```bash
bash scripts/install_redis.sh --instance-name redis6380 --port 6380
bash scripts/install_redis.sh --instance-name redis6381 --port 6381
```

得到 `redis6380.service`、`/etc/redis/redis6380.conf`、`/var/lib/redis/redis6380/`。
每个实例独立 systemd 单元，可分别启停；网卡队列/中断/ntuple 的按实例分流见 `redis-optimize`。

注意：

- 多实例共享同一版本前缀时，后一次部署会复用已编译产物。
- 每个实例必须使用不同端口；脚本检测到端口占用会失败。
- 同一台机器多实例的内存与连接数需按业务规划，部署阶段不设置 `maxmemory`。

## 5. 在线与离线部署

在线（默认）：

```bash
bash scripts/install_redis.sh
```

离线：

```bash
# 在有网机器获取源码包并记录校验值，拷贝到目标机
sha256sum redis-7.0.15.tar.gz
bash scripts/install_redis.sh --tarball /root/redis-7.0.15.tar.gz

# 若无内置校验值，显式提供
bash scripts/install_redis.sh --tarball /root/redis-7.2.4.tar.gz --sha256 <sha256>
```

离线依赖：

- 目标机已有 gcc/make/tar 时可用 `--skip-deps`。
- 缺少工具链时，预先挂载 ISO 或配置本地 yum/apt 源安装 `gcc make tar`（TLS 构建另需 `openssl-devel`/`libssl-dev`）。
- 脚本不会把远程下载内容直接交给 shell 执行，也不会关闭 TLS 校验。

## 6. 升级

1. 先备份数据和现有配置：

   ```bash
   systemctl stop redis
   cp -a /etc/redis/redis.conf /root/redis.conf.bak.$(date +%Y%m%d%H%M%S)
   cp -a /var/lib/redis/redis /root/redis-data.bak.$(date +%Y%m%d%H%M%S)
   ```

2. 用同一实例名与新版本部署（如 `--version 7.2.4 --sha256 <...>`）；脚本会备份旧配置/单元并重启服务。
3. 验收：`redis-cli INFO server | grep redis_version`、`PING`、业务连通性。
4. RDB 格式跨大版本升级不可逆（Redis 7 的 RDB v10 不能被旧版本读取）；回滚前必须保留旧版本二进制与旧数据快照。

## 7. 回滚与卸载

> **危险操作警告**：卸载与清理会永久删除安装文件、配置或数据，且不可恢复。以下命令仅供人工确认后执行；
> 执行前必须先备份、核对实例名/端口/路径，并在维护窗口内操作。本技能脚本不会自动执行卸载命令。

回滚到旧版本（二进制与数据快照都在的前提下）：

```bash
systemctl stop redis
# 用旧前缀启动同一份旧配置（或恢复旧配置备份）
/opt/redis-OLD_VERSION/bin/redis-server /etc/redis/redis.conf
# 恢复数据目录快照后再启动
```

卸载（保留数据，由人工执行）：

```bash
systemctl disable --now redis
systemctl daemon-reload
```

随后由运维人员按发行版规范删除以下文件与目录（本技能不提供删除命令，避免误删）：

- systemd 单元：`/etc/systemd/system/redis.service`
- 配置文件：`/etc/redis/redis.conf`
- 安装前缀：`/opt/redis-7.0.15`
- 数据目录 `/var/lib/redis/redis` 默认保留，确认备份完成后再处理

卸载（连同数据，不可恢复）：

- 先停止服务并完成备份，二次确认实例名、端口与路径。
- 按发行版规范删除数据目录 `/var/lib/redis/redis`、日志 `/var/log/redis/redis.log` 与部署产物 `/var/log/redis-deploy`。

多实例逐个执行，单元名与目录名替换为对应实例名。脚本不提供自动卸载入口，避免误删数据。

## 8. 服务管理与验收命令

```bash
systemctl status redis --no-pager
systemctl restart redis
journalctl -u redis -n 100 --no-pager

/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 ping
/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 INFO server \
  | grep -E 'redis_version|tcp_port|config_file|executable'
ss -lntp | grep ':6379'

# 有密码时用环境变量，避免密码进入 argv/history
export REDISCLI_AUTH="$(cat /root/.redis-pass)"   # 文件需 chmod 600
/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 ping
unset REDISCLI_AUTH
```

验收标准：

- `PING` 返回 `PONG`
- `INFO server` 的 `redis_version` 等于目标版本
- 端口处于 LISTEN
- `systemctl is-active redis` 为 `active`
- `deploy.env` 中 `DEPLOY_STATUS=ok`、`REDIS_VERIFY=ok`

## 9. 配置基线说明

`assets/redis.conf.template` 的默认值偏保守，适合作为全新环境基线：

| 参数 | 默认 | 说明 |
|------|------|------|
| `bind` | 127.0.0.1 | 默认只允许本机访问 |
| `protected-mode` | yes | 未认证且非回环时拒绝外部连接 |
| `daemonize` / `supervised` | no / no | 由 systemd 以 simple 类型托管，避免依赖 libsystemd |
| `tcp-backlog` | 511 | 需 `net.core.somaxconn >= 511` |
| `save` | 3600/300/60 三档 | 默认 RDB 快照策略 |
| `appendonly` | no | 默认不开启 AOF；需要时由业务确认后调整 |
| `maxmemory-policy` | noeviction | 避免缓存场景静默淘汰；内存上限由业务规划 |
| `loglevel` | notice | 日志写 `/var/log/redis/<instance>.log` |

修改方式：

- 部署期临时改：直接改模板不符合“可复现”要求，建议部署后按 `redis-optimize` 的单变量流程改配置并复测。
- 重跑部署会覆盖并备份配置；长期自定义参数应放入独立 include 文件并在模板之外维护。
- `--keep-conf` 仅用于“完全接管配置”的场景，此时 requirepass 不会由脚本写入。

## 10. 系统参数建议（需人工确认，脚本不自动修改）

| 项 | 建议 | 检查 |
|----|------|------|
| `vm.overcommit_memory` | 1 | `cat /proc/sys/vm/overcommit_memory` |
| THP | never 或 madvise | `cat /sys/kernel/mm/transparent_hugepage/enabled` |
| `net.core.somaxconn` | >= 511（建议 1024） | `cat /proc/sys/net/core/somaxconn` |
| nofile | 1000000（systemd 单元已设置） | `systemctl show redis -p LimitNOFILE` |

这些是主机级变更，可能与同机其它业务冲突，必须先确认再改；持久化方式（sysctl.d、内核启动参数）
参考目标发行版文档。Redis 启动日志中的相关 WARN 属于提示，不代表部署失败。

## 11. 故障排查

| 症状 | 处置 |
|------|------|
| `make` 在 jemalloc 阶段失败 | 确认 gcc 完整；可改用 `make MALLOC=libc` 后重试（需手工执行），或检查磁盘空间 |
| 下载失败 / 证书错误 | 检查 `--mirror`、代理、时间同步与 CA 证书；离线改用 `--tarball` |
| sha256 不匹配 | 源码包损坏或被篡改；重新下载，不要带 `--allow-unverified` 绕过 |
| 端口已被监听 | `ss -lntp "sport = :6379"` 找到进程；先停旧实例，或换 `--port` |
| 服务启动即退出 | `journalctl -u redis -n 100`；常见为配置语法、目录权限、端口占用、`requirepass` 文件不可读 |
| `PING` 超时 | 确认 `bind` 与探测地址一致；容器/防火墙放行；检查 `protected-mode` 与密码 |
| `requirepass` 不生效 | `--keep-conf` 下脚本不写密码；检查配置文件权限与重启是否成功 |
| 客户端报 `NOAUTH` | 使用 `REDISCLI_AUTH` 或客户端密码配置，禁止用 `-a` 暴露密码 |
| 启动日志提示 THP/overcommit | 按第 10 节确认后调整；不影响启动 |
| systemd 不可用 | 使用 `--no-systemd`，前台运行 `redis-server <conf>` 并由上层进程管理 |
| SELinux 阻止监听 | 先用 `audit2allow` 确认，再按发行版策略放行端口（如 `semanage port -a -t redis_port_t -p tcp <port>`） |
| 自定义数据/日志目录在 SELinux 下不可写 | 按发行版策略给目录打标签（如 `semanage fcontext -a -t redis_var_lib_t "<dir>(/.*)?"` 后 `restorecon -Rv <dir>`）；无可用策略时改用默认目录或联系系统管理员 |
| 升级后旧版本无法读数据 | Redis 7 RDB 格式不向下兼容；回滚必须使用升级前的数据快照 |

## 12. 安全说明

- 默认 `bind 127.0.0.1` + `protected-mode yes`；开放远程访问必须显式配置。
- 非回环 bind 且未提供密码时脚本直接失败；`--allow-insecure-remote` 是显式降级，不允许在未告知用户时使用。
- 密码只从 `--password-file` 读取：不进入 argv、不写日志、不写 `deploy.env`；配置文件含明文
  `requirepass`，因此权限固定为 `0640 root:<运行组>`，目录 `/etc/redis` 为 root 所有。
- 客户端认证使用 `REDISCLI_AUTH` 环境变量或客户端自身配置；禁止 `redis-cli -a <值>`。
- 下载包必须校验 sha256；脚本不会执行远程脚本，也不会关闭 TLS 校验。
- 重复部署只备份、不删除旧配置/单元/数据；卸载需人工确认后执行。
