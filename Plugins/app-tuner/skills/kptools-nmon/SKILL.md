---
name: kptools-nmon
description: >
  采集系统性能数据（CPU 利用率、内存、磁盘 IO、网络等）。
  当用户需要采集系统性能指标、CPU 利用率、内存使用、磁盘 IO 统计、
  网络吞吐采集、nmon 数据采集时触发。
license: GPL-3.0
compatibility: Linux, nmon 16.x
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: system_resources
  version: "1.0.0"
---

# kptools-nmon — 系统性能数据采集

## 工作流程

1. 检查 nmon 可用性：`bash scripts/discover.sh`
2. 工具缺失时：`bash scripts/deploy.sh`（root 自动通过包管理器安装并复验；非 root 打印安装命令，需用户确认后安装），安装成功后重试采集一次；仍失败则向用户说明缺失影响并给出安装命令，不要静默跳过
3. 执行采集：`bash scripts/collect.sh -d 10 -i 1`
4. 返回标准化输出（CSV 原文或 JSON envelope）

## 采集命令

```bash
# 按时长采集（默认 10 秒，每秒采样）
bash scripts/collect.sh -d 30 -i 1

# 单次快照
bash scripts/collect.sh --snapshot

# 附带 Top Processes 统计
bash scripts/collect.sh -d 30 --top

# Top Processes 含命令行参数
bash scripts/collect.sh -d 30 --top-args

# JSON envelope 包装输出
bash scripts/collect.sh -d 30 --json

# 数据文件保留到指定目录
bash scripts/collect.sh -d 30 -m /tmp/nmon-keep
```

## 输出

- stdout: nmon CSV 原文（读自数据文件）；`--json` 时为 envelope（CSV 原文置于 `data` 字段，`data_file` 为保留的文件路径）
- stderr: 诊断信息（数据文件路径、时长换算提示、错误）

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-d` | 采集时长（秒） | 10 |
| `-i` | 采样间隔（秒） | 1 |
| `--top` | 附带 Top Processes 统计 | 关闭 |
| `--top-args` | Top Processes 含命令行参数 | 关闭 |
| `--snapshot` | 单次快照 | - |
| `--json` | JSON envelope 包装 | - |
| `-m <dir>` | 数据文件保留目录 | 临时文件（用后删除） |
| `--help` | 显示帮助 | - |

## 不要做的事

- 不要解析 nmon 输出为其他格式 — 原样返回，由 agent 解读
- 不要在采集过程中添加额外处理 — 保持透传
- 不要用于单进程 PID 级采集 — nmon 仅支持全系统 + Top Processes，进程级指标由 kptools-proc-collector 覆盖

## 约束

- nmon `-f/-F` 数据采集模式会 fork 到后台：collect.sh 轮询数据文件的 ZZZZ 快照行判断完成（不强依赖进程退出——数千接口的宿主机 BBBP 元数据采集可达数十秒，个别 nmon 16p 构建快照完成后进程也不退出；上限采集窗口 +300s，超时无快照报错、有部分快照告警继续），交互模式被禁用
- 快照数过多会导致数据文件过大（建议单次采集不超过 800 个快照）
- `-d` 不能被 `-i` 整除时按向上取整采集，stderr 提示实际时长

## 错误处理

- `discover.sh` 显示 NOT_INSTALLED → `yum install nmon` 或 `apt install nmon`，或运行 `bash scripts/deploy.sh`（root）
- collect.sh 报 `nmon not found` → 同上
- collect.sh 报 `interval must not exceed duration` → 调整 `-d`/`-i` 关系（如 `-d 10 -i 20` 改为 `-d 20 -i 10`）
- collect.sh 报 `nmon data file not found or empty` → 重试；检查磁盘空间和 /tmp 可写性

## 参考

- [nmon 使用指南](references/nmon-guide.md)
