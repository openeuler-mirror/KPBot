---
name: redis-deploy
description: Redis 环境部署技能（Linux，aarch64/x86_64 通用）。当需要安装、部署、搭建、重装、升级或卸载 Redis 环境（单机或同机多实例），需要生成 redis.conf 与 systemd 服务、配置数据与日志目录，或在离线、内网环境用源码包部署 Redis 时使用。未指定版本时默认安装 Redis 7.0.15（源码构建并做 sha256 校验）；只负责环境部署，不负责性能调优（调优用 redis-optimize）。
license: MulanPSL-2.0
compatibility: Linux（openEuler 24.03 已验证，兼容 RHEL/CentOS/Debian 系），aarch64 或 x86_64，root 权限，gcc/make/tar 与 curl 或 wget；离线部署需自备 redis-{version}.tar.gz。
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: redis
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# Redis 环境部署

在 Linux 主机上完成 Redis 的源码构建、安装、配置与 systemd 托管，支持在线/离线、
单机/同机多实例、升级与回滚，并产出可被后续测试或调优 skill 复用的 `deploy.env`。

## 版本策略（强约束）

| 用户输入 | 行为 |
|----------|------|
| 未指定版本 | **安装 Redis 7.0.15**（默认值，内置官方 sha256） |
| 指定版本 | 按指定版本部署；`assets/checksums.txt` 未收录时必须提供 `--sha256` |
| 只说要“最新版” | 先与用户确认具体版本号，确认前不下载、不安装 |
| 要求用发行版仓库的 redis 包 | 说明本技能用源码构建保证版本精确；若坚持仓库包，按用户明确版本执行，但不得声称版本为 7.0.15 |

禁止把“未指定版本”解释为发行版仓库默认版本；禁止跳过 sha256 校验（除非用户明确接受
`--allow-unverified` 并记录告警）。

## 何时触发

- 需要安装、部署、搭建、重装 Redis 环境，或目标机上还没有可用的 redis-server
- 需要生成 `redis.conf`、数据/日志目录与 systemd 服务
- 需要离线/内网源码包部署，或指定 Redis 版本（默认 7.0.15）
- 需要同机多实例、升级、回滚或卸载 Redis 环境
- 需要一份可复验的部署产物（`deploy.env`）供测试/调优环节消费

不适用：Redis 性能调优（绑核、网卡多队列、ntuple、参数调优）→ skill `redis-optimize`；
Redis Cluster / Sentinel 拓扑编排 → 另行规划，本技能只部署单机或同机多实例。

## 工作流总览

```text
前置检查（只读） → 依赖安装 → 下载/复用源码包 + sha256 校验 → 编译安装
  → 用户与目录 → 渲染 redis.conf / systemd 单元 → 启动 → PING/INFO 验收 → deploy.env
```

原则：每一步失败都保留日志与现场，不删除旧配置、旧单元、旧数据；重复执行先备份再覆盖。

## 快速开始

在 skill 根目录执行：

```bash
# 1) 只读前置检查
bash scripts/preflight_check.sh --version 7.0.15 --port 6379

# 2) 默认部署：Redis 7.0.15、127.0.0.1:6379、systemd 托管
bash scripts/install_redis.sh

# 3) 指定版本（校验值需自行核对官方发布）
bash scripts/install_redis.sh --version 7.2.4 --sha256 <64位sha256>

# 4) 离线部署
bash scripts/install_redis.sh --tarball /root/redis-7.0.15.tar.gz

# 5) 同机多实例
bash scripts/install_redis.sh --instance-name redis6380 --port 6380

# 6) 只打印计划
bash scripts/install_redis.sh --dry-run
```

## 默认布局

| 项 | 默认值 |
|----|--------|
| 版本 | 7.0.15 |
| 安装前缀 | `/opt/redis-7.0.15` |
| 实例 / 端口 | `redis` / 6379 |
| bind | `127.0.0.1`（仅本机） |
| 配置 | `/etc/redis/redis.conf`（0640 root:redis） |
| 数据 | `/var/lib/redis/redis` |
| 日志 | `/var/log/redis/redis.log` |
| 服务 | `redis.service`（Type=simple，LimitNOFILE=1000000） |
| 产物 | `/var/log/redis-deploy/<run_id>/env/deploy.env` |

多实例以实例名隔离：`--instance-name redis6380 --port 6380` 对应
`redis6380.service`、`/etc/redis/redis6380.conf`、`/var/lib/redis/redis6380`。

## 参数

```text
--version VER            默认 7.0.15
--instance-name NAME     默认 redis
--port PORT              默认 6379
--bind ADDR              默认 127.0.0.1，可空格分隔多地址
--prefix DIR             默认 /opt/redis-<version>
--conf-dir / --data-root / --log-dir / --run-dir   目录覆盖
--run-as USER            默认 redis（不存在则创建系统用户）
--password-file FILE     requirepass 来源，要求权限 600
--allow-insecure-remote  非回环 bind 且无密码时显式降级（不推荐）
--mirror URL             默认 https://download.redis.io/releases
--tarball FILE           离线源码包
--sha256 HEX             非内置版本的校验值
--allow-unverified       跳过校验（不推荐，记录告警）
--build-root DIR         默认 /var/tmp/redis-build
--result-dir DIR         默认 /var/log/redis-deploy/<run_id>
--build-tls              构建 TLS 支持（需 OpenSSL 开发包）
--skip-deps / --no-systemd / --keep-conf / --force / --dry-run
```

## 安全基线

