# VectorDBBench 部署执行手册

> 本文件是 `vectordbbench-deploy` SKILL.md 的执行细节层：SKILL.md 负责触发、版本策略与总体流程，
> 此处给出 Python 3.11+ 环境、源码安装、extras、离线 wheelhouse、venv/CLI 布局、升级回退、
> 安全说明与故障排查。实际 benchmark 运行与向量数据库调优不在本手册范围。

## 1. 支持范围与前置条件

| 项 | 要求 |
|----|------|
| 操作系统 | openEuler 24.03、RHEL/CentOS/Rocky/Alma 8+、Debian 12+/Ubuntu 24.04+ |
| CPU | aarch64（鲲鹏）或 x86_64 |
| 权限 | root（安装系统包、写 /opt 与 /usr/local/bin） |
| Python | **3.11+**（VectorDBBench master 的 pyproject 要求 `requires-python >= 3.11`） |
| venv | Python 自带 `venv` 模块（Debian 需 `python3-venv`） |
| 构建 | git、gcc、make、tar；脚本可自动安装 |
| 磁盘 | venv + 依赖约 2-5 GiB；数据集与结果另算 |
| 网络 | 默认访问 GitHub 与 PyPI；离线需 `--source-dir` + `--wheelhouse` |

默认布局：

```text
/opt/vectordbbench/                    # 安装根目录 root:root 0755
/opt/vectordbbench/venv-py3.11/        # 版本化 venv
/opt/vectordbbench/current -> venv-py3.11
/usr/local/bin/vectordbbench -> .../current/bin/vectordbbench
/usr/local/bin/init_bench -> .../current/bin/init_bench
/var/cache/vectordbbench-deploy/       # 构建缓存 root:root 0700
/var/log/vectordbbench-deploy/<run_id> # deploy.env 与日志，root-only 0700
```

## 2. 版本策略

- **未指定版本时使用内置固定版本 `v2.0.0`**：checkout 后比对 tag 解析出的 commit 与内置值，
  不一致立即停止（防止 tag 被移动、镜像被篡改或 `--git-url` 非上游）。
- `master`/`main` 等移动分支默认拒绝，必须显式 `--allow-moving-branch`；安装后记录实际 commit
  作为审计字段，不替代安装前的固定版本校验。
- 记录实际 `git rev-parse HEAD` 到 `VECTORDBBENCH_COMMIT`，并用 `pip show vectordb-bench`
  记录 `VECTORDBBENCH_PACKAGE_VERSION`。
- 指定 tag/commit 时按 ref checkout。
- `--source-dir` 使用目录自带版本；目录没有 `.git` 时使用
  `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0` 作为包版本回退，并在部署产物中标注 source-dir。
- 升级前保留旧 venv（版本化目录）；不自动删除旧版本。

## 3. Python 3.11+ 与 venv

- 自动按 `python3.13`、`python3.12`、`python3.11`、`python3` 顺序查找满足 3.11+ 的解释器。
- 解释器必须属于 root 且组/其他不可写，避免 root 执行用户可替换的 Python。
- 找不到 3.11+ 时明确失败；请安装发行版提供的 `python3.11`/`python3.12`：
  - openEuler 24.03 / Debian 12 / Ubuntu 24.04 通常自带 3.11+。
  - RHEL 8/9、Ubuntu 22.04 默认 Python 可能低于 3.11，需要额外安装并用 `--python` 指定。
  - Debian 系还需要对应 `python3.X-venv` 包。
- venv 布局：`/opt/vectordbbench/venv-py<major.minor>`；`current` 符号链接指向当前 venv。
- 同一 Python 版本重跑会复用 venv 并重新 `pip install`；venv 损坏时创建带 run_id 的新目录，旧目录保留。

## 4. 安装与数据库 extras

VectorDBBench 的 PyPI 包名为 `vectordb-bench`，默认只包含 PyMilvus/Zilliz Cloud 客户端。
其他数据库需要安装 extras：

| 目标数据库 | extras |
|------------|--------|
| Milvus / Zilliz Cloud | 默认，无需 extras |
| pgvector / PolarDB-PG / VectorChord / ADBPG | `pgvector` |
| Qdrant | `qdrant` |
| Pinecone | `pinecone` |
| Weaviate | `weaviate` |
| Elastic / 阿里云 Elasticsearch | `elastic` |
| Redis | `redis` |
| Valkey | `valkey` |
| MongoDB | `mongodb` |
| OpenSearch / Lindorm | `opensearch` / `lindorm` |
| OceanBase | `oceanbase` |
| Doris | `doris` |
| TiDB / PolarDB MySQL | `tidb` / `polardb` |

用法：

```bash
# 默认（仅 PyMilvus）
bash scripts/install_vectordbbench.sh

# pgvector 客户端
bash scripts/install_vectordbbench.sh --extras pgvector

# 多个 extras
bash scripts/install_vectordbbench.sh --extras pgvector,redis
```

安装命令等价于在源码目录执行：

```bash
pip install ".[pgvector,redis]"
```

## 5. 在线、离线与 pip 源

在线默认：

```bash
bash scripts/install_vectordbbench.sh
```

离线需要同时提供源码与依赖 wheelhouse：

```bash
bash scripts/install_vectordbbench.sh \
  --source-dir /root/VectorDBBench \
  --wheelhouse /root/wheels \
  --extras pgvector
```

