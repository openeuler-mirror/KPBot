# pgvector 部署执行手册

> 本文件是 `pgvector-deploy` SKILL.md 的执行细节层：SKILL.md 负责触发、版本策略与总体流程，
> 此处给出 PostgreSQL 安装矩阵、pg_config 选择、pgvector 构建/扩展升级、远程数据库限制、
> 安全说明与故障排查。性能调优不在本手册范围。

## 1. 支持范围与前置条件

| 项 | 要求 |
|----|------|
| 操作系统 | openEuler 24.03、RHEL/CentOS/Rocky/Alma 8+、Debian 11+/Ubuntu 20.04+ |
| CPU | aarch64（鲲鹏）或 x86_64 |
| 权限 | root（安装包、`make install`、启动 PostgreSQL） |
| PostgreSQL | 13+（pgvector master 的常见支持范围；发行版默认版本过旧时请升级） |
| 构建 | gcc、make、git、tar；脚本可自动安装 |
| PostgreSQL 开发包 | `pg_config` 与服务端头文件 `postgres.h`（RHEL: `postgresql-devel`；Debian: `postgresql-server-dev-all`） |
| 数据库权限 | 连接角色需超级用户（默认 postgres） |
| 磁盘 | 构建缓存至少 1 GiB 余量 |

默认路径布局：

```text
/var/cache/pgvector-deploy/            # 构建缓存，root:root 0700
/var/log/pgvector-deploy/<run_id>/     # deploy.env 与阶段日志
$(pg_config --sharedir)/extension/     # vector.control、vector--*.sql
$(pg_config --pkglibdir)/vector.so     # 扩展动态库
```

## 2. 版本策略

- **未指定版本时使用 `master`**，不自动选择某个 release tag。
- `master` 是移动分支：部署完成后必须把 `git rev-parse HEAD` 写入 `deploy.env` 的
  `PGVECTOR_COMMIT`，扩展版本写入 `PGVECTOR_EXTENSION_VERSION`。
- 指定 tag（如 `v0.8.0`）或 commit 时按 ref checkout；tag 与 commit 更适合可复现环境。
- 离线部署用 `--source-dir`，使用目录当前内容；目录自带版本，不能再指定非 master 的 `--version`。
- 升级前先备份数据库；pgvector 不保证任意版本之间的降级路径。

## 3. PostgreSQL 安装与启动

### 3.1 安装包与初始化

脚本按发行版自动处理：

| 发行版 | 包 | 初始化/启动 |
|--------|----|-------------|
| openEuler / RHEL / CentOS | `postgresql-server`、`postgresql-devel` | `postgresql-setup --initdb` + `systemctl enable --now postgresql` |
| Debian / Ubuntu | `postgresql`、`postgresql-server-dev-all` | 包 postinst 自动建集群；必要时 `pg_lsclusters` / `pg_ctlcluster` |
| SUSE | `postgresql-server`、`postgresql-devel` | systemd 服务 |

- 已有 PostgreSQL 时使用 `--skip-postgres-setup`，脚本只检查客户端与头文件，不安装/重启数据库。
- PGDG 多版本、自定义数据目录或容器/无 systemd 环境：先由 DBA 初始化并启动实例，
  再用 `--skip-postgres-setup` + `--pg-config` 指定匹配版本；不要让脚本猜测数据目录。
- `setup_postgres.sh --check-only` 可以单独检查实例是否在指定端口运行。
- 默认端口 5432；多集群或自定义端口用 `--db-port`。

### 3.2 pg_config 选择

`pg_config` 决定扩展安装目录，必须与实际运行的 PostgreSQL 主版本一致：

```bash
pg_config --version
pg_config --bindir
pg_config --sharedir
pg_config --pkglibdir
pg_config --includedir-server
```

- 系统有多个 PostgreSQL（如 PGDG 多版本并存）时，用 `--pg-config` 指定与实例匹配的路径。
- 脚本会比较 `pg_config` 主版本与运行实例 `SHOW server_version`；不一致直接失败。
- 缺少 `postgres.h` 表示只装了客户端/`libpq`，需要安装匹配版本的 server dev 包。

### 3.3 数据库连接

- 本机默认使用 Unix socket + peer 认证：`runuser -u postgres -- psql`。
- 远程数据库用 `--db-host` / `--db-port` / `--db-user`；密码通过 `--db-password-file`（0600）
  注入 `PGPASSWORD`，psql 使用 `-w`，不会交互提示。
- 远程连接只用于执行 SQL；扩展文件必须安装在运行 PostgreSQL 的数据库主机上。
  在 A 机编译、连接 B 机数据库时，B 机不会自动获得 `vector.control` / `vector.so`。

## 4. 源码获取与构建

在线（默认）：

```bash
bash scripts/install_pgvector.sh --version master
```

- `git clone` 到 root 私有构建目录，checkout 指定 ref。
- `--git-url` 仅支持 http(s)://；本地目录请用 `--source-dir`，不要用 file:// 等本地 URL 绕过属主/写权限校验。
- 构建目录默认 `/var/cache/pgvector-deploy`，要求 root:root 0700；
  每次用 `mktemp -d` 创建独立工作目录，父目录属主与 sticky 位会被校验。
- 构建前检查源码树不存在非 root 属主或组/其他可写的文件/目录。

离线：

```bash
bash scripts/install_pgvector.sh --source-dir /root/pgvector
```

