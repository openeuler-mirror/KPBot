---
name: kptools-vllm-ttft-report
description: 从已有 Prefill 日志或 TTFT 采集 JSON 生成离线 HTML，按请求、执行步和 TP 展示。用于已有日志直接看报告；不发请求、不部署或回滚打点。
compatibility: Python 3.10+; requires the installed sibling TTFT base skill (KPBot !181); JSON input also requires the sibling collect skill (KPBot !222).
---

# 离线 TTFT 报告

输入：用户指定的原始 Prefill 日志和请求 ID，或采集 skill 的交接 JSON，以及新 HTML 输出路径。无需 NPU。

1. 以当前 `SKILL.md` 所在目录的父目录为同级 Skill 根目录（仓内为 `Plugins/app-tuner/skills/`，安装后为宿主的 Skills 目录）。`TTFT_CLI` 是该根目录下 `kptools-vllm-ttft/scripts/ttft.py` 的绝对路径；运行前确认 `python3` 可用且该脚本是可读文件，否则报缺少基础包（KPBot !181）并停止。
2. JSON 输入时，`RECEIPT` 是用户提供的交接单路径，`HANDOFF` 是该根目录下 `kptools-vllm-ttft-collect/scripts/handoff.py` 的绝对路径；缺脚本则报缺少采集 Skill（KPBot !222）并停止。运行 `python3 "$HANDOFF" verify "$RECEIPT"`，跨机器时附加 `--log "$LOCAL_COPY"`，其中 `LOCAL_COPY` 是用户提供的同一份日志的本地副本。校验失败立即停止；成功则从输出获取 `LOG` 和 `REQUEST_ID`。直接输入日志时，两者来自用户（请求 ID 可不提供）。
3. 运行 `python3 "$TTFT_CLI" check "$LOG"`；缺必需标记时停止，不默认用 `--force`。若日志有多条 BREAKDOWN，保留原日志并另外截取一次请求的完整片段；`--req` 不会消除多条 BREAKDOWN。
4. `HTML` 是用户指定的新报告路径。已知请求 ID 时运行 `python3 "$TTFT_CLI" report "$LOG" --req "$REQUEST_ID" --mode prefill -o "$HTML"`；未指定 ID 时完全省略 `--req`，由 CLI 选择唯一请求。确认输出存在；不覆盖已有报告或源日志。

输出：HTML 路径、请求、计量单位和缺失证据。缺失不是零；Python put 耗时不等同 Mooncake 原生传输耗时。
