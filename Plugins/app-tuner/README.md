# app-tuner

应用级调优插件 — 服务器应用性能优化的端到端 Agent 工作流。

同时支持 **Claude Code** 和 **OpenCode** 两种 AI 编码工具。

## 概述

app-tuner 围绕 1 个主编排 skill（`kpbot-app-tuner`）组织，外加分域独立 skill 和工具辅助 skill，形成完整的应用性能调优体系：

- **主编排**: `skills/kpbot-app-tuner/` — 环境采集 → 瓶颈识别 → 动态路由 → 迭代验证 → 报告输出
- **分析型子 skill**（subskills）: BIOS/OS/网络/CPU 亲和性/编译/性能库/应用配置/加速卡/硬件升级/瓶颈预筛/数据库专项等，路由表见主 skill 的 `SKILL.md`
- **AgentENV 专项技能包**: `skills/kptools-agentenv-tuning/` — 独立入口及按需安装的专项 skill
- **验证类 skill**: `skills/model-precision-analysis/` — 模型优化后的精度回归验证

## 安装

### OpenCode

```bash
# 克隆 KPBot 仓库
git clone https://atomgit.com/openeuler/KPBot.git

# 安装到你的项目
cd /path/to/your-app-project
/path/to/KPBot/install.sh project opencode

# 启动
opencode
```

安装后 `install.sh` 会自动:
1. 将 skill 拷贝到 `.opencode/skills/`
2. 生成 `AGENTS.md` 系统提示词(若仓库根有 CLAUDE.md)

验证安装:

```bash
opencode debug skill | grep app-tuner
```

### Claude Code

```bash
# 安装 marketplace
claude plugins marketplace add https://atomgit.com/openeuler/KPBot.git

# 或本地安装
claude plugins install --plugin-dir /path/to/KPBot/Plugins/app-tuner
```

## 技能组成

完整 skill 清单不在此文件逐项维护（避免每次新增 skill 都修改本文件）。按以下方式查阅：

| 类别 | 查阅位置 | 说明 |
|------|----------|------|
| 分析型子 skill（subskills） | [`skills/kpbot-app-tuner/SKILL.md`](skills/kpbot-app-tuner/SKILL.md) 路由表 | 主编排器的瓶颈路由目标，含职责边界 |
| 分域独立 skill | `skills/` 下各域目录的 `DOMAIN.md` | microservice/nginx、ascend_kunpeng、bigdata、storage 等域的成员表 |
| 工具辅助类 skill | `ls skills/kptools-*` | 采集器/分析器/部署器，只读或主动探测 |
| 独立调优 skill | `skills/` 目录 | 场景优化、环境部署、时序剖析等独立入口 |

## 目录结构

```
skills/
├── kpbot-app-tuner/          # 主编排 skill（subskills/ 含分析型子 skill）
├── kptools-*                 # 工具辅助类 skill（采集、分析、部署）
├── microservice/             # 微服务域（nginx 调优链等）
├── ascend_kunpeng/           # 昇腾+鲲鹏域
├── bigdata/                  # 大数据域
├── storage/                  # 存储域
├── agentic-infra/            # Agent 基础设施域
├── opencode/                 # OpenCode 平台覆盖层（差异文件）
└── ...                       # 独立调优/验证/环境部署 skill
```

## 卸载



```bash
/path/to/KPBot/bin/kpbot uninstall project opencode
```