- `--source-dir` 必须是 root 属主、组/其他不可写；复制到私有构建目录后再次校验。
- `--wheelhouse` 必须是 root 属主、组/其他不可写；内部文件也会校验。
- 离线安装使用 `--no-index --find-links` + `--no-build-isolation`，因此 wheelhouse 必须包含：
  `pip`、`setuptools`、`wheel`、`setuptools_scm`、VectorDBBench 全部依赖和 extras。
- 私有 pip 索引优先用 `--pip-index-url-file`（root 属主 0600 文件，取首行），或在执行前预置
  环境变量 `PIP_INDEX_URL`；脚本只经 `PIP_INDEX_URL` 传给 pip，不写入日志与 `deploy.env`。
- `--pip-index-url` 仅接受不含凭据的 URL；含 `user:token@` 的命令行 URL 会被拒绝（argv 可被
  `ps`/`/proc` 读取）。脚本打印索引地址时会把 userinfo 脱敏为 `***@`。
- 在线安装的第三方依赖由 pip 解析；本技能不对整棵依赖树做 hash 锁定，需要可复现依赖树时
  使用 wheelhouse，并自行校验 wheel 的 sha256 或签名。
- 不执行远程脚本管道，不关闭 TLS 校验；私有 HTTP 索引如需 `PIP_TRUSTED_HOST`，在环境中自行配置。

## 6. 安装后验证

```bash
/opt/vectordbbench/current/bin/vectordbbench --help
vectordbbench --help
vectordbbench --version 2>/dev/null || true
/opt/vectordbbench/current/bin/pip show vectordb-bench
```

期望：

- `vectordbbench --help` 输出 `Commands:` 列表并退出码 0
- `pip show vectordb-bench` 显示 `Version`
- `deploy.env` 中 `DEPLOY_STATUS=ok`、`VECTORDBBENCH_VERIFY=ok`

桌面 UI（可选）：

```bash
/opt/vectordbbench/current/bin/init_bench
```

UI 依赖 Streamlit；会占用端口并写入用户缓存，建议由普通用户在自己的会话中运行。

## 7. 升级、回退与清理

升级：

1. 记录当前 venv 路径与 commit；venv 目录不会被自动删除。
2. 用新 tag/commit 重跑脚本；会创建/复用 venv、重新 pip install，**CLI 验证通过后**才切换
   `current`（失败保留原指向）。
3. 验证 CLI 与目标数据库 extras。

回退：

- 将 `/opt/vectordbbench/current` 指回旧 venv 目录，并同步更新 `/usr/local/bin/vectordbbench` 链接
  （旧 venv 在重跑时保留）。
- 旧目录与新目录的 Python 版本可能不同，回退前确认解释器仍可用。

清理：

> **危险操作警告**：删除 venv、构建缓存或日志会永久丢失环境；必须由运维确认后执行。
> 本技能脚本不提供自动卸载入口，也不会自动删除旧 venv 或构建目录。

- 卸载/清理前先确认没有正在运行的 CLI/UI 进程。
- 需要处理的路径：`/opt/vectordbbench`、`/var/cache/vectordbbench-deploy`、
  `/var/log/vectordbbench-deploy`，以及 `/usr/local/bin` 下的 CLI 链接。
- 数据集通常位于运行用户自己的缓存/数据目录，需单独规划磁盘。

## 8. 故障排查

| 症状 | 处置 |
|------|------|
| 固定版本 commit 校验失败 | 检查 tag/镜像/`--git-url`；确认上游后更新内置 commit |
| `--pip-index-url` 含凭据被拒绝 | 改用 `--pip-index-url-file`（root 0600）或环境变量 `PIP_INDEX_URL` |
| 找不到 Python 3.11+ | 安装 python3.11/3.12，或用 `--python /usr/bin/python3.11` |
| Python 不可信/组可写 | 使用 root 安装的系统 Python；不要用用户目录下的 Python |
| 缺少 venv 模块 | Debian/Ubuntu 安装 `python3.X-venv`；RHEL 确认 python3 包完整 |
| pip 下载失败 | 检查网络/代理/索引；离线用 `--wheelhouse` |
| wheelhouse 缺包 | 补齐 pip/setuptools/wheel/setuptools_scm 与全部依赖 wheels |
| `setuptools_scm` 版本错误 | 源码目录需有 `.git`；无 `.git` 时会使用 0.0.0 回退版本 |
| CLI 链接冲突 | 脚本会备份已有 `/usr/local/bin/vectordbbench` 为 `.bak.<时间戳>` 再创建 |
| `current` 是真实目录 | 脚本拒绝覆盖；人工重命名后重试 |
| 数据库客户端缺失 | 用 `--extras <名称>` 重装；确认 extras 名称与上游 README 一致 |
| 磁盘不足 | venv+依赖约 2-5 GiB；清理旧 venv/构建目录前先确认 |
| aarch64 依赖无 wheel | 安装 gcc/make/python3-devel，或准备 aarch64 wheelhouse |

## 9. 安全说明

安全基线、默认版本与凭据处理以 SKILL.md「安全基线」为准，本节不重复维护。仅补充执行层注意事项：

- venv 与 CLI 以 root 安装；普通用户运行 CLI 时只读 venv，数据集/结果目录由该用户自行准备。
- 升级/回退/清理前确认没有正在运行的 CLI/UI 进程；脚本不自动删除旧 venv、构建目录或用户数据。
