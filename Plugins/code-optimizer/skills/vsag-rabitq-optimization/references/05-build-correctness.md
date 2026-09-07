# 阶段 5：编译与正确性验证

这是进入性能测试前的强制门槛。先复用兼容 build 目录增量编译，再运行聚焦测试，最后执行约定的完整测试集合。

## 编译检查

- 使用目标仓库规定的编译器、构建参数、formatter 和静态分析版本；既有工作使用 clang-format 15、clang-tidy 15，但以目标仓库要求为准。
- 用户指定 NUMA node 时保持构建和测试绑定一致，例如 `numactl --cpunodebind=0 --membind=0`。
- 检查 OpenBLAS/CBLAS 配置、链接符号和 feature guard，确保目标实现真正进入 binary。
- 先运行与 RaBitQ、transformer、DataCell、batcher、searcher 相关的增量构建和测试，再运行约定的完整 unit、auxiliary、functional suites。

## 正确性矩阵

- batched 与 scalar query preparation、距离和 top-k 结果；
- L2、IP/Cosine；
- singleton、FHT、batch disabled、generic/transform quantizer fallback；
- PCA、MRQ、padded query stride；
- contiguous 与 gathered code access；
- 环境变量解析和高维 batch policy；
- concurrent collection、worker exception、shutdown/析构；
- recall 与既定容差，不得静默放宽判据。

只有 instrumentation 破坏 timing-sensitive 测试的前提时才允许排除该测试，并记录原因和替代验证。编译、格式/静态检查、相关测试或正确性任一失败，阶段状态即为 `failed`，禁止进入阶段 6。


