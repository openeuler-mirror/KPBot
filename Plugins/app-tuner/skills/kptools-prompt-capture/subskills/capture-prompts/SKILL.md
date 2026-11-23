---
name: capture-prompts
description: >
  在已接入采集的 proxy 上按授权和时限采集 prompt，确认 worker 开关及写盘状态，交付采集分段。
---

# Prompt 采集

## 操作流程

1. 确认指定 proxy 已接入且健康。按 [配置说明](../../references/capture-and-analysis.md#配置准备) 确定绝对配置路径、时长、保存位置、磁盘预算和会话范围。默认仅采元数据；prompt 全文、输出全文和 golden 分别确认，golden 还须有后端兼容性证据。
2. 使用 proxy 所用的 Python 解释器，在相同容器或 PID 命名空间准备配置。新配置按需使用 `init` 与 `load`。开启前用 `show` 逐项对照授权：普通请求看 `text_rate`、`output`、`gold_rate`；跟踪会话看 `follow_sessions`、`follow_new`、`follow_output`、`follow_gold`。跟踪会话会记录请求全文，即使 `text_rate=0`。配置超出授权时不执行 `on`，先准备已确认的配置再核对；不接管其他任务的活动窗口。
3. 按本次授权窗口开启采集。仅采元数据时，用 `--metadata-only` 在写配置前再次检查。以下 `$S` 为父 skill 目录，`$CFG` 为配置绝对路径，`$DURATION` 为已确认的正秒数。

```bash
$PY "$S/scripts/capturectl.py" --config "$CFG" on --duration "$DURATION" --metadata-only
$PY "$S/scripts/capturectl.py" --config "$CFG" status --json
```

检查预期 worker 数、版本、有效开关和实际文件增长。返回码 4 表示尚未确认生效，不能只依据配置文件判断。到期或发生持续写盘错误、预算不足时，对本轮采集执行 `off`，再查状态。等待在途记录及写盘收尾；超时记录缺失，不重启 proxy。

确有正文权限时，按确认范围设置普通采样或会话跟踪，再不带 `--metadata-only` 启动。自动跟踪数是每个 worker 的配额，不保证会话完整。

## 输出

交付受限目录中的 `FORMAT=1` 分段、采集时间、配置口径、写入和丢弃情况及实际关闭状态。若已有 `run.json`，记录本轮状态。没有流量时如实记录，采集关闭不等于代码恢复。
