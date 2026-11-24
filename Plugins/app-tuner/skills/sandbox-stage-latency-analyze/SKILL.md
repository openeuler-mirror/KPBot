---
name: sandbox-stage-latency-analyze
description: 将 CubeSandbox evidence 与 startup-latency 结果解析为 Cubelet/Shim/CubeMaster 阶段统计、CreateContainer 详细子阶段和父子拓扑；输出 analysis.json、stage-latency.csv、details JSONL 与摘要。适用于已有采集证据的离线阶段归因，不采集 workload、不渲染图表。
---

# 沙箱阶段延迟分析

## 单点职责

复用 `analyze_c50_profile.mjs`，关联基准请求和组件日志，保留 CreateContainer 在同一分析器中的详细子阶段解析，不另设 Skill。

## 可执行入口

```bash
bash scripts/run.sh PROFILE_DIR [OUTPUT_DIR]
```

`PROFILE_DIR` 必须包含 `evidence/` 和 `startup-latency/`；`OUTPUT_DIR` 默认为 `PROFILE_DIR`。入口通过共享实现一次性发布：

- `analysis.json`：benchmark、Cubelet/Shim/CubeMaster、Host 与 CreateContainer 统计；
- `stage-latency.csv`：阶段父子、顺序、containment、additivity 和来源字段；
- `create-container-details.jsonl`：逐实例 CreateContainer 观测；
- `analysis-summary.md`：请求数、热点阶段和解读边界。

## 成功判据

退出码为 0，JSON 可解析且含 benchmark，CSV 含完整 `api.create` 拓扑，JSONL 与摘要均存在。只分析已有证据，不调用采集器、SVG 或 report。

## blocked / 失败

- 缺共享实现、Node/Python、输入目录或 case summary：非零退出并说明缺失项；缺共享实现退出 2。
- `evidence/evidence-manifest.json` 存在时必须为 `status: success`；failed 或损坏的 manifest 在调用 analyzer 前阻止发布并保留证据。无 manifest 的旧格式继续兼容。
- 必需日志缺失、非空行不是合法 JSON object、基准 JSON 损坏或 analyzer 输出契约不完整：失败，不用空统计掩盖坏日志，也不发布部分结果集。
- evidence 中有轮转 warning 时可生成结果，但摘要与下游必须保留该风险。

阶段不可加性、跨进程时钟、部分观测和 warmup 口径见[共享判读约定](../sandbox-timeline-profile/references/interpretation.md)。
