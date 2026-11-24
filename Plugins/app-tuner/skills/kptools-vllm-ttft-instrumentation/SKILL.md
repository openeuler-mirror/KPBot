---
name: kptools-vllm-ttft-instrumentation
description: 只为 vLLM v0.27.1 源码预检、应用或回滚 TTFT Python 打点。在用户明确指定 vLLM 一侧时使用；不管理另一侧、服务启停或报告。
compatibility: Python 3.10+; requires the sibling kptools-vllm-ttft package (KPBot !181) with interrupted-apply recovery.
---

# vLLM TTFT 打点

输入：目标 Prefill 服务使用的 Python 解释器、目标部署身份、独立的绝对持久化状态目录。设 `TTFT_CLI` 为同级 `kptools-vllm-ttft/scripts/ttft.py` 的绝对路径，`PY` 为 serving Python；缺少原工具或解释器时停止，不下载其他版本替代。

预检：以当前 Skill 目录的父目录为同级根目录，由此解析 `TTFT_CLI` 的绝对路径。确认它是可读文件，且 `"$PY" --version` 成功；缺失时报告缺少基础包（KPBot !181）或 serving Python 并停止。

1. 在服务启动目录运行 `"$PY" "$TTFT_CLI" inspect --python "$PY"`，取得 `modules.vllm.origin`；`version` 只是 Python 版本。用同级 `kptools-vllm-ttft/references/deployment.md` 中的安装记录核对命令，使用同一个 `PY`，取 `DIST=vllm`、`ORIGIN=modules.vllm.origin`。可编辑安装或源码构建用与该路径对应的部署构建记录核实版本。该指南的「不是目标 v0.27.1rc 基线时停止」仅适用于 vLLM-Ascend（`DIST=vllm-ascend`）；本流程以 `vllm` 分发包版本 `0.27.1`（上游 `v0.27.1`，payload 基线 `6e448d0ea9bf`）为门禁，不要求 RC。确认后再运行 `"$PY" "$TTFT_CLI" plan --python "$PY" --repo vllm` 核对载荷内容。版本、路径或内容任一未核实就停止。
2. 默认只出计划。仅在用户明确确认本次修改且操作者已停止目标服务后才可应用。若计划报告任何文件已包含打点（即使全部已打点），不得直接 `apply`：本轮 `state.json` 不记录先前已打点的文件。先按部署指南核对原 `state.json` 对应同一部署、源码路径及先前打点文件；取得明确回滚授权后用原状态安全回滚。无法核实原状态、原状态未覆盖先前打点文件或回滚失败时停止，交由操作者检查，不强制覆盖。
3. 停服期间在同一服务启动目录重跑第 1 步的 `plan`，确认仍是已核实的源码路径、内容检查通过且已打点文件数为零；否则停止，不对混合状态执行 `apply`。从复检到应用结束，确保没有其他部署或进程写入该源码。满足上述条件后，使用支持写源码前保存 `state.json` 的基础包执行 `"$PY" "$TTFT_CLI" apply --python "$PY" --repo vllm --state-dir "$STATE"`；不使用先写源码的旧版本。`STATE` 每侧每次独立。若中断，保持停服并保留该状态，不重试应用或启动服务；由操作者按第 4 步授权回滚。
4. 回滚前核对该目录确属本次 vLLM 应用、确认已停服并取得明确授权；运行 `"$PY" "$TTFT_CLI" rollback --state-file "$STATE/state.json"`。工具拒绝或发现后续编辑时停止，不强制覆盖。

输出：组件、源码和版本、计划结果、是否实际应用、状态文件路径（只在生成后提供）、未完成项。不修改 vLLM-Ascend 或 Mooncake，不启停服务，不把计划通过说成运行验证。
