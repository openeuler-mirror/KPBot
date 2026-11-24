# 从工程问题选择报告来源

按下列问题核对已有产物，将实际发现映射到条目。一个条目可综合多项能力。技能名称描述可用路线；标记“实测”需要本轮数据。

| Profiling 问题 | KPBot 入口 | 写入条目的证据 |
| --- | --- | --- |
| CPU、内存、磁盘、网络是否饱和或发生错误？ | io-optimization、use-analysis、kptools-proc-collector、kptools-os-collector、kptools-network-collector、kptools-nmon | 指定进程或设备的利用率、排队、错误和观测窗口 |
| 时间集中在哪个线程、函数、库或调用者？ | kpbot-app-tuner、analyze-hotspot、analyze-caller-context | perf 事件、火焰图、自身与含子调用的占比，链接调用栈 |
| 停顿、缓存、地址翻译及访存延迟由什么贡献？ | kpbot-app-tuner 的 PMU 路线、kptools-devkit、arm-spe-analysis | 指令和周期、缓存与 TLB 事件、SPE 的 PC、数据来源和延迟；注明复用与丢样 |
| 热点代码中哪些机制可解释观测？ | analyze-architecture、arm-instructions-query、kunpeng-microarch、llvm-mca-analysis、register-pressure-analysis | 反汇编、依赖链和静态模型；单列其动态执行证据 |
| 能否定位到业务操作？ | kptools-py-spy、kptools-node-profiler、go-pprof-analysis、tf-profile-collector、faiss-perf-profile、vllm-ascend-profiling、Ascend trace 与沙箱时间线专项 | 算子、内核、语言栈和阶段耗时；注明并发重叠和关键路径 |

| 优化决策 | KPBot 入口 | 写入条目的实验 |
| --- | --- | --- |
| 先验证哪个假设？ | kpbot-app-tuner、kpbot-code-optimizer、interactive-optimizer、decide-optimization | 按热点证据、区分竞争解释的能力和变更成本排优先级 |
| 配置或布局能否解决？ | cpu-affinity-optimization、application-config-optimization、os-optimization、network-optimization、bios-optimization、performance-library-selection、compiler-optimization、accelerator-optimization、hardware-upgrade-analysis | 固定其余变量，比较亲和性、线程、库、构建或平台变体 |
| 应改哪段代码？ | hot-path-overhead-elimination、allocation-path-optimization、memory-access-optimization、prefetch-optimization、branch-elimination、loop-unrolling、operation-fusion、apply-vectorization、precision-transform | 将具体代码变化关联到预计变化的机制指标与业务指标 |
| 哪个专项适合该负载？ | brgemm-optimization、ort-kdnn-attention-opt 及 TensorFlow、Faiss、DiskANN、DPDK、Nginx、数据库专项 | 按真实算子、形状、索引或请求分布选择变体和回退路径 |
| 结果足以采用吗？ | verify-optimization、kernel-paired-benchmark、kernel-business-performance-validation、adversarial-review | 配对测试、噪声控制、正确性与端到端验收；记录实际结果对应的行动 |
