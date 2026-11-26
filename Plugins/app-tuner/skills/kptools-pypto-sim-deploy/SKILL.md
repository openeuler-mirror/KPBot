---
name: kptools-pypto-sim-deploy
description: >
  Use when running PyPTO Serving with a2a3sim or a CPU test executor,
  checking simulated kernels, or validating P-node timing with test
  doubles.
license: MulanPSL-2.0
compatibility: a2a3sim 模拟器或 CPU test executor；PyPTO Serving 环境
metadata:
  skill_type: deployment
  category: serving
  domain: pypto_serving
  version: "1.0.0"
---

# PyPTO Serving 模拟器冒烟

先确认验证目标。模拟器算子对拍、真实 Serving 配测试模型、真实模型生成分别记录证据。测试产出注明 logits、D 端、KV 与并行拓扑哪些使用替身。

- 内核级验证从 pypto-lib 的 hello_world.py --sim 开始，检查编译成功和对拍 PASS。
- 服务级验证启动真实 Serving HTTP、Scheduler 和 Worker，运行测试内核并发送一条请求。需要 Prefill 日志时选 P 节点入口和单步／多步请求。
- 双逻辑 rank 可验证设备 timing 的关联和 TP 布局。模型 TP collective 需具有真实 collective 的 workload。
- 读 [模拟器运行手册](references/deployment.md) 获取环境、替身接入、设备采样与验证步骤。CannLab 链路操作由仓内 `kptools-cannlab-env` skill 覆盖，此处不重复。

真实 Ascend 和模型权重的部署用 kptools-pypto-deploy。采集用 kptools-pypto-monitor，生成看板用 kptools-pypto-monitor-viz。
