# 阶段 7：性能验收与防回退

## 预注册门槛

- P0：目标路径 ratio≤1.00；其他 shape 最大回退≤3%。
- P1：目标 family 至少提升 10%；prepack 稳态不慢于对应 plain。
- P2：对照/优化≥1.5 时才可宣称“快 50%”。

不得看完结果后降低门槛。未达到更高目标时如实停在已通过级别。

## 测量

沿用阶段 1 的固定连续核、相同 NUMA 内存绑定、50 iterations × 3 processes、平均时间中位数和交错 A/B。每个结果附跨轮标准差；噪声大于差值则增加轮次或换空闲环境，不得挑最好样本。

同时比较原后端、plain BRGEMM 和 prepack/blocked BRGEMM；prepack 分开报告一次性 reorder 与稳态 execute。执行 kBlock 或线程扫描时固定其他变量，并归档所有候选而非只保留赢家。

## 全矩阵回退检查

每次优化跑全 shape 矩阵。任何超过 3% 的非目标回退必须用 verbose 对比 blocking、direct output、barrier、PackB 和线程数，修复或收窄 family/路由。特别检查：prepack 改变调度、parallel-K 取消 direct output、family 规则缩小搜索空间、单/多实例配置错配。

## 不接受的结论

- 单次或 min 时间；仅绑 NUMA node；A/B 使用不同核心。
- 未确认新库加载或 verbose 行为变化。
- 用 copy-only、内存下降、代码结构或 commit message代替 wall-time A/B。
- 算子级微小矩阵收益直接外推端到端。
- 把图融合、经典 KDNN 或基础设施改动全部归因为 BRGEMM kernel。
