---
tags: [vllm-fastokens-segment-cache-migration, inspect-tokenizer-backend]
---

请用已有证据核对切换后的三个 API 进程。通过回放的 `report.json` 显示 `arms.on.fastokens_version` 是 `0.3.6`。三个 PID 的开关都为 `1`，预算都是 64 MiB，均加载了同名的 fastokens 原生库。运行实例的镜像摘要已在部署记录中和镜像构建清单对应：PID 101 装的是 `0.3.6`，PID 102 装的是 `0.3.5`，PID 103 找不到对应的构建清单。不要改服务或重新测量，逐个说明是否与回放目标一致。
