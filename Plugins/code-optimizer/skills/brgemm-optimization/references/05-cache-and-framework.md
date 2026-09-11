# 阶段 5：缓存与框架集成

## JIT 与 primitive cache

在 MatMul 对象内按 M/N/K tail、accumulate、post-op、direct 等位组合缓存 kernel 变体。descriptor 使用逐字段语义比较；不得 `memcmp` 含 padding、容器地址的结构。post-op chain 必须完整参与相等判断，重复插入时让新引用指向已有对象。

框架侧 primitive cache key 至少包含 input shape、weight shape 和 weight layout。动态 M 变化时重建匹配 primitive；layout 切换时不得复用旧 kernel。

## 生命周期与成本

分别测量首次 prepack reorder/JIT/cache fill 与稳态 execute。只有常量 B 且多次 Run 才可能摊平一次性成本。报告 prepacked buffer、cache 和 scratch pool 的峰值驻留，并审查 mutex 是否覆盖计算区域。对照后端有自己的 prepack/cache 时，两侧必须同口径。

微小矩阵不能仅凭算子级倍率进入白名单；EP dispatch 和 JIT 固定开销可能吞掉收益。动态权重也不得通过缓存 packed 权重指针跨 Run 复用。

## 路由与 fallback

路由输入包括 dtype、B 是否常量、M/N/K 和实际线程数。已验证候选包括大 M 中 K 常量 B，以及修正 blocking 后的小 M 大 K family；微小矩阵默认保留原后端。fallback 放在运行时 shape 路由中，而不是仅靠全局开关。

任何“某 family 慢”的结论必须标注 commit 与 blocking 配置；启发式修复后重新收敛边界。

## 融合边界

batch×M 合并属于本 Skill。FusedTensordotMatMul 的图融合可以消除 Reshape/Transpose，但其端到端收益不能全部归因于 BRGEMM。共享 A 的多 MatMul 和 MHA 联合 GEMV 在来源项目中属于经典 KDNN 路径，除非重新验证，不作为 BRGEMM 已证实收益。融合条件不满足时必须有被测试的普通路径。