- 默认 `bind 127.0.0.1` + `protected-mode yes`；非回环 bind 且未提供密码时脚本直接失败，
  仅允许用户明确要求后使用 `--allow-insecure-remote`。
- 密码只从 `--password-file` 读取：不进入 argv、不写日志、不写 `deploy.env`；配置文件含
  `requirepass`，权限固定 `0640 root:<运行组>`。
- 客户端验证使用 `REDISCLI_AUTH` 环境变量；禁止 `redis-cli -a <值>` 把密码放进命令行。
- 下载包必须做 sha256 校验；7.0.15 内置校验值；不把远程下载内容直接交给 shell 执行，不关闭 TLS 校验。
- 不自动修改内核参数（overcommit、THP、somaxconn）；只检查并提示，改动需用户确认。
- 不删除旧配置、旧单元、旧数据；卸载与清数据必须人工确认后执行。

## 产物与验收

`deploy.env` 关键字段：

```text
DEPLOY_STATUS=ok
REDIS_VERSION=7.0.15
REDIS_PREFIX=/opt/redis-7.0.15
REDIS_SERVER_BIN=/opt/redis-7.0.15/bin/redis-server
REDIS_CLI_BIN=/opt/redis-7.0.15/bin/redis-cli
REDIS_CONF=/etc/redis/redis.conf
REDIS_PORT=6379
REDIS_DATA_DIR=/var/lib/redis/redis
REDIS_SERVICE=redis.service
REDIS_VERIFY=ok
REDIS_PASSWORD_SET=0|1
```

验收命令：

```bash
/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 ping
/opt/redis-7.0.15/bin/redis-cli -h 127.0.0.1 -p 6379 INFO server | grep redis_version
systemctl is-active redis
ss -lntp | grep ':6379'
```

通过标准：`PING` 返回 `PONG`；`redis_version` 等于目标版本；端口 LISTEN；服务 active；
`deploy.env` 中 `DEPLOY_STATUS=ok`。

## 重复执行、升级与回滚

- 同版本重跑：复用已安装二进制，备份并重写配置/单元，重启服务；`--force` 强制重编译。
- 不同版本重跑（同实例名）：切换到新前缀并重启；默认数据目录不变，升级前先备份配置与数据。
- 配置/单元覆盖前都会生成 `.bak.<时间戳>`，`deploy.env` 旧文件重命名为 `.previous.<run_id>`。
- 回滚：停服 → 用旧版本前缀与旧配置启动 → 恢复升级前数据快照；Redis 7 的 RDB 格式不向下兼容，
  没有数据快照不得声称可回滚。
- 卸载步骤见 `references/redis-install-guide.md` §7；脚本不提供自动卸载入口，避免误删数据。

## 常见问题速查

| 症状 | 处置 |
|------|------|
| 未指定版本 | 直接用默认 7.0.15，不要改成仓库版本 |
| sha256 不匹配 | 重新获取源码包；不要用 `--allow-unverified` 掩盖 |
| 端口占用 | `ss -lntp` 定位；停旧实例或换 `--port` |
| 服务启动失败 | `journalctl -u <instance> -n 100`；查配置、目录权限、端口、密码文件 |
| PING 无响应 | 核对 bind 与探测地址、防火墙、`protected-mode`、密码 |
| jemalloc 编译失败 | 检查 gcc 与磁盘；可手工 `make MALLOC=libc` 后复测 |
| 无 systemd | 用 `--no-systemd` 落盘，前台运行 `redis-server <conf>` |
| 需要远程访问 | 提供 `--password-file` 并明确 bind；否则不要开放 |
| 启动日志 THP/overcommit 告警 | 按手册第 10 节确认后调整，不影响本次部署判定 |

## 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight_check.sh` | 部署前只读检查：权限、OS/架构、资源、工具、端口、既有安装、下载源、运行参数、源码包校验；有 FAIL 时退出码非零 |
| `scripts/install_deps.sh` | 安装 gcc/make/tar/下载工具与 CA（可选 OpenSSL 开发包）；幂等，支持 `--dry-run` |
| `scripts/install_redis.sh` | 主部署流程：下载/校验、构建、用户/目录、配置、systemd、启动验收、写 `deploy.env` |
| `scripts/lib/common.sh` | 日志、root 检查、sha256 等公共函数 |

## 参考资料

- `references/redis-install-guide.md` — 环境矩阵、版本/校验策略、标准部署、多实例、离线、升级回滚、
  卸载、验收命令、配置基线、系统参数建议、故障排查与安全说明。
- `assets/redis.conf.template` — 基线配置模板（部署时渲染）。
- `assets/redis.service.template` — systemd 单元模板。
- `assets/checksums.txt` — 内置版本 sha256 清单（默认 7.0.15）。

## 输出契约

- 成功：`deploy.env` 中 `DEPLOY_STATUS=ok`，并给出配置路径、数据目录、服务名、验收结果。
- 失败：返回失败阶段、日志路径、已生成产物与缺失项；等待人工处理，不自动删除现场。
- 任何变更类操作：说明改前/改后、备份路径与回滚方式；不得输出密码。

## 错误处理

安装依赖失败、下载失败、校验失败、编译失败、端口冲突或服务启动失败时立即停止后续阶段，
保留 `logs/`、`deploy.env`（`DEPLOY_STATUS=failed`）与已有安装/配置，修复后重跑即可继续。
无法满足安全基线（非回环 bind 无密码、校验值缺失且未显式降级）时返回原因并等待用户决策。
