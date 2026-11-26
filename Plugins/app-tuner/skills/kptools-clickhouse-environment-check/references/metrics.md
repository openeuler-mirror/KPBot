# 环境结果字段与单位

本 skill 不产出性能分数。`result.json` 的 `summary.ready/blocked/unknown` 是**探针个数**，不是查询数、节点数或健康评分。

| 字段 | 单位/取值 | 解释 |
| --- | --- | --- |
| `status` / `ready` | ready、blocked、unknown / 布尔 | 本次最小基准前提判定；不能外推生产健康。 |
| `checks[].mandatory` | 布尔 | 必需项决定总体状态；可选项只提供诊断。 |
| `checks[].reason` | 稳定类别或 null | missing_tool、wrong_version、timeout、auth、correctness、resource、error、dependency_failed 等。 |
| `checks[].evidence.duration_ms` | 毫秒 | 本子进程从启动前到结束的墙钟时间，含超时清理。不是服务端查询时延。 |
| `checks[].evidence.timeout_seconds` | 秒 | 本子进程硬超时上限。 |
| `checks[].evidence.free_bytes` | 字节 | 客户端证据目录所在文件系统的可用空间。 |
| `checks[].evidence.version` / `version_output` | 官方版本文本 | 保留完整三段或四段版本；独立 benchmark 不虚构版本。 |
| `checks[].evidence.expected` / `actual` | 聚合值 | 只用于 `numbers(1000)` 的精确正确性比对，两者必须都是 499500。 |

`stdout_log` 和 `stderr_log` 是相对本次输出目录的文件名。日志可能包含目标返回的非秘密诊断；注入的密码及常见 password 字段会脱敏。报告只记录 `config_provided`，不读取或复制配置内容。

结果一致性检查应同时验证 JSON 与对应日志：例如 `exact_query.actual=499500` 必须对应其 stdout，`server_interface` 非零返回码不能同时出现 ready。单独存在 `result.json` 文件不构成验收。
