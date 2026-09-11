# Kunpeng llama.cpp 调优 Skill

## 任务

在 Kunpeng/Linux AArch64 CPU 上优化 llama.cpp 本地 Embedding 性能，并交付实际应用到源码树的 C/C++ 修改。优化方向可以是：

- 接入新的 CPU 算子库；
- 优化 ggml 后端接入、路由、转换或缓存；
- 优化 llama.cpp/ggml native 实现。

配置搜索和 profiling 只用于建立基线、定位热点，不能作为最终优化成果。优化后必须验证 Embedding 正确性，并分别报告 short、medium、long 和加权性能。

本次实验工作负载为：

| 工作负载 | 权重 | 输入 |
|---|---:|---|
| short | 0.60 | `summer party dress` |
| medium | 0.30 | `I need a comfortable and elegant summer party dress suitable for an outdoor evening event with warm weather.` |
| long | 0.10 | `I am building a local semantic search system on a Kunpeng server. Generate an embedding for this representative document discussing llama.cpp CPU inference, thread affinity, NUMA placement, floating point matrix multiplication, backend routing, correctness validation, repeated measurements, and reproducible performance optimization.` |

目标函数为：

```text
0.60 × short_median + 0.30 × medium_median + 0.10 × long_median
```

## 前置条件

- 一台 Kunpeng/Linux AArch64 服务器；使用具体 ISA 优化时，机器需支持相应指令集。
- 一份可独立修改和构建的 llama.cpp 源码。
- 一个 GGUF Embedding 模型；本次使用 `embeddinggemma-300M-F16.gguf`。
- 一个已验证的参考 `llama-server`，用于比较 Embedding 正确性。
- 可用的 C/C++ 编译器和 llama.cpp 构建依赖；本次统一使用 Clang 17、Release、`-O3 -DNDEBUG`。
- 同一 NUMA 节点内数量相同且空闲的物理核；control 与 candidate 必须使用完全相同的 CPU、线程、NUMA 和服务参数。
- control 与 candidate 使用独立源码副本和构建目录，不覆盖或回退已有修改。

正确性要求：输出维度一致、数值有限、cosine `>= 0.999`、max_abs `<= 0.01`。

## 无 Skill / 带 Skill 限制

两组使用相同初始源码、模型、输入、工具链、资源和测试方法，只改变是否提供 Skill。

### 无 Skill

```text
不要读取或使用任何名为 `kunpeng-llama-cpp-tuning` 的 Skill、scripts、references 或既往实验信息。仅凭通用能力完成随本提示提供的公共任务。当前工作目录中的 `.codex/skills` 为空。

禁止读取父目录、其他实验组或既往消融结果。
```

### 带 Skill

- 允许并要求读取当前 `kunpeng-llama-cpp-tuning/SKILL.md`。
- 不允许读取无 Skill 组、既往实验结果或预制优化补丁。
- Skill 只提供调优方法，不提供脚本、代码补丁或固定 CPU/NUMA 参数。

最终测试每组、每个工作负载预热 2 次、正式运行 9 次，并采用 AB/BA 或等价交错顺序。两组必须绑定同一 NUMA 节点内的相同物理核，并使用相同线程数、编译选项和服务参数。

## 本次优化方法最终修改参考

无 Skill 组缓存了 BLAS 路径的 F16 到 F32 权重转换。

带 Skill 组定位到 AArch64 SVE F16 点积热点，将 widened FP32 累加改为原生 FP16 多累加器与树形归约，并在相同条件下比较 8 累加器和 4 累加器版本，最终选择性能更好的 4 累加器实现。该方案通过了上述正确性门禁。

## 当前实验效果

最终统一使用 24 个 NUMA 1 物理核、`-t 24 -tb 24`、Clang 17 Release 构建和相同 native 后端配置：

| 工作负载 | Tokens | 无 Skill | 带 Skill | 延迟降低 |
|---|---:|---:|---:|---:|
| short | 5 | 11.048574 ms | 7.948215 ms | 28.06% |
| medium | 21 | 29.935743 ms | 15.336241 ms | 48.77% |
| long | 56 | 69.493240 ms | 28.501763 ms | 58.99% |

该结果只代表当前模型、工作负载、工具链和服务器上的一次受控实验，不保证在其他 Kunpeng 服务器或 llama.cpp 版本上获得相同比例的提升；更换环境后需要重新测量并选择参数与代码候选。
