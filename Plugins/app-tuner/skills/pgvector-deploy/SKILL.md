---
name: pgvector-deploy
description: pgvector 环境安装部署技能（Linux，aarch64/x86_64 通用）。当需要安装、部署、搭建 pgvector 向量检索环境，为 PostgreSQL 安装或升级 vector 扩展，处理 PostgreSQL 初始化/启动、离线源码构建、本地或远程数据库连接，并验证向量检索时使用。未指定 pgvector 版本时默认安装 master 分支并记录实际 commit；不负责向量检索性能调优。
license: MulanPSL-2.0
compatibility: Linux（openEuler/RHEL/CentOS/Debian 系），aarch64 或 x86_64，root 权限，PostgreSQL 服务端与 devel 包、gcc/make/git；离线部署需准备 pgvector 源码目录。
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: vector_database
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# pgvector 环境安装部署

在 Linux 主机上完成 PostgreSQL 检查/安装、pgvector 源码构建与扩展安装，并在目标数据库创建/升级
`vector` 扩展、执行向量检索冒烟验证，产出可复用的 `deploy.env`。

## 版本策略（强约束）

| 用户输入 | 行为 |
|----------|------|
| 未指定版本 | **安装 pgvector master 分支**（默认值），并记录实际 commit |
| 指定 tag（如 v0.8.0） | 按 tag 构建，记录 commit 与扩展版本 |
| 指定 commit SHA/分支 | 按指定 ref checkout 构建 |
| 使用 `--source-dir` | 使用目录自带版本，不得再指定非 master 的 `--version` |
| 只说要“最新版” | 说明默认 master 是移动分支；确认后按 master 执行并记录 commit |

master 会随上游变化，因此 `deploy.env` 必须记录 `PGVECTOR_COMMIT` 与
`PGVECTOR_EXTENSION_VERSION`；禁止只写“master”而不记录 commit。

## 何时触发

- 需要安装、部署、搭建 pgvector 向量检索环境
- 需要在已有 PostgreSQL 上安装/升级 `vector` 扩展
- PostgreSQL 未安装，需要一并安装发行版 server/devel 包并初始化/启动
- 需要离线源码构建 pgvector，或在鲲鹏 aarch64 / x86_64 上从源码编译
- 需要验证 `vector` 扩展可创建、可做最近邻检索

不适用：向量索引/查询性能调优、HNSW/IVFFlat 参数调优、Embedding 模型部署；
远程数据库主机的分布式部署编排（扩展文件必须安装在运行 PostgreSQL 的主机上）。

## 工作流总览

```text
前置检查（只读） → 依赖安装 → PostgreSQL 安装/初始化/启动
  → 获取源码（git master/tag 或离线目录） → 私有构建目录编译安装
  → 校验 vector.control / vector.so → 建库/创建或升级扩展
  → 向量检索冒烟验证 → deploy.env
```

原则：每步失败保留日志与现场；不自动删除旧数据/旧扩展；升级前先备份数据库。

## 快速开始

在 skill 根目录执行：

```bash
# 1) 只读前置检查
bash scripts/preflight_check.sh

# 2) 默认部署：pgvector master + 本机 PostgreSQL（缺失则安装发行版默认版本）
bash scripts/install_pgvector.sh

# 3) 指定 pgvector tag
bash scripts/install_pgvector.sh --version v0.8.0

# 4) 已有 PostgreSQL，只安装/升级扩展
bash scripts/install_pgvector.sh --skip-postgres-setup --pg-config /usr/bin/pg_config

# 5) 离线部署
bash scripts/install_pgvector.sh --source-dir /root/pgvector

# 6) 只打印计划
bash scripts/install_pgvector.sh --dry-run
```

## 默认布局

| 项 | 默认值 |
|----|--------|
| pgvector 版本 | `master`（记录实际 commit） |
| PostgreSQL | 发行版默认 server/devel 包；已有实例则复用 |
| 构建缓存 | `/var/cache/pgvector-deploy`（root:root 0700，工作目录 `mktemp -d`） |
| 扩展安装位置 | `pg_config --sharedir` / `pg_config --pkglibdir` |
| 目标数据库 | `pgvector`（不存在则创建） |
| 数据库角色 | `postgres`（需超级用户） |
| 本机认证 | peer（`runuser -u postgres -- psql`） |
| 结果目录 | `/var/log/pgvector-deploy/<run_id>` |

