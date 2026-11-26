---
title: 训推社区 Host 洞察研究范围
type: skill-reference
---

# 六社区与三家模型生态

## 核心社区检查表

每项检查：官方身份入口、release、默认分支提交、重要 PR、issues/discussions、文档与集成代码。官方仓库/组织名可能变化，应从官网或已验证官方入口确认；历史记录是定位线索，不是当期核验。

| 社区 | 优先观察对象 | 避免误判 |
| --- | --- | --- |
| vLLM | executor/model runner、scheduler、tokenizer/detokenizer、sampling、KV connector、Host offload、模型与后端 | 主线不等于 release；GPU runner变化可能破坏插件；默认值变化不等于所有负载更优 |
| SGLang | tokenizer manager/router、overlap、HiCache、Host pool、MoE/稀疏/推测解码、CPU/NPU/ROCm | D2H微优化与JSON/网络端到端区分；recipe、CI和实测支持分别标注 |
| vLLM Ascend | vLLM版本配套、CANN/torch_npu、图模式、Host注册/VMM、P/D与模型组合 | A2/A3/A5分开；编译成功不等于硬件运行；不推断NUMA或全部量化路径 |
| TensorRT-LLM | 正式/候选分支、KVCM、Host executor/sampler、预处理、冷层I/O、模型target与known issues | 稳定与候选分支非线性；旧后端弃用、数据类型与依赖变化单列 |
| Mooncake | Transfer Engine、KV缓存/存储组件、分层/共享/淘汰、内存注册、RDMA、拓扑、connector、容错/元数据 | 分别指明组件、版本和调用链；存储容量不等于serving收益 |
| 3FS | 文件系统/分布式存储、元数据、一致性、I/O并行、数据放置、客户端、网络与恢复 | 不因模型厂商来源或通用读写接口就推断框架已接入；必须有具体集成证据 |

LMCache、NVIDIA Dynamo/NIXL 等仅在当期存在直接集成证据或重大部署影响时纳入。训练侧亦采用此边界：训练数据读取、checkpoint读写/恢复、异步rollout、权重同步、训练/推理共享资源等，需要可定位事件及Host影响；不要自动扩大为全训练行业综述。

## 三家 × 五类检查矩阵

分别建 DeepSeek、GLM（智谱/Z.ai）、Kimi（Moonshot/月之暗面）行；每家都独立核查下列五类。优先官网/API公告、官方模型组织与模型卡、技术报告、官方代码/版本，然后是第三方自身代码与发布说明。

| 类别 | 检查内容 | 判定边界 |
| --- | --- | --- |
| 模型发布 | 模型/权重版本、卡片与论文、API兼容、上下文、多模态、推理/工具调用、开放权重、许可证、调用与部署成本 | 正式、预览、论文与传闻分开；API alias与底层版本分开；价格需写单位、生效时间、适用调用路径 |
| Harness | agent loop、规划/工具执行、编码、多Agent、上下文、恢复、评测harness、训推闭环 | 需官方组件或明确模型适配证据；评测成绩不证明官方采用该框架 |
| 记忆 | 会话/长期记忆、检索、写入更新压缩遗忘、持久化与存储 | Agent语义记忆、会话日志、模型内部条件记忆、上下文缓存和KV cache分别命名 |
| 沙箱 | 容器/microVM/受限进程、文件/网络/权限、生命周期、快照、调度、观测 | “任意模型可用”不算特定模型集成；报错/逃逸自述不等于官方确认 |
| Agent配套 | SDK、协议、MCP/连接器、浏览器/计算机使用、搜索、检索、workflow、评测数据、追踪监控与运维 | 只保留对应模型有直接关系证据或明确落地影响的事件 |

每条事件写：事件时间与精度、项目/作者或组织、所属模型族、关系证据、状态、变化、应用场景、CPU/内存/存储/网络/沙箱影响、限制、直接来源。

关系标签使用“DeepSeek 官方 / GLM 官方 / Kimi 官方 / 第三方已验证适配 / 第三方自述 / 待验证”。“第三方已验证适配”须说明由谁、在何配置下验证；若仅作者测试则保留作者报告属性，不假称本次复现。

同一跨模型事件可合并一行，但给出各家的独立关联证据。无可靠增量的单元写标准无增量句，并列已查来源和缺口。首期背景只能计作窗口外基线。

## Host 机会筛选

沿事件链寻找可测变化：

- 调度/准入/过载、队列和取消恢复、请求编排。
- Tokenization、detokenization、协议解析、sampling/logits/logprobs、序列化。
- KV metadata、分层/换入换出、分页、Host pinned/shared pool、分配器/NUMA。
- CPU-GPU/NPU 重叠、同步消除、batching、DMA、注册内存与RDMA。
- 数据读取、checkpoint、存储写放大、一致性与恢复。
- 网络栈、前后处理、监控成本、编译与异构执行。

这不是必须填满的建议清单。将“已有上游修复”“仍缺实际E2E验收”“可新实现的能力”分开；优先调查能改变部署或研发决策的缺口。
