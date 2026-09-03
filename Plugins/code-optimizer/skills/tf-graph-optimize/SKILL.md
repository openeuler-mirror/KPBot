---
name: tf-graph-optimize
description: 编写 TF 图优化规则（Grappler remapper / graph_optimizer PatternRewriter），实现算子融合（MatMul、QKV、激活、bias 等），含 matcher 模式匹配、门控注册和匹配验证。触发：图优化、remapper、rewriter、pattern matcher、算子融合、MatMul 融合、QKV 融合、BiasAdd 融合、grappler、GraphDef、融合规则。
---

# TF 推理优化 - 图优化实施（remapper / rewriter）

图优化把多个独立 op 重写成单个 fused op，减少 kernel 调度和内存往返。本 TF 2.20 fork 有两层图优化，改动前先分清落在哪层。

## 1. 两层图优化架构

| 层 | 文件 | 用途 | 典型案例 |
|---|---|---|---|
| Grappler remapper | `tensorflow/core/grappler/optimizers/remapper.cc` | 通用算子融合 | MatMul→BiasAdd 折叠 `_FusedMatMul`、Sigmoid/Tanh→Mul 融合 |
| graph_optimizer | `tensorflow/core/grappler/optimizers/graph_optimizer/graph_opt.cc` + `*_rewriter.cc` | 长模式/自定义融合 | QKV 投影融合 `KPFusedQKVProjection*` |

`run_graph_optimization` 在 `remapper.cc` 的 `Optimize()` 里被调用（`annc::run_graph_optimization(&mutable_item.graph)`），所以两层顺序是：remapper 前会先跑 graph_optimizer。

## 2. rewriter 实现（graph_optimizer 层，以 QKV 为例）

```cpp
class KPFusedQKVProjectionRewriter : public PatternRewriter {
 public:
  std::string name() const override { return "KPFusedQKVProjection"; }
  bool match_and_rewrite(const NodeDef* node, GraphDef* graph,
                         std::unordered_map<std::string, int>& node_indexes) override {
    // 1. 候选过滤：op 类型 + weight 维度
    // 2. 拓扑追踪：TraceProjectionForward / FindAttentionCore / TraceProjectionBackward
    // 3. 校验：ValidateProjectionSet / BuildSharedVcgInputs
    // 4. 改写：RewriteStandard / RewriteShared（生成 fused 节点 + Identity 替换原投影）
  }
 private:
  bool Reject(const char* reason) { ... }   // 不匹配时拒绝，不改图
};
```

关键设计原则：
- **Reject 不改图**：所有校验在 mutation 前完成，Reject 后 GraphDef 字节不变（可安全重试）。
- **图改写用 Identity 占位**：原投影节点替换成 Identity（指向 fused 输出），避免破坏下游引用；再 `ReplaceDataUsers` 把用户重定向到 fused 输出。
- **保留控制边 / `_class` / `_output_shapes`**：融合节点要继承原节点的 colocation 和 shape 属性。

## 3. 门控注册

`graph_opt.cc` 的 `run_graph_optimization`：

```cpp
if (enabled_aarch64_rewriters()) {           // MIDR 检测华为 0x48（0xd01/0xd02/0xd03/0xd06）
  bool enable_all = FLAGS_annc;
  if (enable_all && FLAGS_annc_fused_qkv)
    optimizer.register_rewriter(CreateKPFusedQKVProjectionRewriter());
  ...
}
optimizer.optimize();
```

- flag 定义在 `tensorflow/core/gflags.cc`，默认值决定开/关。
- **QKV 这类长模式 rewriter 要在通用 MatMul 融合（`KPFusedMatMulRewriter`）之前注册**，让长模式先吃自己的投影。

## 4. 匹配验证（关键）

图优化是否生效，用 `LOG(INFO)`（不是 fprintf）在 matcher 的 `Reject`/`Rewrite` 路径打点，跑一次 server 看 `server.log`：

```
[QKV-GATE] aarch64_rewriters=1 annc=1 fused_qkv=1          ← rewriter 注册了没
[QKV-MATCH] candidate <node> op=MatMul width=512           ← 找到候选
[QKV-MATCH] <node> reject attention_topology(forward)      ← 具体 Reject 原因
[QKV-MATCH] <node> REWRITE shared width=512                ← 融合成功
```

验证后**删除热路径/图优化路径的调试日志**（图优化日志虽不是每请求热路径，但会刷屏；kernel 日志才是热路径，必须删）。

## 5. 踩过的坑速查

- **node_name 悬空引用（UB）**：matcher 里 `add_node` 触发 `RepeatedPtrField` reallocation，之前持有的 `node->name()` 引用失效。修复为值副本（`std::string`），否则图优化结果不确定、server 随机退出。
- **matcher 误融合**：融合边界要精确（如 QKV 只融合 Q/K/V 投影，不碰 attention 核心的 QK/PV），否则数值/语义错误。
- **remapper 的 `FindActivationAndMul` 误放分支**：最初放在 `if (IsMKLEnabled() && ...)` 内，aarch64（无 MKL）上整个融合不执行——平台相关分支要仔细检查。
- **图改完 server 优雅退出无报错**：多半不是图的问题，而是 kernel 没链接（见 `tf-kernel-optimize`），用 `nm -C` 验证。

## 6. 提交前本地编译门控（可选，有本地构建能力时）

> 默认采用外部构建（CI/开发者），无本地编译门控。**若用户具备本地编译能力，则 commit 前强制执行本地编译门控**，拦截「图改对了但编译不过 / 单测失败」类返工：

```bash
# 1. 编译图优化目标（remapper / graph_optimizer 库）
bazel build //tensorflow/core/grappler/optimizers:...  # 按实际 target
# 2. 跑聚焦单测（remapper / rewriter 匹配测试）
bazel test //tensorflow/core/grappler/optimizers:remapper_test \
           //tensorflow/core/grappler/optimizers/graph_optimizer:...
```

- 编译/单测不过 → 修复后再 commit，不把编译问题留给外部构建。
- 无本地构建能力 → 跳过本门控，仍走外部构建协作协议（构建问题回传后修复 rebase）。

## 7. 提交规范

优化目标 TF fork 源码仓库内有 `tensorflow/core/grappler/optimizers/ONLINE_OPTIMIZATION_COMMIT_STANDARD.md`：实现 + 聚焦测试 + 每特性文档 + summary 文档更新应放同一 commit；性能声明要写明受益模型和负面/未测模型。

## 8. 参考案例

- `references/case-lstm-gate-fusion-debug.md` — LSTM gate 融合不生效的根因排查案例（脱敏）
