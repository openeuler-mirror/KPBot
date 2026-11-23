# 来源与适用范围

本 skill 依据 Ray 公开文档编写，真实执行用例固定为 Ray 2.52.0。下表的 `latest` 页面会更新，现场操作先确认目标版本和已安装 API；当前版本不支持的接口按采集指南降级，不将新文档默认值套用到旧版本。

| 来源 | 本 skill 使用的知识 | 边界 |
|---|---|---|
| [Ray Data performance tips](https://docs.ray.io/en/latest/data/performance-tips.html) | batch、并行度、资源与内存的调优观察方向 | 具体参数/默认值随版本变化，不作为固定配置模板 |
| [Monitoring your workload](https://docs.ray.io/en/latest/data/monitoring-your-workload.html) | Dataset stats、operator 及运行监控 | 不承诺兼容任意版本的 stats 文本解析 |
| [Ray resources](https://docs.ray.io/en/latest/ray-core/scheduling/resources.html) | 逻辑资源请求与物理 CPU 隔离的区别 | 不以 num_cpus 推算物理利用率 |
| [Transforming data](https://docs.ray.io/en/latest/data/transforming-data.html) | map/map_batches、数据表示与转换成本 | 选择依赖实际负载与语义 |
| [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) | cpu.max、有效 cpuset、层级约束 | 只读采集，不修改控制器；namespace 可隐藏祖先 |

环境采集、比较记录格式、结论置信度与实验协议属于本 skill 的工程约定，不是 Ray 官方强制规范。对推断必须给出当前任务的证据；工具测试仅证明已覆盖的逻辑，不证明真实负载或硬件收益。
