---
name: faiss-build
description: 构建 Faiss 基线、当前检出采样版本或特性开关候选，并记录编译命令、实际宏、链接依赖和产物身份。适用于可复现 CPU 库构建及构建差异核对。
---

# Faiss 构建

## 输入

源码与目标平台、构建模式、独立构建目录、产物目录及基线编译配置。候选模式补充唯一特性开关；可选后端按实际仓库能力设置。

## 执行步骤

1. 确认目标源码、工具链、ISA、共享/静态库需求与 BLAS/OpenMP 依赖。
2. 区分 `baseline`、`current-head-profile`、`candidate`，按 [构建契约](references/build-contract.md) 选择配置。
3. 在独立目录配置和构建，保留完整命令、日志、编译数据库及库 SHA256；核对真正进入目标翻译单元的宏和 flags。
4. 候选进行 OFF/ON 构建，公共配置保持一致；确认测试程序实际加载对应的库。

## 输出

`BUILD-MANIFEST.json`、配置/构建日志、`compile-definitions.txt`、`libfaiss.sha256` 和链接证据。

## 约束

内置 `scripts/build_faiss.sh` 需要有可读取提交的 Git 工作树。发布归档等非 Git 源码使用项目原生构建命令，记录版本、归档/源码摘要、编译参数和产物身份；不要为运行辅助脚本虚构提交身份。

构建模式不赋予清理工作区的权限。已有修改保留并记录；基线状态不明确时先解决身份问题。ISA、后端和库路径不得从算法名或其他机器配置推定。

## 参考资料

- [模式与参数](references/build-contract.md)
- [算法能力检查](references/algorithm-build-matrix.md)
- [特性开关](references/cmake-feature-gates.md)
- [构建证据](references/build-evidence.md)
- 辅助脚本：`scripts/build_faiss.sh --help`。其默认目标为 CPU shared `faiss`；其他布局按目标工程配置。
