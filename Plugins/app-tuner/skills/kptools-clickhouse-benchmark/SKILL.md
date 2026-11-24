---
name: kptools-clickhouse-benchmark
description: 当需要对已有 ClickHouse 服务执行有界、可复现的合成 SELECT 只读基准时使用。重新检查实时前提，分离预热与至少三轮测量，保留原生吞吐/延迟/计数、精确正确性、原始日志和失败结果；不安装或修改服务，不声称 TPC-H。
license: MulanPSL-2.0
metadata:
  compatibility: Linux；Python 3.10+；已有官方 ClickHouse 客户端、benchmark 接口和获授权的只读目标
  skill_type: tool_assisted
  category: benchmark
  domain: clickhouse
  version: 1.0.0
---

# ClickHouse 有界合成 SELECT 基准

## 职责与安全边界

对一个**现有且获授权测试**的 ClickHouse 目标实际执行固定迭代次数的只读查询负载。脚本独立包含实时预检，不读取陈旧报告，也不依赖其他 skill。它不安装或启停服务，不写数据库，不接受任意 SQL，不运行 TPC-H，不把结果解释为生产容量或业务 SQL 性能。

固定工作负载为 `SELECT sum(number) FROM numbers(N)`；`N` 只能由有界整数 `--rows` 生成，所有参数以 argv 传递且 `shell=False`。

## 依赖与版本口径

需要 Python 3.10+ 和已有官方客户端。服务端版本下限为 22.8.0，同时必须现场通过版本、精确聚合及 benchmark 所需选项检查；这不是宽泛版本兼容承诺。结果保留完整三段或四段版本。

单二进制模式从全局 `--version` 记录共享版本，独立 benchmark 不伪装支持版本命令，只记录 `benchmark_version=unavailable` 和 `--help` 接口证据。两种布局都不会调用 `benchmark --version`。

## 默认协议与上限

- 预热一次，迭代数为 `min(--iterations, 10)`；可用 `--warmup-iterations` 覆盖，预热不进入汇总。
- 默认 3 个测量轮次，每轮 10 次查询；允许 3–10 轮、每轮 1–10000 次。
- 默认 `rows=100000`、`concurrency=1`；上限分别为 1000000 和 16。
- 每个客户端/基准子进程默认最多 120 秒，允许 0.05–600 秒。没有无限迭代或无限时间选项；超时清理与管道读取也有有限上界。
- 原生工具使用 `--delay 0` 和 `--client-side-time`，只输出最终单端点报告。
- 每阶段计时外另执行同一 SELECT，精确比对 `N*(N-1)/2`；任何一轮非零退出、超时、计数/指标缺失或正确性不符都会停止后续轮次并使整次运行失败。

完整预检见 [probes.md](references/probes.md)，指标单位与汇总口径见 [metrics.md](references/metrics.md)。

## 用法

以下命令均在本 skill 根目录执行。输出目录必须不存在，父目录自动创建；旧结果及日志不会被删除或覆盖。

```bash
python3 scripts/run_benchmark.py \
  --host localhost --port 9000 --user default \
  --rows 100000 --iterations 10 --rounds 3 --concurrency 1 \
  --output results/run-001
```

单二进制布局：

```bash
python3 scripts/run_benchmark.py \
  --client ./clickhouse --single-binary --output results/run-single-001
```

TLS 可直接使用 `--secure`。密码仅通过 `CLICKHOUSE_PASSWORD` 环境变量传递，不能放入 argv；脚本会脱敏 JSON 和日志。仅当确有现成客户端配置时才追加 `--config /path/to/existing-client.xml`；配置文件必须是 group/other 无权限的普通文件（通常 `chmod 600`），密码与 TLS 本身不要求创建配置文件：

```bash
read -rsp 'ClickHouse password: ' CLICKHOUSE_PASSWORD; export CLICKHOUSE_PASSWORD; echo
python3 scripts/run_benchmark.py \
  --host db.example --port 9440 --user reader --secure \
  --output results/run-tls-001
```

## 结果与失败语义

`result.json` 保存目标、完整版本证据、固定查询、轮次参数、预热、每轮原生 QPS/RPS/MiB/s、查询计数、客户端 p50/p95/p99 延迟（ms）、墙钟耗时（ms）、正确性和仅限测量轮次的中位数汇总。每阶段 stdout/stderr 与正确性日志单独保存。

退出码：`0=全部通过`，`1=预检/负载/正确性/指标失败`，`2=参数错误`，`3=输出冲突或写入失败`，`130=中断`。失败结果不生成成功汇总，但保留此前所有轮次、非零返回码和日志。超时时只终止本脚本新建的进程组。

## 契约回归

```bash
python3 tests/test_contract.py
```

tier-A 回归覆盖成功、预热排除、精确正确性、旧版本、缺工具、认证、超时、运行失败、指标格式变化、输出冲突、参数上限、安全端点和单二进制布局，并直接解析观察到的 ClickHouse 26.8 原生输出；真实 fork 后代和取消时存活子进程回归显式验证有界返回、异常重抛和 PID 清理。假工具仅提供契约证据，不是服务 E2E。
