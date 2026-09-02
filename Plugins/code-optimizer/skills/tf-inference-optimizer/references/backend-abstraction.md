# backend 抽象（四层分层）

优化动作按四层落地，接口清晰、互不耦合。换后端库或换平台时，只动「后端库层」和部分「kernel 层」，编排层与图优化模式匹配逻辑不变。

```
┌─ 编排层 ──────┐  tf-inference-optimizer
│              │  状态机 + 门控 + 候选池路由 + 报告（与 backend 无关）
├─ 图优化层 ────┤  tf-graph-optimize
│              │  Grappler remapper / graph_optimizer PatternRewriter（模式匹配、门控注册）
├─ kernel 层 ───┤  tf-kernel-optimize
│              │  REGISTER_OP / REGISTER_KERNEL_BUILDER / ShapeFn / BUILD 链接
└─ 后端库层 ────┘  KDNN / ANNC / NEON / SVE
                grouped GEMM、post-op（bias/激活/BN）、prepack、layer_norm 等能力
```

## 1. 各层职责与接口边界

| 层 | 依赖 | 对上层暴露 |
|---|---|---|
| 编排层 | 不依赖任何 backend | 状态机、门控、路由决策 |
| 图优化层 | 依赖 TF Grappler 接口（NodeDef/GraphDef/PatternRewriter） | 融合模式匹配 + 门控注册 |
| kernel 层 | 依赖 TF kernel 框架（OpKernel/AsyncOpKernel/ShapeFn） | 注册 op/kernel 符号 |
| 后端库层 | 依赖具体硬件/库（KDNN/NEON/SVE） | 计算能力：grouped GEMM、post-op、prepack、layer_norm |

## 2. 后端库层能力抽象（kernel 层只调抽象能力）

后端库层对上层只暴露「能力」，不暴露实现细节。kernel 层通过抽象能力调用，而非直接写死某后端 API：

| 抽象能力 | 当前后端实现 | 换后端时替换点 |
|---|---|---|
| grouped GEMM（一次调度多 role） | KDNN::IndependentGroupedGemm | 换其它 BLAS / 自研库 |
| post-op（bias/激活/BN 折叠） | KDNN post-op | 换 Eigen functor / 自研 |
| prepack（权重离线打包） | KDNN prepack | 换其它打包格式 |
| layer_norm / softmax / rnn | KDNN 对应接口 | 换 NEON/SVE 实现 |

## 3. 换后端 / 换平台的影响面

| 变更 | 影响层 | 编排/图优化层是否改动 |
|---|---|---|
| KDNN → 其它 BLAS/自研库 | 后端库层 + 部分 kernel 层 | 否 |
| aarch64 → 其它架构 | 后端库层 + 部分 kernel 层（SIMD 指令） | 否（图优化模式匹配与后端无关） |
| 新增融合规则 | 图优化层 + kernel 层 + 后端库层 | 仅候选池路由，状态机/门控不变 |

## 4. 落地约束

- 图优化层（remapper/rewriter）只负责「识别模式 → 生成 fused 节点」，不内联后端实现。
- kernel 层只负责「注册 + 调度后端抽象能力」，不重复实现 GEMM/激活等数学逻辑。
- 后端库层能力缺失时（如某平台无 grouped GEMM），kernel 层提供 fallback（如逐 role 独立 kernel），保证可运行性。
