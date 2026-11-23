---
name: kptools-vllm-ttft-report
description: 从已有 Prefill 日志或 TTFT 采集 JSON 生成离线 HTML，按请求、执行步和 TP 展示。用于已有日志直接看报告；不发请求、不部署或回滚打点。
compatibility: Python 3.10+; requires the sibling kptools-vllm-ttft package from KPBot PR 181 or a compatible installed revision.
---

# 离线 TTFT 报告

输入：用户指定的原始 Prefill 日志和请求 ID，或采集 skill 的交接 JSON，以及新 HTML 输出路径。无需 NPU。

1. 若接收 JSON，用同级 `kptools-vllm-ttft-collect/scripts/handoff.py` 执行 `python3 "$HANDOFF" verify "$RECEIPT"`。校验失败时停止；路径是采集端绝对路径，跨机器需将同一份日志复制到本机，并使用 `--log "$LOCAL_COPY"` 验证字节一致。拿验证输出中的日志路径和请求 ID。
2. 设 `TTFT_CLI` 为同级 `kptools-vllm-ttft/scripts/ttft.py`。运行 `python3 "$TTFT_CLI" check "$LOG"`，缺必需标记时报告并停止，不默认用 `--force`。底层报告要求日志只有一条 BREAKDOWN；多条时保留原日志，另取一次请求的完整日志片段，不仅靠 `--req` 过滤。
3. 运行 `python3 "$TTFT_CLI" report "$LOG" --req "$REQUEST_ID" --mode prefill -o "$HTML"`。确认输出存在；不覆盖已有报告或源日志。单请求且无显式 ID 时可让原 CLI 自动选择。

输出：HTML 路径、请求、计量单位和缺失证据。缺失不是零；Python put 耗时不等同 Mooncake 原生传输耗时。
