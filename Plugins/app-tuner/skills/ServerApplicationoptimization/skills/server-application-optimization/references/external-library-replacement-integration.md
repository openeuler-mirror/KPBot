# 外部 Library Replacement 接入说明

## 目标

本说明用于将 `library-replacement` 作为 `performance-library-selection` 的 ARM64 专项后端接入。当前框架优先使用仓库根目录内置的 `ref-skills/library-replacement`；仓库外来源只能作为显式审查后的 fallback。

## 默认路径

- 仓库内优先路径：`ref-skills/library-replacement`
- 仓库内入口文件：`ref-skills/library-replacement/SKILL.md`
- 外部 fallback：默认禁用；仓库内路径不可用时回退到内部通用路径，只有显式 `--allow-clone` 且完成来源审查后才允许拉取外部来源

## 适用条件

满足以下条件时，优先使用外部 `library-replacement`：

1. 架构为 `aarch64`
2. 仓库内路径存在，或用户显式批准了经审查的 fallback 路径
3. `optimization_kb.json` 存在
4. 运行环境满足基本只读探测要求

## 知识库路径建议

优先查找：

1. `ref-skills/library-replacement/optimization_kb.json`
2. 用户显式传入的 `optimization_kb_path`

## 安装方式

### 手动安装

1. 优先使用仓库自带的 `ref-skills/library-replacement`（默认已随仓库提供）
2. 若仓库内不可用，则自动回退到内部通用高性能库选型路径；不得自动联网拉取外部来源
3. 放置 `optimization_kb.json`
4. 确认基础工具存在：`perf`、`lsof`、`ps`、`uname`、`lscpu`

### 可选显式安装脚本

当前仓库提供显式安装脚本：

- `scripts/install_external_library_replacement.sh`

该脚本仅在用户主动运行时执行，不会在主 skill 流程中自动触发。
默认只检查仓库内置路径；如确需外部 clone，必须显式传入 `--allow-clone` 并先完成来源审查。

## 回退逻辑

以下情况回退到内部 `performance-library-selection`：

- 非 `aarch64`
- 外部路径不存在
- `optimization_kb.json` 缺失
- 关键探测工具缺失
- 用户未提供足够的在线/离线分析输入

## 使用建议

普通用户：

- 只使用 `server-application-optimization`
- 在性能库替换阶段由框架自动判断是否进入外部路径

高级用户：

- 可以显式指定 `prefer_external_library_replacement=true`
- 可以提供 `target_pid` 或 `launch_command`
- 可以显式提供 `optimization_kb_path`
