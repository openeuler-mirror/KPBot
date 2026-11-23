---
name: kptools-vllm-ascend-ttft-instrumentation
description: 只为 vLLM-Ascend 源码预检、应用或回滚 v0.27.1rc TTFT Python 打点。在用户明确指定 vLLM-Ascend 一侧时使用；不管理另一侧、服务启停或报告。
compatibility: Python 3.10+; requires the sibling kptools-vllm-ttft package (KPBot !181) with interrupted-apply recovery.
---

# vLLM-Ascend TTFT 打点

输入：目标 Prefill 服务使用的 Python 解释器、目标部署身份、独立的绝对持久化状态目录。设 `TTFT_CLI` 为同级 `kptools-vllm-ttft/scripts/ttft.py` 的绝对路径，`PY` 为 serving Python；缺少原工具或解释器时停止，不下载其他版本替代。

预检：以当前 Skill 目录的父目录为同级根目录。检查 `TTFT_CLI` 为可读文件且 `"$PY" --version` 成功；缺失时报告缺少 KPBot !181 或 serving Python 并停止。

1. 在服务启动目录运行 `"$PY" "$TTFT_CLI" inspect --python "$PY"`，取得 `modules.vllm_ascend.origin`；`version` 只是 Python 版本。按同级 `kptools-vllm-ttft/references/deployment.md` 的安装记录核对命令，使用同一个 `PY`，取 `DIST=vllm-ascend`、`ORIGIN=modules.vllm_ascend.origin`。可编辑安装或源码构建用与该路径对应的部署构建记录核实版本。确认实际加载的是目标 v0.27.1rc 基线后，再运行 `"$PY" "$TTFT_CLI" plan --python "$PY" --repo ascend` 核对载荷内容。版本、路径或内容任一未核实就停止。
2. 默认只出计划。仅在用户明确确认本次修改、操作者已停止目标服务后，使用支持写源码前保存 `state.json` 的基础包执行 `"$PY" "$TTFT_CLI" apply --python "$PY" --repo ascend --state-dir "$STATE"`；不使用先写源码的旧版本。`STATE` 每侧每次独立。若中断，保持停服并保留该状态，不重试应用或启动服务；由操作者按第 3 步授权回滚。
3. 回滚前核对该目录确属本次 vLLM-Ascend 应用、确认已停服并取得明确授权；运行 `"$PY" "$TTFT_CLI" rollback --state-file "$STATE/state.json"`。工具拒绝或发现后续编辑时停止，不强制覆盖。

输出：组件、源码和版本、计划结果、是否实际应用、状态文件路径（只在生成后提供）、未完成项。不修改 vLLM 或 Mooncake，不启停服务，不把计划通过说成运行验证。