## 参数

```text
--version VER            pgvector 版本/ref，默认 master
--git-url URL            源码仓库，默认 https://github.com/pgvector/pgvector.git
--source-dir DIR         离线源码目录（要求 root 属主、组/其他不可写）
--pg-config PATH         指定 pg_config（默认自动查找）

--db-name NAME           目标数据库，默认 pgvector
--db-user USER           数据库角色，默认 postgres（需超级用户）
--db-os-user USER        本机 peer 认证 OS 用户，默认 postgres
--db-host HOST           远程数据库主机（默认本机 socket）
--db-port PORT           数据库端口，默认 5432
--db-password-file FILE  远程数据库密码文件（要求 600，经 PGPASSWORD 传入）
--no-create-database     数据库不存在时失败，不自动创建

--build-root DIR         默认 /var/cache/pgvector-deploy（要求 root:root 0700）
--result-dir DIR         默认 /var/log/pgvector-deploy/<run_id>
--skip-deps / --skip-postgres-setup / --dry-run
```

## 安全基线

- `master` 是移动分支：默认安装 master，但必须记录 `PGVECTOR_COMMIT`；升级前备份数据库。
- 构建根目录必须 `root:root 0700`，父目录不得属于非 root 用户或非 sticky 的组/其他可写目录；
  工作目录用 `mktemp -d` 创建。
- 路径参数必须是绝对路径，拒绝 `.`、`..`、`//` 段；有 `realpath` 时归一化符号链接，
  归一化结果命中系统目录黑名单（`/`、`/etc`、`/var` 等）直接失败。
- `--source-dir` 要求 root 属主、组/其他不可写；复制到私有构建目录后再次校验属主与写权限。
- 数据库密码只从 `--db-password-file`（0600）读取，经 `PGPASSWORD` 环境变量传给 psql；
  不进入 argv、不写日志、不写 `deploy.env`。psql 使用 `-w`，不会交互提示密码。
- `CREATE EXTENSION vector` 需要数据库超级用户；脚本会先检查 `rolsuper`，不满足直接失败。
- 不自动修改内核参数；不自动删除扩展或数据库；卸载/回退需人工确认并先备份。

## PostgreSQL 行为

- 未找到 `pg_config` 时，`install_deps.sh` 安装发行版默认的 PostgreSQL server/devel 包。
- `setup_postgres.sh` 在 RHEL/openEuler 上使用 `postgresql-setup --initdb` + systemd；
  在 Debian/Ubuntu 上使用 `pg_lsclusters` / `pg_ctlcluster`。
- 已有 PostgreSQL 时使用 `--skip-postgres-setup`，并用 `--pg-config` 指定与运行实例匹配的版本；
  PGDG 多版本、自定义数据目录或容器环境请先自行初始化/启动，再走该模式。
- `pg_config` 的主版本必须与运行实例一致，否则扩展会安装到错误目录，脚本直接失败。
- 远程数据库：本技能在运行 `pg_config` 的主机上编译，扩展文件必须位于数据库主机；
  远程连接仅用于执行 SQL，不能代替在数据库主机上安装扩展。

## 产物与验收

`deploy.env` 关键字段：

```text
DEPLOY_STATUS=ok
PGVECTOR_VERSION=master
PGVECTOR_COMMIT=<实际 commit>
PGVECTOR_EXTENSION_VERSION=<扩展版本>
PGVECTOR_CONTROL=<sharedir>/extension/vector.control
PGVECTOR_LIB=<pkglibdir>/vector.so
PG_CONFIG=/usr/bin/pg_config
PG_VERSION=<运行实例版本>
PG_DATABASE=pgvector
PG_DB_USER=postgres
PG_HOST=local
PG_PORT=5432
PGVECTOR_VERIFY=ok
```

验收命令：

