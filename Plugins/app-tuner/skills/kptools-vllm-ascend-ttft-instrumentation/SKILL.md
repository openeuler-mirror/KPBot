---
name: kptools-vllm-ascend-ttft-instrumentation
description: 只为 vLLM-Ascend 源码预检、应用或回滚 v0.27.1rc TTFT Python 打点。在用户明确指定 vLLM-Ascend 一侧时使用；不管理另一侧、服务启停或报告。
compatibility: Python 3.10+; unavailable without the installed sibling kptools-vllm-ttft base skill (KPBot !181) and a compatible serving revision.
---

# vLLM-Ascend TTFT 打点

输入：目标 Prefill 服务使用的 Python 解释器、目标部署身份、独立的绝对持久化状态目录。设 `TTFT_CLI` 为同级 `kptools-vllm-ttft/scripts/ttft.py` 的绝对路径，`PY` 为 serving Python；缺少原工具或解释器时停止，不下载其他版本替代。

预检：以当前 Skill 目录的父目录为同级根目录。检查 `TTFT_CLI` 为可读文件且 `"$PY" --version` 成功；缺失时报告缺少 KPBot !181 或 serving Python 并停止。使用下面的 `inspect` 核对实际加载的 vLLM-Ascend 版本与服务部署记录，`plan` 核对载荷内容；不能仅凭目录名或 Git HEAD 判定兼容。

1. 使用 `"$PY" "$TTFT_CLI" inspect --python "$PY"` 核对实际加载位置；执行 `"$PY" "$TTFT_CLI" plan --python "$PY" --repo ascend`。报告匹配结果、源码位置及将改动的文件；内容不匹配、路径不符或版本无法核实时停止。
2. 默认只出计划。仅在用户明确确认本次修改、操作者已停止目标服务后，执行 `"$PY" "$TTFT_CLI" apply --python "$PY" --repo ascend --state-dir "$STATE"`。`STATE` 每侧每次独立，不能复用或覆盖已有状态。
3. 回滚前核对该目录确属本次 vLLM-Ascend 应用、确认已停服并取得明确授权；运行 `"$PY" "$TTFT_CLI" rollback --state-file "$STATE/state.json"`。工具拒绝或发现后续编辑时停止，不强制覆盖。

输出：组件、源码和版本、计划结果、是否实际应用、状态文件路径（只在生成后提供）、未完成项。不修改 vLLM 或 Mooncake，不启停服务，不把计划通过说成运行验证。
