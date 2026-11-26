---
name: kptools-pypto-deploy
description: >
  Use when starting or troubleshooting PyPTO Serving without a simulator
  on real Ascend hardware, including ordinary serving, P/D, Router setup,
  and CannLab NPU deployment.
license: MulanPSL-2.0
compatibility: Ascend NPU 真机环境（PyPTO Serving + torch_npu）；CannLab 链路
metadata:
  skill_type: deployment
  category: serving
  domain: pypto_serving
  version: "1.0.0"
---

# PyPTO Serving 真机部署

使用实际 Ascend 设备、受支持的平台和真实模型权重。以当前 checkout 的 docs/user-guide/pd-disaggregation.md、对应模型文档及 CLI 校验为准。

- 普通 Serving 以 pypto-serving 启动，验收 /health、真实生成响应和模型身份。
- P/D 分别启动 P、D 和 Router。核对设备数、权重、模型 ABI、各自角色和端口。P/D 共用 endpoint JSON，先确认两侧就绪，再经 Router 发生成请求。
- 读 [真机启动手册](references/deployment.md) 获取启动模板、设备与模型配置、调参和 CannLab NPU 排障。CannLab 链路操作（SSH 隧道/remote_tool.sh/同步覆盖/pyfix312 ABI 边界/dev/shm/AICPU 目录等）由仓内 `kptools-cannlab-env` skill 覆盖，此处不重复——遇到链路问题时路由该 skill。
- 资源不足时报告实际设备数或缺失依赖。用户接受模拟验证时，转用 kptools-pypto-sim-deploy。

采真实 P 节点日志用 kptools-pypto-monitor，已有日志要出 HTML 用 kptools-pypto-monitor-viz。
