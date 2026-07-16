# 外部 Network IO Performance 接入说明

## 目标

本说明用于将 `network-io-performance` 作为 `network-optimization` 的深度网络 IO 分析后端接入。当前框架优先使用仓库根目录内置的 `ref-skills/network-io-performance`，历史仓库外路径仅作为兼容 fallback。

## 默认路径

- 仓库内优先路径：`ref-skills/network-io-performance`
- 仓库内入口文件：`ref-skills/network-io-performance/SKILL.md`
- 仓库内主脚本：`ref-skills/network-io-performance/scripts/network_io_check.sh`
- 外部兼容 fallback：已移除硬编码绝对路径；仓库内路径不可用时回退到内部通用路径，无需用户手动指定

## 适用条件

满足以下条件时，优先使用外部 `network-io-performance`：

1. 仓库内或兼容 fallback 路径存在
2. `SKILL.md` 存在
3. `ref-skills/network-io-performance/scripts/network_io_check.sh` 存在
4. 运行环境满足基本网络诊断要求

## 能力范围

外部 `network-io-performance` 当前重点覆盖：

- link up 与活跃接口发现
- 中断号与亲和性分析
- IRQ 负载分析
- 丢包与错误统计
- TX/RX 队列平衡分析

## 安装方式

### 手动安装

1. 优先使用仓库自带的 `ref-skills/network-io-performance`（默认已随仓库提供）
2. 若仓库内不可用，则自动回退到内部通用网络优化路径
3. 确认基础工具存在：`ip`、`sar`、`netstat`、`ethtool`
4. 若需要中断负载分析，建议安装 `irqtop`

## 回退逻辑

以下情况回退到内部 `network-optimization`：

- 外部路径不存在
- `SKILL.md` 缺失
- `network_io_check.sh` 缺失
- 关键工具缺失
- 用户当前场景不允许执行网络只读检测

## 当前注意事项

外部 skill 原始文档里包含针对特定 OpenCode 路径的硬编码调用说明。当前框架不会直接复用这些硬编码路径，而是通过当前仓库中的适配脚本和路径约定来引用其本地脚本。

## 使用建议

普通用户：

- 只使用 `server-application-optimization`
- 在网络优化阶段由框架自动判断是否进入外部路径

高级用户：

- 可以显式指定 `prefer_external_network_io_performance=true`
- 可以显式提供外部 skill 路径
- 可以先运行依赖检查脚本确认当前环境是否适合启用外部网络专项分析
