# 探针、判定边界与官方依据

## 接口边界

实现仅使用下列接口：CLI `--version`、TCP MySQL 协议、`SELECT 1`、
`SHOW FRONTENDS` 的 `Join`/`Alive`/`Version`/`CurrentConnected` 列，
`SHOW BACKENDS` 的 `Alive`，可选使用 `SystemDecommissioned` 和 `Status`，
以及 `numbers("number"="1000")` 的 SUM/COUNT。标量 SELECT 抑制列名，SHOW
保留列名供按列解析；Doris 软件版本读取 FE `Version`，不读取 MySQL 协议版本。
FE 版本解析为 `doris-X.Y.Z...` 后，只接受 2.1.x、3.0.x、3.1.x。拒绝其他版本
是有意的接口审查边界，不断言这些版本一定不兼容，也不宣称全部已做真实 E2E。

## 探针表

| 名称 | 必需 | 实际动作 | ready 的必要条件 |
| --- | --- | --- | --- |
| `credential_source` | 是 | 检查来源、文件元数据并解析认证/TLS 白名单 | 来源无歧义；defaults 文件安全，且不含初始化 SQL、include 或无关选项 |
| `output_storage` | 是 | `statvfs` 等价能力检查 | 报告父文件系统至少有 1 MiB 可用，仅供小报告，不是 Doris 容量检查 |
| `client` | 是 | 有界执行 `mysql --version` 或指定 CLI | 进程成功且标识为 MySQL/MariaDB；随后 SQL 仍必须成功 |
| `query_authentication` | 是 | 通过 TCP 执行 `SELECT 1` | 返回且只返回一行 `1` |
| `frontend_compatibility` | 是 | 执行 `SHOW FRONTENDS` | `CurrentConnected` 的 FE 同时 Join/Alive，版本位于上述接口行 |
| `backend_prerequisites` | 是 | 执行 `SHOW BACKENDS` | 至少一个 Alive、非 decommission、未标记 `isQueryDisabled` 的 BE |
| `numbers_capability` | 是 | 对 `numbers("number"="1000")` 求 SUM/COUNT | 精确返回一行 `499500<TAB>1000`，证明基准需要的实际执行接口可用 |

标量 SELECT 带 `--skip-column-names`；两个 SHOW 查询特意不带此参数，否则无法
按名称读取所需列。契约假 CLI 遵守同样的列名抑制开关，覆盖真实 CLI 输出形状。

每个子进程有独立的有限超时，并在超时后仅杀死其新建的进程组。解析器使用内存中的
完整 stdout/stderr，只有写入探针证据的副本会截断和脱敏。客户端 HOME 与登录文件
路径隔离；defaults 配置只以运行期白名单副本传入，并在结束后清理。

## 状态与失败语义

- **ready**：证据直接支持该项。总 ready 不排除限制章节声明的未测能力。
- **blocked**：证据确定地表明前置条件不满足。原因包括 `missing_tool`、
  `unsupported_client`、`unsupported_version`、`timeout`、`authentication`、
  `authorization`、`unreachable`、`runtime_error`、`invalid_prerequisite`。
- **unknown**：执行成功但内容为空、结构改变或 `SELECT 1` 不符合精确结果，
  无法把异常证据解释成就绪。
- **skipped**：`SELECT 1` 已确定认证失败后，其余依赖探针不再发起连接，避免重复
  失败登录；总结果仍为 blocked。
- 参数和输出冲突属于调用错误，退出 2；不会用“未知”掩盖旧报告冲突。

必需探针 blocked 使总结果 blocked；任何无法确定且未被更早 blocked 覆盖的项
使总结果 unknown。本实现当前所有检查都与合成 SELECT 直接相关，没有无关的
可选安装或部署检查；`required` 字段保留强制/可选区分语义。

## 官方资料

- [Doris 3.x：数据库连接](https://doris.apache.org/docs/3.x/db-connect/database-connect/)
- [Doris 3.x：SHOW FRONTENDS](https://doris.apache.org/docs/3.x/sql-manual/sql-statements/cluster-management/instance-management/SHOW-FRONTENDS/) — 当前 FE、Join、Alive、Version 字段定义。
- [Doris 3.x：SHOW BACKENDS](https://doris.apache.org/docs/3.x/sql-manual/sql-statements/cluster-management/instance-management/SHOW-BACKENDS/) — Alive、decommission、Status 字段定义。
- [Doris 3.x：NUMBERS](https://doris.apache.org/docs/3.x/sql-manual/sql-functions/table-valued-functions/numbers/) — 生成 `[0, number)` BIGINT 行的表函数。
- [Doris 2.1：SHOW FRONTENDS](https://doris.apache.org/docs/2.1/sql-manual/sql-statements/cluster-management/instance-management/SHOW-FRONTENDS/)
- [MySQL 8.0：option-file options](https://dev.mysql.com/doc/refman/8.0/en/option-file-options.html) — defaults 文件选项。
- [MySQL 8.0：password security](https://dev.mysql.com/doc/refman/8.0/en/password-security-user.html) — 不在命令行暴露口令。

文档说明接口语义，不代替现场探针，也不证明生产健康。

## 实现来源

TCP-only argv 构造和精确标量结果验证参考了[已有 Doris 部署技能](https://gitcode.com/openeuler/KPBot/merge_requests/205)中的 `check_service.py`。本技能包含独立的进程控制、三态报告、版本检查、凭据权限检查与脱敏实现。
