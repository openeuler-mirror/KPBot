---
name: kptools-vllm-ttft-collect
description: 为已经运行的 vLLM P/D 服务采集一次可关联的 TTFT 请求证据并生成 JSON 交接。用于要求发单请求、收 Prefill 日志或从既有日志整理采集证据；不启动服务或生成看板。
compatibility: Python 3.10+; requires the sibling kptools-vllm-ttft package from KPBot PR 181 or a compatible installed revision.
---

# TTFT 请求采集

输入：运行中的实例、用户指定的请求方式、Prefill 日志来源；或者已有的原始日志和请求 ID。使用已安装的同级 `kptools-vllm-ttft` 对应日志口径。

1. 实时采集前确认本轮目标实例和调用位置、使用现有认证方式，不回显凭据。记录发请求前的日志位置或时间边界。仅在用户授权请求后发一次，不重试、不启停服务。
2. 保存本轮原始 Prefill 日志范围，保留关联记录，不只 grep 一行。将返回的请求标识与日志中的 `[TTFT][BREAKDOWN] req=…` 对上。没有可靠关联或日志轮转导致范围不明时停止并报告缺失；不把 HTTP 200 当作 TTFT 或 KV 链路验证。已存在日志只读接受，标为 `existing_logs`。
3. 将 `HANDOFF` 设为本 skill 的 `scripts/handoff.py`，运行：

   `python3 "$HANDOFF" collect --log "$LOG" --request-id "$REQUEST_ID" --origin "$ORIGIN"`

   `ORIGIN` 为 `live_request` 或 `existing_logs`。将 stdout 保存到调用方指定的新 JSON 文件；不要覆盖原始日志。日志仍在增长时先在授权路径保存稳定快照。
   交接给报告的快照只允许一条 BREAKDOWN；已有日志有多条时，保留原始文件，另取目标请求的完整片段再生成交接单。

输出：交接 JSON、实例和采集范围、仍缺少的指标证据。此交接只确认文件和请求关联，不声明所有指标可用。
