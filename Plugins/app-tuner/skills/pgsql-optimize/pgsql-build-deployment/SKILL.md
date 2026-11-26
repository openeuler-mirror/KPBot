---
name: pgsql-build-deployment
description: >
  PostgreSQL 17/18 源码编译与部署：依赖预检、configure/make 安装布局（ARM aarch64/鲲鹏、
  LTO/LSE/outline-atomics 编译选项）、contrib 扩展开发与安装（Makefile 模型、自研观测扩展
  pg_buffer_stats 的部署与 shared_preload_libraries 重启语义）、补丁管理与回退
  （git apply --check / -R）、initdb/pg_ctl 生命周期、postgresql.conf 关键 GUC 模板、
  二进制备份与版本切换回滚；含只读 preflight_check.sh。触发词：PG 编译、PostgreSQL
  安装、contrib 扩展、make install、pg_ctl、initdb、打补丁、shared_preload_libraries、
  pg_buffer_stats 安装、版本回滚。
license: MulanPSL-2.0
compatibility: PostgreSQL 17.5 / 18.3（官方 tag REL_17_5 / REL_18_3）实测；aarch64/鲲鹏为主口径，x86_64 通用
metadata:
  skill_type: deployment
  category: guideline
  domain: relational_database
  activation_mode: manual
  side_effect_free: "false"
  version: "1.0.0"
---

# PostgreSQL 17/18 编译部署

> 定位：主动执行类 skill（编译、安装、部署、补丁）。所有路径经变量注入；破坏性操作
> （替换二进制、DROP/重建、initdb 覆盖目录）必须显式确认；本 skill 不做性能调参决策
> （→ pgsql-performance-optimization）与压测（→ pgsql-test-verification）。

## 何时使用

- 从源码编译 PostgreSQL（含优化构建：LTO/LSE）；
- 开发/安装 contrib 扩展或自研扩展（pg_buffer_stats 等观测扩展）；
- 给源码树打补丁（如 NUM_BUFFER_PARTITIONS 扩容补丁）并要可回退；
- 部署新实例（initdb）、管理启停（pg_ctl）、做版本切换与二进制回滚。

## 变量表（正文与脚本统一引用，全部可环境变量覆盖）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${PG_SRC}` | PG 源码树根 | `/opt/postgresql`（检出版） |
| `${PG_INSTALL}` | 安装前缀（--prefix） | `/opt/pgsql` |
| `${PGDATA}` | 数据目录 | `/opt/pgsql/data` |
| `${PG_PORT}` | 监听端口 | `5432` |
| `${PG_BIN}` / `${PG_CONFIG}` | bin 目录 / pg_config 路径 | `${PG_INSTALL}/bin` / `${PG_BIN}/pg_config` |
| `${PGUSER}` / `${PGHOST}` | 连接用户 / 主机 | `postgres` / `127.0.0.1`（本机部署口径） |
| `${BIN_BACKUP_DIR}` | 二进制备份目录 | `/opt/pgsql/binbak` |
| `${PATCH_DIR}` | 补丁存放目录（**必须在源码树外**，避免清理误删补丁/审计记录） | `${PG_INSTALL}/patches`（即 `/opt/pgsql/patches`，不在 `${PG_SRC}` 内） |

凭据只经 `PGPASSWORD` 环境变量或 `~/.pgpass` 注入，不写入脚本、不进命令行 argv。

## 一句话流程

```
preflight_check.sh（只读预检）
→ 源码编译安装（§1）→ contrib 扩展（§2）→ 补丁管理（§3）
→ initdb/启停（§4）→ GUC 模板（§5）→ 验收
```

## 核心纪律

1. **预检先行**：编译前跑 `scripts/preflight_check.sh`（只读），依赖/目录/端口
   不满足先补齐，不要 make 报错才回头查。
2. **补丁先 --check 后 apply，回退用 -R**：任何补丁先 `git apply --check`，失败
   不硬上；回退 `git apply -R` 后重编（与 nebula 家族同款纪律）。补丁原件与审计
   记录放源码树外（`${PATCH_DIR}` 默认 `${PG_INSTALL}/patches`）；重编只做
   `make clean`（仅清构建产物），**禁止 `git clean -xfd` 清整棵源码树**——会连带
   删除未跟踪的补丁/审计记录/本地文件、毁掉回退依据；破坏性清理须显式确认。
3. **观测扩展要重启**：`shared_preload_libraries` 指向的扩展（pg_stat_statements、
   pg_buffer_stats）在 postmaster 启动期申请共享内存——改配置后 reload 不生效，
   必须 restart。
4. **二进制替换必须可回滚**：换版本前整套备份（bin+lib+share），验收不过原样退回；
   源数据目录不动。

## 参考索引（渐进披露）

| 文件 | 内容 |
|------|------|
| `references/build-from-source.md` | 依赖、configure/make、ARM 优化构建、安装布局与验收 |
| `references/contrib-extensions-and-patches.md` | contrib 开发模型、自研观测扩展、补丁管理与回退 |
| `references/deployment-and-config.md` | initdb、pg_ctl 生命周期、GUC 模板、二进制备份与版本回滚 |

## 输出契约

部署/变更完成后至少给出：

1. **身份记录**：版本 tag（如 REL_17_5）、`pg_config --configure` 全串、二进制
   md5/commit；
2. **验收结果**：`pg_ctl status`、`SELECT version();`、扩展 `SELECT * FROM
   pg_extension;`、冒烟（→ pgsql-test-verification 的 smoke_test.sh）；
3. **回滚说明**：备份目录位置与恢复命令。

## 维护约定

- 版本锚点只用官方 tag（REL_17_5 / REL_18_3）；补丁文件登记出处与目的；
- 与 pgsql-architecture 的机制表述保持同一口径（锁/缓冲区名词不另造）。

## 交接说明

姊妹 skill：`pgsql-architecture`（机制知识库）、`pgsql-test-verification`（测试
验证）、`pgsql-hotspot-analysis`（热点分析）、`pgsql-performance-optimization`
（性能优化）。同属 pgsql-optimize 领域分组，由同一系列 PR 交付（源码位于
`Plugins/app-tuner/skills/pgsql-optimize/`，`install.sh` 会把五个成员平铺安装到
`<tool>/skills/<name>/`，安装后入口即可用——该安装行为随分组 DOMAIN.md 合入后
生效；此前 install.sh 对缺 DOMAIN.md 的领域分组按设计整组跳过，入口暂不可用属
预期窗口期）。找不到姊妹入口时不要凭空继续：把部署状态（版本/配置/补丁清单）
交回维护者。
