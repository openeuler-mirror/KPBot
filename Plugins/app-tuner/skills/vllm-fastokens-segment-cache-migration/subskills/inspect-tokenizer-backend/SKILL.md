---
name: inspect-tokenizer-backend
description: >
  检查 vLLM 当前生效的分词后端、段缓存配置和迁移条件；也用于切换后或回退后的只读核对。
---

# 当前分词后端分析

## 操作流程

`$PY` 为服务使用的 Python 解释器，`$S` 为安装后的父技能目录。容器化部署时，在指定的服务容器内运行。`MODEL` 取服务的 `--tokenizer` 值，未设置时取 `--model` 值。

```bash
"$PY" "$S/scripts/fastokens_segment_cache.py" probe --model "$MODEL" --out "$OUT"
```

按实际配置增加 `--tokenizer-mode` 和 `--revision`。Linux 上可重复传入 `--pid` 选定服务进程；只查进程或版本时可省略 `--model`。`--offline` 跳过 PyPI 查询；已有目标版本时同时传入 `--target-version`。

1. 核对 Python 或 Rust 前端、版本与受检 PID。有进程证据时，逐个核对 `VLLM_USE_FASTOKENS`、`FASTOKENS_SEGMENT_CACHE` 和已加载的 fastokens 原生库。开关为 `1` 但未观察到库时，不能认定 fastokens 已生效。查不到进程证据时，`load_current` 只说明当前 shell 的加载结果，不代表运行中的服务。
2. 根据 `verdict` 说明适用性及原因。Rust 前端不适用 Python 包迁移；找不到发行版本时列为待确认，可用用户提供的官方 wheel 确定目标版本。探测结论不代替候选加载和回放验证。
3. 用户完成切换后，以通过回放的 `report.json` 中 `arms.on.fastokens_version` 为准，逐个核对预期 API 进程的开关、段缓存预算及实际部署版本。可用运行中实例的镜像摘要关联构建清单中的 fastokens 版本，或核对进程实际加载的原生库摘要与已验证候选构建的摘要。库名和当前 shell 的包版本不能单独证明运行中的进程用了候选版本。逐个 PID 记录「一致」「不一致」或「无法核实」，缺少回放或部署证据时不判为一致。
4. 用户完成回退后，对照快照中的变量与库哈希，说明一致、不一致或无法确认的项目。

## 输出

交付 `probe.json` 和实际后端、证据来源及迁移判定。候选加载、切换后和回退后的探测写入不同目录，保留迁移前的结果。
