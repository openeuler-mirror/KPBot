---
name: kptools-clickhouse-environment-check
description: 当需要在 ClickHouse 基准测试前只读检查已有安装、非默认端点、认证、版本接口和必要资源时使用。输出 ready/blocked/unknown 与逐探针证据；不启动或修改服务，也不把端口连通当成就绪。
license: MulanPSL-2.0
metadata:
  compatibility: Linux；Python 3.10+；目标机已有官方 ClickHouse 客户端与 benchmark 接口
  skill_type: tool_assisted
  category: environment_check
  domain: clickhouse
  version: 1.0.0
---

# ClickHouse 基准环境只读检查

## 职责与边界

检查一个**已经存在且获授权访问**的 ClickHouse 目标是否满足随包合成 SELECT 基准的最低前提。只调用官方客户端的只读接口并写入新证据目录；不安装依赖，不启动、停止或重启服务，不创建数据库或表，不修改 OS/服务配置。

`ready` 仅表示本次实际执行的工具、认证、SQL 与输出目录检查通过，不代表生产健康、集群健康、复制延迟、容量或性能达标。

## 依赖与版本口径

- Python 3.10+，仅使用标准库。
- 独立的 `clickhouse-client` 与 `clickhouse-benchmark`，或同一官方二进制的 `client`/`benchmark` 子命令。
- 服务端版本不得低于 22.8.0；还必须通过实时版本查询、精确聚合和 benchmark `--help` 接口检查。这个下限是脚本契约，不是对一系列上游版本的兼容承诺。
- 完整保留三段或四段版本。单二进制模式从全局 `--version` 取得共享版本；**不会调用不受支持的 `benchmark --version`**。独立 benchmark 以 `--help` 和所需选项作为接口证据。

## 用法

以下命令均在本 skill 根目录执行。`--output` 必须是不存在的新目录，父目录会自动创建，旧证据不会被覆盖。

```bash
python3 scripts/check_environment.py \
  --host localhost --port 9000 --user default \
  --output results/preflight-001
```

非默认 TLS 端点可直接使用 `--secure`；密码只从 `CLICKHOUSE_PASSWORD` 环境变量注入，脚本没有 `--password` 参数，也不会把密码放进 argv、JSON 或日志。账号优先级为显式 `--user`、`--config` 中的 `<user>`、客户端内置 `default`；解析后的账号会显式传给所有连接与 benchmark 接口，结果 JSON 的 `target.user` 记录实际账号，`target.user_source` 记录 `cli`、`config` 或 `client_default`。配置无法解析或账号为空时会在启动客户端前失败。以下示例在有界子 shell 中运行，`EXIT` trap 在正常或失败退出时清除密码，不修改父 agent 会话中的 `CLICKHOUSE_PASSWORD`。若此前已在父会话导出此变量，使用结束后执行 `unset CLICKHOUSE_PASSWORD`。仅当确有现成客户端配置时才追加 `--config ./existing-client.xml`；该文件必须为普通文件且 group/other 无权限（通常 `chmod 600`），示例不要求预先创建配置文件。

```bash
(
  trap 'unset CLICKHOUSE_PASSWORD' EXIT
  read -rsp 'ClickHouse password: ' CLICKHOUSE_PASSWORD || exit
  export CLICKHOUSE_PASSWORD
  echo
  python3 scripts/check_environment.py \
    --host db.example --port 9440 --user reader --secure \
    --output results/preflight-tls-001
)
```

已有单一 `clickhouse` 二进制时：

```bash
python3 scripts/check_environment.py \
  --client ./clickhouse --single-binary --output results/preflight-single-001
```

`--timeout` 是每个子进程的有限秒数，默认 20，允许 0.05–600；超时或取消（包括键盘中断）立即只 SIGKILL 本脚本创建的进程组，随后有界回收组长和读取管道，覆盖组长先退出、后代忽略 SIGTERM 的情况；取消异常随后原样抛出。详细探针与状态判定见 [probes.md](references/probes.md)，字段与单位见 [metrics.md](references/metrics.md)。

## 输出与退出码

标准输出和 `<output>/result.json` 是同一份 JSON。每个 `checks[]` 含 `name`、`mandatory`、`status`、`reason` 和 `evidence`；原始 stdout/stderr 经密码脱敏后分文件保存。

- `ready`：所有必需探针就绪；可选的客户端/服务端系列不一致只保留提示。
- `blocked`：存在必需探针失败，原因区分 `missing_tool`、`wrong_version`、`timeout`、`auth`、`correctness`、`resource` 或 `error`。
- `unknown`：没有已阻断项，但版本或依赖证据不足，不能判为就绪。

退出码：`0=ready`，`1=blocked`，`2=参数错误或输出目录/写入失败（含冲突）`，`3=unknown`。失败时仍保留已完成的探针证据；参数或输出冲突在启动客户端前失败。

## 契约回归

```bash
python3 tests/test_contract.py
```

该 tier-A 用例以假官方 CLI 验证真实脚本进程和 JSON 语义，覆盖成功、旧版本、缺工具、认证、超时、错误聚合、接口缺失、单二进制 26.8 行为、安全端点、配置账号对齐、数值口令、探针日志写入失败、密码示例的父会话清理及输出冲突；另用真实 fork 后代和取消时存活的子进程回归验证有界清理、异常重抛和 PID 回收。Mock 结果不是 ClickHouse 服务 E2E。
