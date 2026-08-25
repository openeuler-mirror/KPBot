---
name: tf-kernel-optimize
description: 编写 TF 自定义 fused op/kernel（REGISTER_OP + REGISTER_KERNEL_BUILDER、grouped GEMM、KDNN 后端），并处理 BUILD 依赖、链接和编译选项，让 kernel 真正编译进 binary 且能在同步/异步环境运行。触发：自定义 op、fused kernel、REGISTER_OP、REGISTER_KERNEL_BUILDER、grouped GEMM、KDNN、kernel 链接、-fexceptions、No OpKernel registered、AsyncOpKernel。
---

# TF 推理优化 - 自定义 kernel 实施

图优化生成 fused op 节点后，必须有对应 kernel 真正编译进 binary 才能运行。这是「图改对了但 server 退出」最常见的根因。

## 1. op + kernel 注册（三个文件）

```cpp
// 1) ops/xxx.cc  —— op 定义（含 ShapeFn）
REGISTER_OP("KPFusedQKVProjection")
    .Input("q_input: float").Input("q_weight: float")
    .Input("k_input: float").Input("k_weight: float")
    .Input("v_input: float").Input("v_weight: float")
    .Output("q: float").Output("k: float").Output("v: float")
    .Attr("projection_width: int").Attr("pattern: string")
    .SetShapeFn([](InferenceContext* c){ /* rank/维度校验 */ });

// 2) kernels/xxx.cc  —— kernel 实现 + 注册
class KPFusedQKVProjectionOp : public AsyncOpKernel { ... };
REGISTER_KERNEL_BUILDER(Name("KPFusedQKVProjection").Device(DEVICE_CPU),
                        KPFusedQKVProjectionOp);
```

ShapeFn 的 rank/维度校验要与 matcher 生成的 fused 节点 input 完全对齐（否则图执行时 shape 推断失败）。

## 2. BUILD：让 kernel 链接进 binary（最关键的坑）

kernel 的 `tf_kernel_library` 定义后，必须被 `embedding_fused_ops`（最终 binary 依赖的聚合库）引用：

```python
# kernels/BUILD
tf_kernel_library(
    name = "qkv_projection_op",
    srcs = if_enable_annc(["qkv_projection_op.cc"]),
    deps = MATH_DEPS + kdnn_deps(),
)
cc_library(
    name = "embedding_fused_ops",
    deps = if_enable_annc([
        ...
        ":qkv_projection_op",   # ← 漏了这行，kernel 不进 binary
    ]),
)
```

**验证**：`nm -C <binary> | grep -c <KernelClassName>`（应为非 0，如 QKV 的 22）。为 0 即 kernel 未链接——图优化生成了节点但运行时 `No OpKernel was registered`，server 优雅退出且无报错日志。

## 3. 编译选项（-fexceptions）

kernel 用 try/catch（KDNN 会抛异常）时，需在 `tf_copts()` 的 `if_enable_kdnn` 里加 `-fexceptions`：

```python
# tensorflow/tensorflow.bzl  tf_copts()
if_enable_kdnn(["-DENABLE_KDNN", "-fexceptions"]) +   # 位置在 -fno-exceptions 之后才生效
```

**不要只加在 target 的 `copts`**：`tf_kernel_library` 内部是 `copts + tf_copts()`，`tf_copts()` 里的 `-fno-exceptions` 排在后面会覆盖 target 的 `-fexceptions`。

## 4. 同步 / 异步环境兼容（AsyncOpKernel）

fused kernel 若继承 `AsyncOpKernel`，其 `ComputeAsync` 依赖 `context->runner() != nullptr`。但 **tf_pre_run（模型加载预运行）是同步环境，`runner == nullptr`**，会导致启动即退。加同步 fallback：

```cpp
void ComputeAsync(OpKernelContext* context, DoneCallback done) override {
  auto* runner = context->runner();
  if (runner == nullptr) {
    // 同步顺序执行（复用同一 GEMM 逻辑），不依赖 runner
    try { /* 顺序跑各 role */ } catch (...) { context->CtxFailure(...); }
    done();
    return;
  }
  // 正常异步并发路径
}
```

## 5. 调试日志用 LOG(INFO) 而非 fprintf

server 是守护进程，stderr 被重定向，`fprintf(stderr)` 看不到；`LOG(INFO)`/`VLOG` 能进 server.log。**但 kernel 的 Compute 是每次请求的热路径，调试日志验证后必须删**（否则锁竞争 + I/O 拖垮 QPS）。

## 6. 踩过的坑速查

- **kernel 没链接**：见第 2 节，`nm -C` 验证，这是最高频的坑。
- **`-fexceptions` 位置**：见第 3 节，加在 `if_enable_kdnn` 里，别加在 target copts。
- **AsyncOpKernel 同步 fallback**：见第 4 节，tf_pre_run 无 runner。
- **分组 GEMM / per-role bias**：Shared kernel 用 `KDNN::IndependentGroupedGemm` 一次调度多 role，bias 用 `num_bias` attr 控制（0 或 3），避免逐 role 独立 kernel 调度。
- **日志吞错误**：InitGoogleLogging 之前的错误不打印，靠 `LOG(INFO)` 打点或 gdb/strace 定位。

## 7. 数值等价（业务硬约束）

正确性测试用例必须全部通过（口径由用户在环境验证阶段定义，见 `./AGENTS.md`）。fused kernel 的数学顺序需与独立 op 保持一致：

- 复用与独立 op 完全相同的 functor（如 Eigen `scalar_logistic_op`/`scalar_tanh_op`）保证 bit 级等价。
- grouped GEMM 会改变并行归约切分，**可能改变浮点累加顺序**（这不是「只改调度、不改累加」）。若用户正确性用例要求 bit 级等价 → 用确定性归约或逐 role 独立 kernel；若仅要求容差内结果一致 → 必须在正确性用例上实测验证，不可声明式假设结果不变。
