# AgentENV 技能包

AgentENV 沙箱在 openEuler/ARM 上的功能适配和性能调优，独立组织在
`Plugins/app-tuner/skills/kptools-agentenv-tuning/`，包含一个入口及按需安装的专项 Skill。
入口 `SKILL.md` 的 name 统一为 `kptools-agentenv-tuning`，不改各专项的名称。

```text
kptools-agentenv-tuning/
├── SKILL.md
├── references/validation.md
├── references/optimization-process.md
├── scripts/collect_environment.sh
├── evals/evals.json
└── skills/                    # 实际安装的专项 Skill
```

## 安装与调用

在 KPBot 仓库运行，目标目录替换为实际项目：

```bash
bash install.sh project claude /path/to/project
# 或
bash install.sh project opencode /path/to/project
```

安装器已支持 `skills/<package>/skills/<skill>/SKILL.md`。
安装后入口和实际存在的专项 Skill 均有独立入口，专项 Skill 的共享 `references/` 和 `scripts/`
通过安装器链接到技能包资源。直接使用插件时，从 `kptools-agentenv-tuning` 入口
按相对路径读取专项文件。

示例：

> 用 kptools-agentenv-tuning 检查 AgentENV 并发恢复慢的问题。先检查 ARM 服务器环境，
> 结合我的沙箱测试命令给出可验证的方案。

专项 Skill 可在实际安装后按其名称独立调用；入口不会把未安装的专项当作可用能力。

## ARM 服务器上的第一步验证

在 ARM WebIDE 终端或 SSH 会话中，从 KPBot 仓库根目录运行：

```bash
bash Plugins/app-tuner/skills/kptools-agentenv-tuning/scripts/collect_environment.sh \
  --output "/tmp/agentenv-evidence-$(date -u +%Y%m%dT%H%M%SZ)" \
  --role unknown
```

如果 WebIDE 是容器，这份结果只是容器视角。Host/Guest 各需单独采集，
并在角色确认后使用 `--role host` 或 `--role guest`。
已知 AgentENV 进程时追加 `--pid PID`，记录目标进程实际允许的 CPU 和 cgroup。

检查产物：

- `collection-status.txt`：采集结果，不是优化结论。
- `environment.txt`：时间、hostname、boot_id、采集 UID、内核、架构、声明的节点角色。
- `kernel-config.txt`：运行内核配置来源及相关配置项；不可读时明确标记 unknown。
- `virtualization.txt`、`topology.txt`：执行环境与 CPU 拓扑。
- `ublk.txt`：驱动和控制设备可见性，不创建或删除块设备。
- `ublk-module.txt`：磁盘上的模块元数据；需与已加载模块的 srcversion 等交叉核对。
- `target-process.txt`：指定进程的名称、能力、CPU/内存节点允许集合与 cgroup。
- `target-identity.txt`：目标 exe 与原始 stat；权限不足时单独记录 unknown。
- `cpu-pressure.txt`、`perf-policy.txt`：节点 CPU 压力与 perf 策略，只作为诊断输入。
- `manifest.tsv`：每项检查的退出码和证据可用性。

退出码：0 = ARM Linux 采集完成；2 = 不支持的平台；1 = 参数或输出目录错误。
缺失工具或配置单独标记为 unknown，不会推断成“功能未开启”。

## 验证范围

环境采集可验证脚本和前置检查。真实优化还需 AgentENV 的部署说明、沙箱恢复
用例、Pvspinlock/TLBI 对应实现及可恢复的内核测试环境。
普通 ARM 容器不足以证明 Host/Guest 半虚拟化或 TLBI 范围控制可用。

`evals/` 中的评估输入检查适用性判断、项目参数适配、实施权限、单变量对照和结果处理。
它们是评估输入，不代表已经运行模型评估或复现性能收益。
性能对照与结果格式见 `references/validation.md`。各专项的评估输入位于
`evals/<skill>.json`，入口的通用评估输入仍位于 `evals/evals.json`。

采集器回归可在无提权条件下执行：

```bash
python3 evals/test_collector.py
# 对照保留的原版：
AGENTENV_COLLECTOR=/absolute/path/to/original/collect_environment.sh \
  python3 evals/test_collector.py
```

非 ARM Linux 跳过现场证据断言，但仍检查参数、覆盖保护和不支持平台的退出行为。
这些是采集器检查，不是模型评估或沙箱性能测试。

Agent 用例由容器内 root 拥有的监督器采集 trace 并执行评分，模型代理以非特权用户
运行，在工作区内仅能写入 `opt/`。运行时认证文件只读挂载，不写入镜像；OpenCode 进程仍需读取
该文件，应使用专用于测试且权限最小的凭据，不要将凭据、现场采集结果或个人路径提交到仓库。
模型调用证据、评分文件和输入夹具由监督器保护，运行前后核对校验和。
构建指定用例镜像后，在仓库根目录执行不联网的容器隔离与负向测试：

```bash
AGENTENV_TEST_IMAGE=<镜像名> python3 -B -m unittest discover \
  -s Plugins/app-tuner/skills/kptools-agentenv-tuning/evals -p test_container_security.py -v
```

用例镜像默认使用原有 openEuler 基础镜像；网络环境需要时可用 Docker 的
`--build-arg BASE_IMAGE=<兼容镜像>` 指定镜像源。

## 通用优化流程

流程见 [optimization-process.md](references/optimization-process.md)：

1. 确认当前项目的优化目标、实现、环境与权限。
2. 按业务确定规格、负载、并发、预热和采样方案，建立有效基线。
3. 单变量实施，核实配置和机制生效，复测正确性与性能。
4. 需要消融时保持共同负载与其余设置不变，分别衡量各项贡献。
5. 根据实际收益和资源代价保留或回退，记录最终有效配置。

不预设项目相关参数，不写入案例成绩或固定收益。
