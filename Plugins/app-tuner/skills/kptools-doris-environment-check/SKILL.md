---
name: kptools-doris-environment-check
description: 当用户要求在 Doris 查询基准前只读检查已有实例、MySQL 客户端、非默认 FE 端点、认证、版本或 FE/BE 前置条件时使用。交付逐项证据和 ready/blocked/unknown JSON；不部署、不启停服务、不写数据库，也不把端口连通当作就绪。
metadata:
  compatibility: Linux；Python 3.9+；用户已有 Doris 2.1.x、3.0.x 或 3.1.x 与 mysql/mariadb CLI
  skill_type: tool_assisted
  category: environment_check
  domain: doris
  version: 1.0.0
---

# Doris 查询基准只读环境检查

## 范围与输入

检查一个**已经存在且获用户授权查询**的 Doris FE。脚本只执行 `SELECT 1`、
`SHOW FRONTENDS`、`SHOW BACKENDS` 和 `numbers(1000)` 精确聚合能力查询，
并检查客户端、凭据来源与报告磁盘空间。不会安装软件、启动或停止服务、修改
OS、创建数据库/表或写入 Doris。

必需输入是一个新的 `--output` 文件路径。默认目标是 `127.0.0.1:9030`、用户
`root`、客户端 `mysql`；用 `--host`、`--port`、`--user`、`--client` 覆盖。
版本许可仅限代码实际解析的 Doris 2.1.x、3.0.x、3.1.x 接口行；其他版本明确
`blocked/unsupported_version`，不能外推为整个 Doris 或生产环境兼容性。

## 独立运行

在本 skill 根目录运行，依赖 Python 标准库及已安装的 MySQL/MariaDB 客户端：

```bash
python3 scripts/check_environment.py \
  --host 127.0.0.1 --port 9030 --user root \
  --output results/preflight-001.json
```

使用非默认端点或 `mariadb` 时显式传参。所有 SQL 都通过 argv 数组和
`--protocol=TCP` 交给官方 CLI，不经过 shell 插值。

### 安全认证

认证实例可使用管理员提供的现有 defaults 文件：先将 `DORIS_CLIENT_CONFIG` 设置为该文件路径，文件须归当前用户所有、非符号链接且权限不含 group/other 位。无密码实例无需此文件。

```bash
python3 scripts/check_environment.py \
  --config "$DORIS_CLIENT_CONFIG" --output results/preflight-002.json
```

也可由受控环境注入 `DORIS_PASSWORD`（脚本只向客户端子进程映射为
`MYSQL_PWD`），或复用已有 `MYSQL_PWD`。密码绝不能作为命令行参数。配置与密码
环境二选一，避免来源不明；无密码实例可都不提供。脚本仅在内存中解析配置，
连接目标 `host`、`port`、`user` 仅由命令行指定；请从配置中移除这些项。
认证/TLS 配置可包含 `password`、`ssl-ca`、`ssl-cert`、`ssl-key` 等白名单选项；
拒绝初始化 SQL、外部 include 和非认证/TLS 选项，再将白名单选项写入运行期独占
临时文件并在结束后清理。客户端使用隔离 HOME 和登录文件路径，不读取隐式
`.mylogin.cnf`；报告只保存脱敏、截断的证据副本，完整输出仅用于内存解析。

## 输出与退出码

报告父目录自动创建，报告用独占创建和 `0600` 权限写入；已有文件或符号链接
不会被覆盖。`status=ready` 要求所有必需探针通过，绝不由 socket-open、包导入
或旧报告推断。每项包含 `required`、三态 `status`、`reason` 和有界脱敏证据。

- `0 / ready`：当前连接、精确 SQL 结果、当前 FE、版本、可用 BE 和
  `numbers(1000)` 聚合能力满足本次合成 SELECT 基准的最低前置条件。
- `1 / blocked`：已确定的工具缺失、版本不支持、超时、认证/权限、连接、
  节点状态或运行错误。
- `2`：参数或输出创建错误；已有输出保持不变。
- `3 / unknown`：返回结构或结果无法支持确定判断。

即使 ready 也不是生产健康、数据读写、导入能力或持续容量证明。探针定义、
失败分类、官方文档和实现来源见 [references/probes.md](references/probes.md)。

## 可执行契约

```bash
bash cases/contract/run.sh
```

Tier-A 用例使用临时假 CLI 验证成功、缺失工具、错误版本、认证、超时、运行失败、
错误结果、输出冲突、配置权限和脱敏；它不是 Doris 服务 E2E。真实服务结果只有
在用户明确给出目标和授权后，通过上述入口重新采集才有效。
