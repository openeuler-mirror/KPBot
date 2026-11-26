# sra_test 构建与运行

## 1. 先核对工程接口

读取实际 Makefile/CMake、算法注册、配置解析、主程序和 `--help`。确认目标名称、参数顺序、显式配置选项、数据路径如何解析，以及保存/加载索引的类型。

## 2. 构建

明确 Faiss 头文件根、库路径、编译器、语言 ABI、OpenMP/BLAS 及必要扩展。源码宏与库 CMake 选项分开记录；未支持的扩展字段或编译开关不加入配置。
```bash
cd <sra-test-repo>
make <verified-algorithm-target>
ldd <test-binary>
readelf -d <test-binary>
```
若构建脚本交互询问路径/flags，按当前工程要求填写并保存回答和最终命令。只在工程支持时使用额外定义，默认不添加可选后端/量化开关。

## 3. 数据和索引

检查维度、数据类型、训练/查询/ground-truth 形状和 hash。程序若使用相对 data/路径，选择正确工作目录或在任务授权目录提供映射，并记录实际解析路径。
先生成并保存正确的完整索引类型，再在 A/B 使用同一文件。包装器是否被序列化、加载后是否重建以及 SearchParameters 覆盖行为都要核对；不能把 base 索引当作完整包装索引加载。

## 4. 显式配置与资源

配置目录、数据集名和索引路径按真实 CLI 传递，避免默认解析覆盖调用意图。将 CPU/NUMA 边界施加到父进程、加载阶段和 worker，检查内部绑核是否遵守外部 mask；声明的是逻辑 CPU 还是物理核。
```bash
numactl -C <cpu-list> -m <memory-node> -- \
  <test-binary> <verified-arguments>
```
仅当当前接口已经确认时，使用诸如 `<binary> <algorithm> <dataset> --config <config-dir>` 的参数形状。

## 5. 正式测量

读取当前计时代码和输出，确定预热、循环统计、单查询/批量、每线程工作量、QPS 及 Recall 公式。完整配置和实际输出写 manifest 与原始日志，再按实验协议进行交错对照。

## 6. 产物

保存构建命令、头文件/库身份、链接与加载证据、有效配置、数据/索引 hash、实际线程 mask、逐轮日志和解析结果。功能冒烟、采样及正式性能分别标注。
