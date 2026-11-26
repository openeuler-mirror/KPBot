---
name: kptools-pypto-monitor-viz
description: >
  Use when rendering PyPTO Serving PD_DIAG v2 prefill logs as standalone
  HTML or investigating blank phase and TP cells.
license: MulanPSL-2.0
compatibility: python3 >= 3.8 纯 stdlib；PD_DIAG v2 prefill 日志
metadata:
  skill_type: tool_assisted
  category: serving
  domain: pypto_serving
  version: "1.0.0"
---

# 从 PyPTO 日志生成 HTML 看板

优先使用采集 checkout 的 tools/pd_metrics_report.py，让读写端使用同一事件契约。技能内的 [标准库报表脚本](scripts/pd_metrics_report.py) 是当前 PD_DIAG v2 契约的备用副本（同步自 pypto_serving 仓库 tools/pd_metrics_report.py——上游升级时以采集 checkout 的版本为准，此副本按需更新）。

~~~bash
python tools/pd_metrics_report.py prefill.log -o prefill-report.html
python tools/pd_metrics_report.py prefill-parent.log prefill-worker.log -o prefill-report.html
~~~

脚本可读多份日志并输出自包含 HTML。存在坏记录时写出含明细的 HTML，并返回退出码 2。导出 0 个请求时查 P 端 PYPTO_SERVING_PD_DIAG、输入文件、version=2、role=prefill 和 req_resolve。vLLM 附件脚本的 v1 是另一套契约。

## 表格与验收

- 每个请求先显示 Step 0，汇总入口、排队、Prefill TTFT 和调度。请求列只显示短 ID，悬停和筛选保留全名。Step 1…N 是请求内序号，原始 step ID 留在说明里。
- TP 在设备 forward 前。只用实际 device.forward 或 device.sampling 证据展开 rank。Step、Host sampling、prepare 及主进程交接数据跨 TP 行合并。共享 Host forward 与分组总耗时留在日志里。
- 按 host/pid/run_id 确定时钟域，使用实际 request_id、request_ids、step_id 和 tp_rank 关联。同域重叠区间取并集。设备只有 duration 时保留独立观测；歧义与不适用留空。共享批次耗时不分摊。转义日志中的文本。
- 核对请求数、步数、rank 数，并抽查 native device_wall。检查采样只出现在执行采样的 rank，或者在 Host 共享格。无 copy 的传输子项留空。
- 整条请求的连续性以原始区间到 prefill.ready 的覆盖为准。可见列隐藏了一些外框，异步区间也可能重叠。交付 HTML 和输入日志，注明测试内核、测试 logits、替身 D/KV 等范围。把成功生成与浏览器已打开分别说明。

采集边界见同目录的 kptools-pypto-monitor。
