# Faiss 构建契约

## 输入

记录 repo、源码身份及补丁、build_mode、build_dir、run_dir、目标平台/ISA、工具链、依赖与并行资源。candidate 另需唯一特性宏；外部后端选项仅在目标工程支持时使用。

## 模式

- baseline：构建明确的参考源码/补丁/宏集，记录实际身份。辅助脚本要求干净工作区，遇已有修改应保留并选择隔离源码状态或合适模式。
- current-head-profile：允许已识别的工作区改动，用于调查当前真实二进制；不得冒充另一个冻结基线。
- candidate：相对参考构建只启用一个主要候选，公共条件一致。验证候选宏进入实际目标翻译单元。

## 配置示例

```bash
cmake -S <faiss-repo> -B <independent-build-dir> \
  -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_PYTHON=OFF \
  -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build <independent-build-dir> --target faiss --parallel <jobs>
```
这是 CPU 共享库配置形状，实际目标、opt level、BLAS/OpenMP 及 ISA 按当前工程和基线确定。附加选项通过本次配置显式记录；不预设特定扩展后端。

## 目录与证据

优先使用各自独立的 OFF/ON 构建目录并保护参考产物。需要清理时只处理本任务明确拥有且解析验证过的目标；不清理整个仓库或用户修改。脚本的 `--clean` 仅适用于其允许的 repo/build 或 repo/build-*目录，不是默认步骤。
保存 BUILD-MANIFEST.json、CMake/构建日志、实际 compile_commands、宏与 flags 摘要、链接信息和库 SHA256。测试程序必须确认实际加载库，配置成功不等于路径正确。

## 辅助脚本

`scripts/build_faiss.sh --help` 描述 CPU shared/generic 构建接口；`--cmake-arg` 扩展目标工程配置。`--krl on|off` 是可选兼容选项，仅在目标 CMake 支持该后端时使用；未指定则不注入该选项。`--blas` 保留其 MKL_LIBRARIES 传参接口，其他 BLAS 发现方式按当前 CMake 支持的变量设置。

`--candidate-macro` 指定待核验的实际编译定义，不自动启用 CMake 选项；需要时通过 `--cmake-arg -DFAISS_ALGORITHM_CANDIDATE=ON` 启用。已修改源码的 OFF 验证可用 `current-head-profile` 模式并记录工作区身份；它不替代独立的原始基线。

辅助脚本的 manifest 声明 Release / generic 构建。配置后会核对实际 `CMakeCache.txt`；附加参数或工程中的强制赋值若改变该形状，或与显式 `--krl` 冲突，将停止构建并保留配置日志。其他构建形状使用项目原生 CMake 命令并记录实际配置，不沿用该辅助脚本的固定声明。

## 失败处理

保留失败日志并报告具体配置、编译、链接或宏命中问题。不得为通过构建改变冻结查询参数，或把未成功的产物作为正式对照。
