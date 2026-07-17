# kpbot-app-tuner skill 使用指南

## 1. 安装与接入

以 Claude Code 为例：

```bash
# 1. 克隆仓库
git clone https://atomgit.com/openEuler/KPBot.git

# 2. 放入 Claude Code 的 skills 搜寻路径
mkdir -p ~/.claude/skills
cp -r KPBot/Plugins/app-tuner/skills/ServerApplicationoptimization/skills/kpbot-app-tuner ~/.claude/skills/
```

Claude Code 启动时自动发现 `~/.claude/skills/` 下的 skill，无需额外注册。

其他编程 Agent：

- Codex：在仓库上下文中直接使用 `Plugins/app-tuner/skills/ServerApplicationoptimization/skills/kpbot-app-tuner/` 主源目录。
- Cursor：通过项目规则、Agent 配置或上下文指向 `Plugins/app-tuner/skills/ServerApplicationoptimization/skills/kpbot-app-tuner/SKILL.md`。
- OpenCode：使用 `Plugins/app-tuner/skills/ServerApplicationoptimization/.opencode/skills/kpbot-app-tuner/` 轻量入口。
- 其他支持目录式 `SKILL.md` 的 Agent：直接加载 `Plugins/app-tuner/skills/ServerApplicationoptimization/skills/kpbot-app-tuner/`。

## 2. 使用

### 整体编排调用

```shell
╭─── Claude Code v2.1.12 ──────────────────────────────────────────────────────╮
│                                 │ Tips for getting started                   │
│          Welcome back!          │ Run /init to create a CLAUDE.md file with… │
│                                 │ ────────────────────────────────────────── │
│             ▐▛███▜▌             │ Recent activity                            │
│            ▝▜█████▛▘            │ No recent activity                         │
│              ▘▘ ▝▝              │                                            │
│                                 │                                            │
│   glm-5.1 · API Usage Billing   │                                            │
│       ~/Desktop/code/test       │                                            │
╰──────────────────────────────────────────────────────────────────────────────╯

  /model to try Opus 4.5

❯ /kpbot-app-tuner 使用该skill帮忙优化mysql                      
8u32GB只读场景，部署组网选择远端压测，远端压测方法，请参考/home/s00527847/docs/ 
mysql/mysql_sysbench_test.md，不确定的先找我确认 
```
在对话中描述优化意图即可触发全流程：

```
帮我优化这台服务器上 MySQL 8.0 的 CPU 性能，当前 QPS 偏低。
压测脚本是 sysbench oltp_read_only，部署路径 /data/mysql。
```

主编排 skill 自动完成：启动输入询问 → 测试场景确认 → 环境诊断与备份 → 环境诊断结果确认 → 基线确认 → 瓶颈识别 → 性能信息采集 → 候选优化 skill 列表生成 → 迭代验证 → 报告、review、环境还原和案例归档。

启动时 Agent 会先确认三件事：用户是否已提供应用场景和环境信息、当前是先建立基线还是基于已运行应用开始、环境备份前是否采集 BIOS 配置。如需采集 BIOS 配置，需要提供 BMC/IPMI/Redfish 账号和密码/token；不采集也可以继续，BIOS 设置证据会按降级处理。

### 子 skill 独立调用

除整个大 skill 编排外，也可以针对单一优化方向直接调用每个 subskill。独立调用时，调用方需要提供该 subskill 所需的证据目录、基线数据和变更边界；subskill 不负责跨阶段编排、全局停止判断和最终案例归档。

```
用 cpu-affinity-optimization 帮我看下当前 MySQL 的绑核配置是否合理
```

可用的子 skill：

| 子 skill | 适用场景 |
|----------|---------|
| `cpu-affinity-optimization` | 绑核、NUMA 配置优化 |
| `os-optimization` | THP、irqbalance、Kernel 参数调整 |
| `bios-optimization` | Power Profile、SMT、C-State、NUMA BIOS 配置建议 |
| `network-optimization` | 网卡、中断、协议栈调优 |
| `application-config-optimization` | 线程、连接池、缓存参数调整 |
| `compiler-optimization` | 编译选项、LTO、PGO 评估 |
| `performance-library-selection` | malloc、memcpy 等性能库替换 |
| `accelerator-optimization` | GPU/NPU 等计算卡瓶颈分析 |
| `hardware-upgrade-analysis` | 更高规格硬件建议和容量边界判断 |
| `other-optimization` | 既有 skill 无法覆盖的专项分析 |

### ref-skill 独立调用

`ref-skills/` 中的第三方或参考 skill 也可单独用于专项分析，例如：

| ref-skill | 适用场景 |
|---|---|
| `ref-skills/cpu-affinity-optimization` | 只做 CPU 亲和性策略分析或脚本生成 |
| `ref-skills/network-io-performance` | 只做网络 IO、队列、中断和网卡参数专项分析 |
| `ref-skills/library-replacement` | 只做 malloc、memcpy、压缩、加密等库替换评估 |
| `ref-skills/compiler-option-optimization` | 只做编译器选项、目标架构和代码生成分析 |

在完整服务器应用优化 Agent 流程中，ref-skill 不直接替代主流程；应由对应 subskill 作为统一入口接入，确保安全门禁、证据路径、候选池和报告口径一致。

## 3. 输出

优化完成后，`output/` 目录下获得：

- `final-report.md` — 结构化优化报告
- `candidate_pool.json` — 候选动作池
- `optimization_summary.json` — 收益汇总