- 源码目录必须属于 root 且组/其他不可写；复制到私有构建目录后再次校验。
- 目录需包含 `Makefile` 与 `vector.control`。

编译安装：

```bash
make -C <src> -j$(nproc) PG_CONFIG=<pg_config>
make -C <src> install PG_CONFIG=<pg_config>
```

安装完成后校验：

```bash
test -f "$(pg_config --sharedir)/extension/vector.control"
test -f "$(pg_config --pkglibdir)/vector.so"
ls "$(pg_config --sharedir)/extension"/vector--*.sql
```

## 5. 扩展创建、升级与验证

目标数据库默认 `pgvector`，不存在时创建（`--no-create-database` 可禁止）：

```bash
psql -d pgvector -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -d pgvector -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

已有扩展版本低于新默认版本时，脚本执行 `ALTER EXTENSION vector UPDATE`。扩展 SQL 脚本：

```sql
CREATE TEMP TABLE pgvector_smoke (id int, embedding vector(3));
INSERT INTO pgvector_smoke VALUES (1, '[1,2,3]'), (2, '[4,5,6]');
SELECT id FROM pgvector_smoke ORDER BY embedding <-> '[1,2,3]' LIMIT 1;
```

期望返回 `1`（最近邻）。`vector(3)` 与 `<->` 只有在扩展开启后可用。

## 6. 升级与回滚

升级：

1. 备份目标数据库（逻辑备份或快照），确认可恢复。
2. 使用新 tag 或新 commit 重跑 `install_pgvector.sh`；脚本重新编译/安装并更新扩展。
3. 验收 `extversion`、最近邻查询与业务查询。

回滚：

- pgvector 不保证降级 update path。回退优先使用升级前的数据库备份恢复。
- 只回退二进制文件而不回退数据库 schema 可能导致版本不匹配；不要在没有备份时操作生产库。
- 构建目录保留供排查，清理由人工确认后执行。

## 7. 卸载与清理

> **危险操作警告**：卸载扩展或删除数据库/数据目录会永久丢失数据，必须由 DBA 确认并先备份。
> 本技能脚本不提供自动卸载入口，也不会自动删除数据库、扩展文件或构建目录。

- 卸载扩展：由 DBA 在目标数据库执行删除 `vector` 扩展的 SQL，并确认没有业务依赖。
- 删除扩展文件：确认没有其它数据库使用后，由运维按发行版规范处理
  `pg_config --sharedir` 下的 `extension/vector*` 与 `pg_config --pkglibdir` 下的 `vector.so`。
- 删除构建缓存与日志目录前先确认没有正在运行的部署；路径为
  `/var/cache/pgvector-deploy` 与 `/var/log/pgvector-deploy`。

## 8. 故障排查

| 症状 | 处置 |
|------|------|
| `pg_config: command not found` | 安装 PostgreSQL server/devel；或 `--pg-config` 指定路径 |
| `postgres.h: No such file` | 安装与实例匹配的 server dev 包 |
| `pg_config` 与实例版本不一致 | `--pg-config` 指向运行实例对应版本 |
| PostgreSQL 未启动 | 去掉 `--skip-postgres-setup` 让脚本启动，或手工启动后重试 |
| 无法以 postgres 连接 | 检查 `pg_hba.conf`、端口、`--db-os-user`；本机默认 peer 认证 |
| `permission denied to create extension` | 使用超级用户（默认 postgres） |
| `could not open extension control file` | 扩展未安装到运行实例的 sharedir；检查 pg_config 是否匹配数据库主机 |
| 远程数据库报 control file 不存在 | 在数据库主机上执行本技能；远程 psql 不能替代安装扩展 |
| 密码认证失败 | 用 `--db-password-file`（600）或配置 `.pgpass`；脚本不会交互提示 |
| 编译失败 | 查看 `logs/build.log`；确认 gcc/make、PostgreSQL 头文件与磁盘空间 |
| 扩展版本未更新 | 检查是否执行了 `ALTER EXTENSION vector UPDATE`；查看 `pg_available_extensions` |
| 构建根目录权限被拒绝 | 修正为 root:root 0700，或改用 root 私有路径 |
| 源码目录被拒绝 | 使用 root 属主且组/其他不可写目录；不要从用户可写目录 root 构建 |
| `--git-url` 传入 file:// 被拒绝 | 本地源码改用 `--source-dir`（要求 root 属主且组/其他不可写）；`--git-url` 仅支持 http(s):// |

## 9. 安全说明

- 构建目录 root:root 0700，父目录必须是 root 属主；组/其他可写时必须带 sticky 位。
- 路径参数拒绝 `.`、`..`、`//`；`realpath -m` 归一化后再次检查系统目录黑名单。
- `--source-dir` 必须是 root 属主且组/其他不可写；复制后在私有目录再次校验。
- `--git-url` 仅接受 http(s)://；本地源码必须走 `--source-dir` 并经过 root 属主/写权限校验，
  禁止 file:// 等本地 URL 绕过校验。
- `make` 以 root 在私有目录执行；不要从用户可写目录构建。
- 数据库密码只从 0600 文件读取并经环境变量传递，不进入命令行、日志或 `deploy.env`。
- 不自动修改内核参数；不自动删除扩展、数据库或构建目录。
- 远程数据库场景必须确认扩展文件位于数据库主机，避免“本机安装成功、远程仍不可用”。
