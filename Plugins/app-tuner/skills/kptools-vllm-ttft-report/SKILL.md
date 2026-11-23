---
name: kptools-vllm-ttft-report
description: 从已有 Prefill 日志或 TTFT 采集 JSON 生成离线 HTML，按请求、执行步和 TP 展示。用于已有日志直接看报告；不发请求、不部署或回滚打点。
compatibility: Python 3.10+; requires the installed sibling TTFT base skill (KPBot !181); JSON input also requires the sibling collect skill (KPBot !222).
---

# 离线 TTFT 报告

输入：用户指定的原始 Prefill 日志和请求 ID，或采集 skill 的交接 JSON，以及新 HTML 输出路径。无需 NPU。

1. 以当前 `SKILL.md` 所在目录的父目录为同级 Skill 根目录（仓内为 `Plugins/app-tuner/skills/`，安装后为宿主的 Skills 目录）。`TTFT_CLI` 是该根目录下 `kptools-vllm-ttft/scripts/ttft.py` 的绝对路径；运行前确认 `python3` 可用且该脚本是可读文件，否则报缺少基础包（KPBot !181）并停止。
2. JSON 输入时，`RECEIPT` 是交接单路径，`HANDOFF` 是同级根目录下 `kptools-vllm-ttft-collect/scripts/handoff.py` 的绝对路径；缺脚本则报缺少采集 Skill（KPBot !222）并停止。选择本轮已授权且与交接单对应的本地日志源，例如用户提供的副本或本轮采集已授权保存的快照；仅在没有此类来源时请用户提供，并在取得前停止。交接单的 `prefill_log` 仅记录采集来源，即使同机也不据此打开文件。将选定的来源按字节复制到本次报告独用的临时目录，设 `LOG` 为快照的绝对路径；采集服务和其他进程不写入该快照。运行 `python3 "$HANDOFF" verify "$RECEIPT" --log "$LOG"`。失败立即停止；成功后从校验结果获取 `REQUEST_ID`。后续检查和报告都只读同一份已校验的快照，不再读取原路径或截取快照。
3. 直接输入日志时，`LOG` 为用户提供的日志路径，请求 ID 可省略。若有多条 BREAKDOWN，保留原日志，另存包含一条 BREAKDOWN 及其完整关联记录的单请求片段，并将 `LOG` 改为片段路径。无法确定目标请求或完整边界时，停止并请用户提供片段；`--req` 不能替代截取。
4. 对最终选定的 `LOG` 运行 `python3 "$TTFT_CLI" check "$LOG"`；缺必需标记时停止，不使用 `--force` 绕过。如果直接输入的 `LOG` 在这一步才被发现有多条 BREAKDOWN，按上一步选定片段，更新 `LOG` 并对片段重新运行 `check`，再生成报告。
5. `HTML` 是用户指定的新报告路径。已知请求 ID 时运行 `python3 "$TTFT_CLI" report "$LOG" --req "$REQUEST_ID" --mode prefill -o "$HTML"`；未指定 ID 时完全省略 `--req`，由 CLI 选择唯一请求。确认输出存在；不覆盖已有报告或源日志。临时快照保留到报告命令结束。

输出：HTML 路径、请求、计量单位和缺失证据。缺失不是零；Python put 耗时不等同 Mooncake 原生传输耗时。
