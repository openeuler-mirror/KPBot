# scripts/fold_traces.py

`go tool pprof -traces` → FlameGraph collapsed 格式折叠器（纯 Python 标准库，
>= 3.8，只读输入，不执行任何 profile 数据）。

## 用法

```bash
# 折叠 + 校验输出（总权重 + top-10 帧份额）
python3 scripts/fold_traces.py traces.txt out.collapsed

# 之后用 flamegraph.pl 渲染
flamegraph.pl out.collapsed > full.svg
```

## CLI 契约（退出码）

| 退出码 | 含义 |
|--------|------|
| 0 | 成功；stdout 打印校验摘要（collapsed 栈数、总权重 ms、top-10 帧） |
| 1 | 输入解析成功但**一个 trace 块都没有**（典型：把 `-raw` 输出当 `-traces` 喂进来）——不静默产出空文件 |
| 2 | 用法错误（参数不是 2 个）/ 目标是目录 / 目标已存在（拒绝覆盖）/ 输入不存在 / 目标父目录不存在 |

## 处理的格式（实测形态验证）

- 块间虚线分隔行（`------` 前缀）
- 块内**叶子帧在前**，折叠时整块逆序为根帧在前（collapsed 要求）
- 权重单位兼容 `460ms` 与 `1.23s`（归一化到 ms）
- 帧名 `(inline)` 尾注剥离
- 同栈多块自动累加合并

## 校验用法（折叠后三道门的数据源）

fold 完成后 stdout 的 `total weight` 用于**权重门**（≈ pprof 头部 Total samples），
`top-10 flat frames` 用于**帧门**（与 `go tool pprof -top` 对账）。语义门（预期
消失/保留的函数逐项 grep）在 collapsed 文件上做。见 references/flamegraph-pipeline.md。

## 边界

- 只解析 `-traces` 文本格式；`-raw` 的位置列是数字 location ID，喂进来会走到
  exit 1（no trace blocks）
- 帧续行要求缩进 ≥12 列（`-traces` 实际格式）
- fold_traces.py 容忍 CRLF **输入**（universal newlines + rstrip 双保险），无需转换
- **脚本文件**如需 shebang 直执行必须 LF（`#!/usr/bin/env python3\r` 会报 `python3\r: No such file`）
