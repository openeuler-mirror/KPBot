# Ascend + Kunpeng 域（A+K 异构协同）

同一套部署里**同时**涉及昇腾 NPU 与鲲鹏 CPU 的问题：P/D 两侧环境一致性、Host 侧 KV/内存分层与 offload、
NUMA 绑核与 NPU/HCCS 亲和、Host 侧预处理与 tokenizer 并行、RoCE 网卡亲和与 HCCL 联动。

## 边界判据

- **属于本域**：结论只有把 NPU 与 CPU 两侧放在一起看才成立（例：P/D 两侧 cpuset 配额不一致导致 decode 抖动；
  Host 侧 NUMA 跨 node 取 KV 拖慢 TTFT；RoCE 网卡与 NPU device 不在同一 NUMA node）。
- **不属于本域**：只在 NPU 侧成立（单卡/集群状态采集、CANN/算子、HCCL 通信自身）→ 归昇腾采集/算子侧；
  只在 CPU 侧成立（Kunpeng 上的算子与系统调优）→ 归 `code-optimizer` 的鲲鹏侧或 `microservice/`。

## 成员

| Skill | 职责 |
|---|---|
| [`kptools-ascend-pd-env`](kptools-ascend-pd-env/SKILL.md) | 采集并对比 vLLM-Ascend P/D 容器环境：内核、cgroup 配额、CPU/NUMA 拓扑、CANN 与驱动版本、npu-smi 快照、进程命令行与环境变量、Python 包清单。单容器出单环境报告，多容器或搭配已有采集包则自动对比并标差异——用于排查 P/D 两侧环境不一致、CANN/驱动/torch_npu/vLLM 版本漂移、HCCL/ASCEND 变量不同、cpuset/cgroup 配额不同 |

## 域内路由

- 怀疑 **P/D 两侧环境不一致**（版本漂移、cpuset/cgroup 配额、HCCL 变量、跨机跨时间快照对比）→ `kptools-ascend-pd-env`。
- 要在 A+K 上做**端到端调优**（PD 分离部署参数、Host 侧瓶颈归因、NUMA/亲和、HCCL 与 RoCE 联动）→ 待建主线 skill
  `ascend-kunpeng-pd-disaggregation-tuning`（从 SGLang-A+K 的实际调优过程沉淀，素材与压测口径需先确认）。

## 后续迁入 / 待议

- A+K 主线优化 skill（上面那条）。
- 6 个纯昇腾采集技能（`kptools-ascend-smi` / `-hccn` / `-profiling` / `-profiling-analysis` /
  `-profiling-analysis-nostack`）落在本域还是独立成 `ascend/` 采集域，另开 PR 讨论。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/ascend_kunpeng/**`
