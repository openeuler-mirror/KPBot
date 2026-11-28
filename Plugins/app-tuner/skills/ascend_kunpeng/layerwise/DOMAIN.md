# Ascend P2P Layerwise 子域

本子域聚焦昇腾 vLLM-Ascend PD 分离下的 P2P Layerwise / KV 逐层传输，不负责通用 P/D 环境采集或 profiling trace 分析。

## 边界判据

- 属于本域：`MooncakeLayerwiseConnector`、`MultiConnector`、PD KV 逐层传输、`AscendStoreConnector` 非 layerwise 组合、`remote_block_ids` 映射异常、layerwise 精度验证。
- 不属于本域：
  - P/D 两侧环境差异、CANN/驱动/torch_npu 版本漂移 → `kptools-ascend-pd-env`
  - 已有 profiling trace，需要拆 step / 算占比 / 找空泡 → `kptools-ascend-profiling-analysis`
  - 纯 CPU 侧算子、系统或微服务调优 → `code-optimizer` 或 `app-tuner` 的其他子域

## 成员

| Skill | 职责 |
|---|---|
| [`ascend-pd-p2p-layerwise-workflow`](ascend-pd-p2p-layerwise-workflow/SKILL.md) | 一键适配编排：串起准入、配置、验证、排障和 closeout，只在多阶段任务中使用 |
| [`ascend-pd-p2p-layerwise-admission`](ascend-pd-p2p-layerwise-admission/SKILL.md) | 判断能不能开：模型/KV cache 契约、6 个 go/no-go gate、支持矩阵、已验证/未验证边界 |
| [`ascend-pd-p2p-layerwise-config`](ascend-pd-p2p-layerwise-config/SKILL.md) | 怎么配：公开 Mode A、MultiConnector 子项、Store/Mooncake 非 layerwise、配置合法性检查 |
| [`ascend-pd-p2p-layerwise-verification`](ascend-pd-p2p-layerwise-verification/SKILL.md) | 怎么验证：证据采集、启动与日志检查、P2P 传输、精度和失败注入 |
| [`ascend-pd-p2p-layerwise-troubleshooting`](ascend-pd-p2p-layerwise-troubleshooting/SKILL.md) | 出问题怎么查：启动失败、block 映射、FirstWin 冲突、超时、社区版本门禁 |

## 域内路由

- 用户要求端到端适配或一键流程 → `ascend-pd-p2p-layerwise-workflow`
- 只判断目标形态、模型/KV cache 结构是否可以开 → `ascend-pd-p2p-layerwise-admission`
- 已确认可开，需要生成或校验配置 → `ascend-pd-p2p-layerwise-config`
- 已启动，需要验证传输、精度或失败处理 → `ascend-pd-p2p-layerwise-verification`
- 启动失败、请求超时、`IndexError`、输出异常 → `ascend-pd-p2p-layerwise-troubleshooting`

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/ascend_kunpeng/layerwise/**`