```bash
psql -d pgvector -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
psql -d pgvector -c "CREATE TEMP TABLE t(id int, e vector(3));
INSERT INTO t VALUES (1,'[1,2,3]'),(2,'[4,5,6]');
SELECT id FROM t ORDER BY e <-> '[1,2,3]' LIMIT 1;"
```

通过标准：`vector` 扩展版本非空；最近邻查询返回 `1`；`deploy.env` 中
`DEPLOY_STATUS=ok`、`PGVECTOR_VERIFY=ok`。

## 重复执行与升级

- 重跑会重新获取源码、编译并 `make install`，然后 `CREATE EXTENSION IF NOT EXISTS vector`；
  已有扩展版本低于新默认版本时执行 `ALTER EXTENSION vector UPDATE`。
- 升级前必须备份目标数据库；扩展升级失败时保留日志与构建目录，由人工恢复。
- 回退：pgvector 不保证降级 update path；应使用升级前数据库备份恢复，或在新库中重建。
- 构建目录保留在 `/var/cache/pgvector-deploy`，失败时日志打印路径；清理需人工确认。

## 常见问题速查

| 症状 | 处置 |
|------|------|
| 未指定版本 | 用默认 master，并在 deploy.env 记录 commit |
| 找不到 pg_config | 安装 PostgreSQL server/devel，或用 `--pg-config` 指定 |
| 缺少 postgres.h | 安装与运行实例匹配的 devel 包，或换 `--pg-config` |
| pg_config 与实例版本不一致 | 用 `--pg-config` 指向运行实例对应版本 |
| PostgreSQL 未运行 | 去掉 `--skip-postgres-setup` 让脚本初始化/启动，或手工启动 |
| 非超级用户 | 用超级用户（默认 postgres）执行 `CREATE EXTENSION` |
| 远程 DB 报 extension control file 不存在 | 在数据库主机上执行本技能；远程 psql 不能替代本地扩展安装 |
| 密码认证失败 | 用 `--db-password-file`（600）或配置 `.pgpass`；脚本不会交互提示 |
| 构建根目录权限/属主被拒绝 | 用默认 `/var/cache/pgvector-deploy`，或修正为 root:root 0700 |
| 源码目录被拒绝 | 使用 root 属主且组/其他不可写的目录；不要从用户可写目录 root 构建 |

## 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight_check.sh` | 部署前只读检查：权限、OS、工具、PostgreSQL、源码/网络、构建目录、密码文件 |
| `scripts/install_deps.sh` | 安装 gcc/make/git/tar/findutils/coreutils 与 PostgreSQL server/devel；支持 `--no-postgres` |
| `scripts/setup_postgres.sh` | 按发行版初始化并启动 PostgreSQL；支持 `--check-only` |
| `scripts/install_pgvector.sh` | 主流程：源码获取、私有构建、安装、extension 创建/升级、冒烟验证、deploy.env |
| `scripts/lib/common.sh` | 日志、root 检查、pg_config 解析等公共函数 |

## 参考资料

- `references/pgvector-install-guide.md` — PostgreSQL 安装矩阵、pg_config 选择、pgvector 构建/升级、
  远程数据库限制、安全说明与故障排查。
- `assets/smoke_test.sql` — 手动向量检索冒烟 SQL。
- 上游项目：https://github.com/pgvector/pgvector

## 输出契约

- 成功：`deploy.env` 中 `DEPLOY_STATUS=ok`，并给出扩展版本、commit、pg_config、数据库与验收结果。
- 失败：返回失败阶段、日志路径、已生成产物与缺失项；等待人工处理，不自动删除现场。
- 任何变更类操作：说明改前/改后、备份路径与回滚方式；不得输出数据库密码。

## 错误处理

依赖安装失败、PostgreSQL 启动失败、pg_config 与实例不匹配、非超级用户、编译失败或扩展验证失败时
立即停止后续阶段，保留 `logs/`、`deploy.env`（`DEPLOY_STATUS=failed`）与构建目录，修复后重跑。
无法满足安全基线（路径非法、构建目录不安全、源码目录非 root）时返回原因并等待用户决策。
