---
name: kptools-vllm-ttft
description: >
  诊断 vLLM / vLLM-Ascend v0.27.1rc 的首 token 延迟（TTFT），预检已有
  Prefill 日志并生成按请求、执行步和 TP 展示的 HTML 报告。用于分析 TTFT
  breakdown、排队、KV cache 保存、wait_for_save 和 Mooncake Python put 耗时，
  或为匹配的 v0.27.1rc 源码规划、部署和回滚 Python TTFT 打点。
compatibility: Python 3.10+。离线分析使用标准库。部署需目标服务的 Python 环境，适用源码由 payload 内容校验。
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: vllm_ttft
---

# vLLM TTFT 诊断

## 工作流程

1. **已有日志**：直接运行 `check` 和 `report`。离线分析不需要 NPU、vLLM 或 Mooncake。
2. **缺少打点**：在目标服务容器中用实际 serving Python 执行 `inspect`，并对 vLLM 和 vLLM-Ascend 分别执行 `plan`。确认目标源码、兼容性检查结果和回滚位置后，获得用户明确同意，再执行 `apply`。服务停启由操作者通过原有方式完成。具体命令见[部署与回滚](references/deployment.md)。
3. **启动时无法导入 `mooncake.engine`**：在原服务启动目录先运行 `diagnose`，记录解释器和实际模块来源。按[部署与回滚](references/deployment.md)核对同名模块遮蔽。
4. **解读结果**：报告中的 `put (s)` 是 Python `backend.put` 的主机侧总耗时。按请求和执行身份关联证据，区分实测零和缺失值。字段边界见[报告口径](references/report.md)。

## 从日志生成报告

将 `SKILL_DIR` 设为本 `SKILL.md` 所在目录的绝对路径。日志和输出路径由用户指定。

```bash
python3 "$SKILL_DIR/scripts/ttft.py" check /path/to/log_prefill.log
python3 "$SKILL_DIR/scripts/ttft.py" report /path/to/log_prefill.log \
  --mode prefill -o /path/to/ttft_report.html
```

- `check` 统计 `BREAKDOWN`、`QUEUE` 和 `AscendStoreSave` 标记。检查失败时保留缺失项并排查日志完整性。
- 单请求日志自动选中。多请求日志先列出请求 ID 和 `BREAKDOWN` 次数，请用户选定后传 `--req <request-id>`。保留原始日志和能保留关联记录的请求日志切片。
- 返回报告路径、请求 ID、指标单位和缺失证据。正式报告保留原始关联记录，分享前核对并脱敏。

## 使用边界

- 打点只适用于 payload 内容校验通过的 v0.27.1rc 系列源码。`plan` 通过仅表示内容校验通过，服务验证需要真实请求。
- 在部署和回滚时保留本地改动与状态文件。遇到冲突或校验失败，保留原错误并停止。禁止强制应用、重置源码或覆盖原状态。
- 维护现有推理、存储及传输调用。服务配置、P/D 部署和 Mooncake 构建由现有运维流程处理。算子或设备级分析使用 `kptools-ascend-profiling`。

[离线验证入口](cases/offline-contract/entrypoint.sh)覆盖两套 payload 的预检、应用、回滚和报告生成。[源码来源与许可](references/provenance.md)记录版本和再分发范围。
