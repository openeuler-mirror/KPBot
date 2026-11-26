---
name: vectordbbench-deploy
description: VectorDBBench 环境安装部署技能（Linux，aarch64/x86_64 通用）。当需要安装、部署、搭建 VectorDBBench 向量数据库基准测试环境，创建 Python 3.11+ venv、从源码安装 vectordb-bench、安装数据库客户端 extras、配置离线 wheelhouse，或验证 vectordbbench CLI 时使用。未指定版本时默认安装内置固定版本 v2.0.0 并校验 commit、记录包版本；master 等移动分支需显式 --allow-moving-branch；不负责实际 benchmark 运行与数据库调优。
license: MulanPSL-2.0
compatibility: Linux（openEuler/RHEL/CentOS/Debian 系），aarch64 或 x86_64，root 权限，Python 3.11+ 与 venv，git/gcc/make；在线需访问 GitHub 与 PyPI，离线需准备源码目录与 wheelhouse。
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: vector_database
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# VectorDBBench 环境安装部署

在 Linux 主机上创建 Python 3.11+ 独立 venv，从源码安装
[VectorDBBench](https://github.com/zilliztech/VectorDBBench)（默认内置固定版本 v2.0.0），安装目标数据库
客户端 extras，验证 `vectordbbench` CLI，并产出可复用的 `deploy.env`。

## 版本策略（强约束）

| 用户输入 | 行为 |
|----------|------|
| 未指定版本 | **安装内置固定版本 v2.0.0**，并校验 tag 解析出的 commit 与内置值一致 |
| 指定 tag（如 v1.0.22） | 按 tag checkout 构建，记录 commit 与包版本 |
| 指定 commit | 按 commit checkout 构建，记录 commit 与包版本 |
| master/main/develop | 默认拒绝；必须显式 `--allow-moving-branch`，并记录实际 commit |
| 使用 `--source-dir` | 使用目录自带版本；无 `.git` 时包版本回退为 0.0.0 |
| 只说要“最新版” | 说明默认是固定版本；如需移动分支必须显式 opt-in |

内置版本校验失败（tag 被移动、镜像被篡改、`--git-url` 指向非上游）时立即停止。
`deploy.env` 必须记录 `VECTORDBBENCH_COMMIT` 与 `VECTORDBBENCH_PACKAGE_VERSION`；
记录 commit 是审计字段，不能替代安装前的固定版本校验。

## 何时触发

- 需要安装、部署、搭建 VectorDBBench 环境
- 需要从源码安装 `vectordb-bench`，或指定 tag/commit（master 需显式 opt-in）
- 需要为 pgvector、Milvus、Qdrant、Pinecone、Redis 等数据库安装对应客户端 extras
- 需要离线 wheelhouse、内网 pip 索引或私有镜像
- 需要验证 `vectordbbench --help`、包版本与 CLI 链接

不适用：实际 benchmark 运行、数据集下载、向量数据库性能调优、结果分析；
这些应在部署完成后单独执行。

## 工作流总览

```text
前置检查（只读） → 系统依赖 → 查找 Python 3.11+ → 创建/复用 venv
  → 克隆/复制源码到 root 私有目录 → 升级 pip/build 工具
  → pip install .[extras] → 验证 vectordbbench --help
  → 更新 current / 全局 CLI 链接 → deploy.env
```

## 快速开始

在 skill 根目录执行：

```bash
# 1) 只读前置检查
bash scripts/preflight_check.sh

# 2) 默认部署：内置固定版本 + Python 3.11+ + 仅 PyMilvus
bash scripts/install_vectordbbench.sh

# 3) 指定 tag 并安装 pgvector 客户端
bash scripts/install_vectordbbench.sh --version v1.0.22 --extras pgvector

# 4) 已有 Python 3.11，指定解释器
bash scripts/install_vectordbbench.sh --python /usr/bin/python3.11

# 5) 离线安装
bash scripts/install_vectordbbench.sh \
  --source-dir /root/VectorDBBench \
  --wheelhouse /root/wheels \
  --extras pgvector

# 6) 只打印计划
bash scripts/install_vectordbbench.sh --dry-run
```

## 默认布局

| 项 | 默认值 |
|----|--------|
| 版本 | `v2.0.0`（内置固定版本，校验 commit） |
| Python | 自动查找 3.13/3.12/3.11/3（要求 3.11+） |
| 安装根目录 | `/opt/vectordbbench` |
| venv | `/opt/vectordbbench/venv-py<major.minor>` |
| current | `/opt/vectordbbench/current` 符号链接 |
| CLI | `/usr/local/bin/vectordbbench`（可 `--no-cli-link`） |
| UI | `/usr/local/bin/init_bench`（存在时） |
| 构建缓存 | `/var/cache/vectordbbench-deploy`（root:root 0700） |
| 结果目录 | `/var/log/vectordbbench-deploy/<run_id>`（root-only 0700） |

## 参数

```text
--version VER           版本/ref，默认内置固定版本 v2.0.0
--allow-moving-branch   允许 master/main 等移动分支（默认拒绝；会记录实际 commit）
--git-url URL           源码仓库，默认 https://github.com/zilliztech/VectorDBBench.git
--source-dir DIR        离线源码目录（root 属主、组/其他不可写）
--python PATH           指定 Python 3.11+ 解释器
--extras LIST           数据库客户端 extras，逗号分隔（如 pgvector,redis）

--install-root DIR      默认 /opt/vectordbbench
--bin-dir DIR           CLI 链接目录，默认 /usr/local/bin
--no-cli-link           不创建全局 CLI 链接
--build-root DIR        默认 /var/cache/vectordbbench-deploy
--result-dir DIR        默认 /var/log/vectordbbench-deploy/<run_id>

--wheelhouse DIR        离线 wheelhouse（root 属主、组/其他不可写）
--pip-index-url-file FILE  从 root 属主 0600 文件读取 pip 索引（推荐，首行生效）
--pip-index-url URL     自定义 pip 索引（禁止含凭据；建议改用文件或环境变量）
--skip-deps / --dry-run
```

## 安全基线

- 默认安装内置固定版本 v2.0.0，并在 checkout 后校验 commit；master/main 等移动分支
  必须显式 `--allow-moving-branch`。记录 commit 仅是审计字段。
- 构建根目录必须 `root:root 0700`，父目录不得属于非 root 用户或非 sticky 的组/其他可写目录；
  工作目录用 `mktemp -d` 创建。
- 路径参数必须是绝对路径，拒绝 `.`、`..`、`//` 段；`realpath` 归一化后再检查系统目录黑名单。
- Python 解释器、源码目录、wheelhouse 必须 root 属主且组/其他不可写（`stat -L` 解引用符号链接）；
  复制源码后再次校验。
- venv 以 root 安装；CLI 链接覆盖前先备份为 `.bak.<时间戳>`；`current` 是真实目录时拒绝覆盖；
  `current` 只在 CLI 验证通过后切换，失败保留原指向。
- 自定义 pip 索引通过 `PIP_INDEX_URL` 环境变量或 root 属主 0600 文件传入；
  命令行 URL 不得含用户名/凭据，不写入日志/deploy.env；日志目录 root-only 0700。
- 依赖由 pip 从配置的索引正常解析；需要可复现的依赖树时使用离线 wheelhouse 并自行校验制品。
- 不自动修改内核参数；不自动删除旧 venv、构建目录或用户数据；清理需人工确认。

## 产物与验收

`deploy.env` 关键字段：

```text
DEPLOY_STATUS=ok
VECTORDBBENCH_VERSION=v2.0.0
VECTORDBBENCH_COMMIT=<实际 commit>
VECTORDBBENCH_PACKAGE_VERSION=<pip 版本>
VECTORDBBENCH_SOURCE=<仓库或 source-dir>
VECTORDBBENCH_EXTRAS=<extras 或 none>
VECTORDBBENCH_PYTHON=<解释器>
VECTORDBBENCH_PYTHON_VERSION=<完整版本>
VECTORDBBENCH_VENV=<venv 路径>
VECTORDBBENCH_CLI=<venv 内 CLI>
VECTORDBBENCH_CLI_LINK=<全局链接或 none>
VECTORDBBENCH_UI_LINK=<init_bench 链接或 none>
VECTORDBBENCH_PIP_SOURCE=pypi|custom-index|wheelhouse
VECTORDBBENCH_VERIFY=ok
```

验收命令：

```bash
/opt/vectordbbench/current/bin/vectordbbench --help
/opt/vectordbbench/current/bin/pip show vectordb-bench
cat /var/log/vectordbbench-deploy/<run_id>/env/deploy.env
```

通过标准：`vectordbbench --help` 输出 `Commands:` 且退出码 0；`pip show` 有版本；
`deploy.env` 中 `DEPLOY_STATUS=ok`、`VECTORDBBENCH_VERIFY=ok`。

## 重复执行与升级

- 同一 Python 版本重跑会复用 `venv-py<major.minor>`，升级 pip/build 工具并重新 pip install；
  新 venv 验证通过后才把 `current` 链接切到该 venv。
- 不同 Python 版本会创建新的 `venv-py<major.minor>`，旧 venv 保留。
- venv 损坏时创建带 run_id 的新目录，旧目录保留；不自动删除。
- 安装或 CLI 验证失败时 `current` 保持原指向，不把“当前版本”留在不可用状态。
- 升级前记录旧 venv 路径与 commit；回退时把 `current` 指回旧 venv 并更新 CLI 链接。
- 数据库 extras 变更通过 `--extras` 重跑；不会自动卸载旧 extras。

## 常见问题速查

| 症状 | 处置 |
|------|------|
| 未指定版本 / 想装 master | 默认 v2.0.0；移动分支显式 `--allow-moving-branch` |
| 固定版本 commit 校验失败 | 检查 tag/镜像/`--git-url`；确认上游后再更新内置 commit |
| 私有索引含凭据 | 用 `--pip-index-url-file`（root 0600）或环境变量，不要放命令行 |
| 其余（Python、venv、pip、wheelhouse、CLI 链接、磁盘） | 见 `references/vectordbbench-install-guide.md` §8 故障排查 |

## 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight_check.sh` | 只读检查：权限、OS、资源、工具、Python 3.11+/venv、源码/wheelhouse、目录安全 |
| `scripts/install_deps.sh` | 安装 git/gcc/make/tar、系统 Python3 + pip/venv/dev 头文件 |
| `scripts/install_vectordbbench.sh` | 主流程：源码获取、venv、pip 安装、CLI 验证/链接、deploy.env |
| `scripts/lib/common.sh` | 日志、root 检查、Python 3.11+ 查找与安全校验 |
| `assets/smoke_check.sh` | 只读验证 CLI `--help` 输出命令列表 |

## 参考资料

- `references/vectordbbench-install-guide.md` — Python/venv、extras、离线 wheelhouse、升级回退、
  安全与故障排查。
- 上游项目：https://github.com/zilliztech/VectorDBBench
- PyPI：https://pypi.org/project/vectordb-bench/

## 输出契约

- 成功：`deploy.env` 中 `DEPLOY_STATUS=ok`，并给出 CLI、venv、Python、包版本、commit 与验收结果。
- 失败：返回失败阶段、日志路径、已生成产物与缺失项；等待人工处理，不自动删除现场。
- 任何变更类操作：说明改前/改后、备份路径与回滚方式；不得输出 pip 索引凭据或数据库密码。

## 错误处理

系统依赖安装失败、Python 3.11+ 缺失、固定版本 commit 校验失败、venv 创建失败、pip 安装失败、
CLI 验证失败或链接冲突时立即停止后续阶段，保留 `logs/`、`deploy.env`（`DEPLOY_STATUS=failed`）
与构建目录，修复后重跑；`current` 未通过验证不会切换。
无法满足安全基线（路径非法、Python/源码/wheelhouse 属主不安全、pip 索引凭据出现在命令行）时
返回原因并等待用户决策。
