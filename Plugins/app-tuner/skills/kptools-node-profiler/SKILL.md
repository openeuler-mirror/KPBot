---
name: kptools-node-profiler
description: >
  采集 Node.js 进程 CPU 性能数据：V8 火焰图（--prof/--cpu-prof 注入）、
  isolate 日志解析、prof-process 热点汇总、0x 火焰图。
  当用户需要分析 Node.js 服务 CPU 热点、网关/代理性能、V8 火焰图、
  Node CPU profiling、Node.js 进程卡顿归因时触发。
license: MIT
compatibility: Node.js ≥ 8（--prof）；--cpu-prof 需 ≥ 12；0x 需 npm
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: node_profiling
  version: "1.0.0"
---

# kptools-node-profiler — Node.js 进程 CPU profiling

## 工作流程

1. **语言路由（必做）**：目标为 Node.js 进程（cmdline/exe 含 node）→ 本 SKILL
2. 检查环境：`bash scripts/discover.sh`
3. 运行中进程：V8 无 attach 能力 → `bash scripts/collect.sh --guide -p <PID>` 输出注入指引，由用户确认后重启服务
4. 日志解析：`bash scripts/collect.sh --parse -f <isolate-*.log>` / `--parse-dir -d <dir>`
5. cpuprofile 产物：`bash scripts/collect.sh --cpuprofile -d <dir>`

## 采集命令

```bash
# 解析单个 V8 日志（node --prof 启动注入后生成）
bash scripts/collect.sh --parse -f isolate-*.log

# 解析目录内全部日志（多 worker 场景）
bash scripts/collect.sh --parse-dir -d /path/to/logs

# 定位 --cpu-prof 产物（Chrome DevTools 格式）
bash scripts/collect.sh --cpuprofile -d /tmp/cpuprof

# 输出注入指引（含 PID 的命令模板：--prof / --cpu-prof / 0x）
bash scripts/collect.sh --guide -p <PID>
```

## 输出

- stdout: prof-process 文本汇总（[Summary]/[JavaScript]/[C++] 段）、cpuprofile 清单、注入指引
- stderr: 诊断信息（产物存档路径、错误原因）
- 退出码: 0 成功；2 依赖缺失；3 参数错误
- 解析产物同时存档至 `./node-profiler-evidence/<时间戳>/`

## 参数速查

| 参数 | 说明 |
|------|------|
| `--parse -f <log>` | 解析单个 isolate-*.log |
| `--parse-dir -d <dir>` | 解析目录内全部日志 |
| `--cpuprofile -d <dir>` | 定位 *.cpuprofile 产物 |
| `--guide -p <PID>` | 生成注入指引 |

## 约束

- **需重启注入**：V8 采样器不支持 attach 运行中进程；对运行中进程只输出指引，不伪造采集结果
- `--prof` 采样开销约 1ms 间隔，高 QPS 网关低峰采集
- isolate-*.log 生成于进程 CWD；容器场景确认卷挂载
- `--cpu-prof` 产物优雅退出时写出，强杀可能丢失
- 0x 为可选增强（npm install -g 0x），未安装不影响核心路径
- `kill -USR2` 仅翻转 V8 日志，不能开启 CPU profiling

## 错误处理

- `node not found` → 安装 Node.js（https://nodejs.org）
- `未发现 isolate-*.log` → 服务需以 `--prof` 注入重启（--guide 输出模板）
- `无法读取文件` → 确认路径与挂载

## 参考

- [Node.js profiling 指南](references/node-profiler-guide.md)
