---
name: kptools-thread-affinity-analyzer
description: >
  线程绑核分析器：读取 kptools-thread-affinity-collector 的输出目录
  （samples.jsonl 采样时间序列 + topology_raw.txt 拓扑快照），按采样数据
  拓扑富化（NUMA/L3 域/NPU PCI 归属），自动判定 7 类异常（A1 忙线程跨
  NUMA 漂移 / A2 NPU 主机线程与设备 NUMA 错位 / A3 忙线程挤核 / A4
  掩码震荡 / A5 绑核意图未覆盖 / A6 观测完整性 / A7 线程翻动），输出
  HTML 报告（含"仅看异常线程"过滤）与 summary.json。线程角色识别规则
  外置 references/roles.json（--roles 可覆盖）。当用户需要分析线程绑核
  数据、生成绑核分析报告、判读线程掩码时间线、定位绑核异常（跨 NUMA
  漂移/挤核/掩码震荡/绑核意图未覆盖）、根据采样数据出 HTML 报告时
  触发。采集由 kptools-thread-affinity-collector 负责；本 skill 只读
  采样目录不做在线采样。
license: MulanPSL-2.0
compatibility: Linux/macOS/Windows（纯 stdlib 零依赖）；python3 >= 3.6；输入为 collector 的输出目录（跨机采样包可直接分析）；只读采样目录，不修改任何采样数据
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: thread_affinity
  version: "1.0.0"
---

# kptools-thread-affinity-analyzer — 线程绑核分析器

输入是 `kptools-thread-affinity-collector`（同目录 `skills/`）的采样目录
（可以是刚采完的现场，也可以是从目标机拷回来的采样包——只要包含
samples.jsonl + topology_raw.txt）。本 skill 做**离线分析**：拓扑富化
→ 线程角色识别 → 7 类异常判定 → HTML 报告 + summary.json。不依赖
目标进程存活，也不做在线采样。

## 工作流程

1. 确认采样目录完整：`samples.jsonl` + `topology_raw.txt`（缺 topology
   时若本机有 /sys 会自动补采拓扑；跨机包必须带上）
2. 分析：`python3 scripts/analyze.py <采样目录>`
3. 打开输出目录内的 HTML 报告（"仅看异常线程"过滤开关；每条异常附证据
   链）；机器可读结论在 `summary.json`
4. 字段含义、判读方法见 `references/report-guide.md`

## 分析命令

```bash
# 基本用法（报告输出到采样目录内）
python3 scripts/analyze.py ./af_20261123

# 自定义忙阈值与报告输出位置
python3 scripts/analyze.py ./af_20261123 --busy 30 --out ./reports

# 自定义角色规则（现场适配新框架线程名，不改代码）
python3 scripts/analyze.py ./af_20261123 --roles ./my_roles.json

# topology_raw.txt 缺失且需要本机补采（同机分析场景）
python3 scripts/analyze.py ./af_20261123
# （默认行为即尝试补采；跨机包缺 topology 时报告退出码 3）
```

## 异常判定（7 类，输出中含完整证据链）

| ID | 异常 | 判定要点 | 严重度 |
|----|------|---------|--------|
| A1 | 忙线程未绑核跨 NUMA 漂移 | 忙% ≥ 阈值(默认50) 且允许掩码跨多 NUMA 且活跃样本实际跨多 node | high |
| A2 | NPU 主机线程与设备 NUMA 错位 | 角色 ∈ 算子下发/拷贝/通信表 且 主要落点 node ∉ NPU 设备 node 集合（numa_node=-1 → 跳过） | high |
| A3 | 忙线程挤核 | 同一 tick 内 ≥3 个忙线程报同一核，出现比例 ≥5% | medium |
| A4 | 掩码震荡 | 单线程允许掩码变更 ≥3 次且线程活跃（OMP 动态绑核特征） | medium |
| A5 | 绑核意图未覆盖 | 线程允许掩码严格宽于主线程掩码（库线程绕过启动绑核） | medium |
| A6 | 观测完整性 | psr ∉ 允许掩码样本比例（tick 内双文件读取竞态预期 <0.5%；>5% 升 high） | info/超5%升high |
| A7 | 线程翻动 | 线程实例数 ≥3× 峰值并发数 | info |

## 输出契约

- `<out>/绑核分析报告.html`：数据质量 + 异常区（每条附证据）+ 线程
  画像表（角色/忙%/允许核/落点分布/迁移下界/掩码变更）+ 掩码时间线 +
  NPU 设备归属 + 口径说明
- `<out>/summary.json`：完整结构化结论（环境/线程画像/findings/
  data_quality），供 agent 直接消费
- stdout：结论行（线程实例/忙线程/异常数）+ 报告路径
- 退出码：`0` 成功（发现异常也算成功——异常是产品不是错误）；`1`
  用法/IO 错误；`2` samples.jsonl 无法解析；`3` 拓扑无法解析且本机
  /sys 无法补采（跨机解析采样包常见）

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `<采样目录>` | collector 输出目录（samples.jsonl + topology_raw.txt） | 必填 |
| `--busy <阈值%>` | 忙线程判定 | 50 |
| `--out <dir>` | 报告输出目录 | 采样目录 |
| `--roles <file>` | 覆盖角色识别配置 | `<skill>/references/roles.json` |
| `--topology-raw <file>` | 覆盖 topology_raw.txt 路径 | `<采样目录>/topology_raw.txt` |

## 约束

- 只读采样目录：不修改任何采样数据；报告阶段也只在输出目录内写文件
- 在线采样能力不提供——需要采数据先运行
  `kptools-thread-affinity-collector`
- NPU 设备按 PCI vendor `0x19e5` 识别；`numa_node=-1`（VM/容器 sysfs
  常见）→ 该设备不参与 A2 判定（无归属数据时下"错位"结论属于编造）
- 角色（mainThread/acl_thread/hccl 等）只做标注不做策略：线程名为数字
  或纯框架名时按观测呈现，不猜角色

### 和同类 skill 的分界

- 采集（找目标机、采样到数据落盘）归 `kptools-thread-affinity-collector`
- 瞬时快照（ps -L 一把抓）归 `kptools-proc-collector`
- 绑核动作/策略执行归 `cpu-affinity-optimization`（app-tuner 子 skill）；
  本 skill 只发现证据并路由
- 训练 rank 的内存 NUMA 归属由 `kptools-train-mem-affinity` 覆盖

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 2 | samples.jsonl 损坏/为空；重新采集 |
| 退出码 3 | topology_raw.txt 缺失且分析机无 /sys（跨机包）——把采样包里的 topology_raw.txt 一并带上 |
| 报告里 L3 域为空 | 采样机 sysfs 无 index3 cache 信息（虚拟机常见），L3 列降级隐藏 |
| 允许掩码全是 0-127 类全掩码 | 进程未做任何绑核（默认状态），忙线程会按 A1 判定漂移 |
| coverage/完整性 A6 比例偏高 | 采样异常（掩码解析竞态或采集器 bug）——先修观测再信结论 |
| 报告里角色列"未匹配" | 线程名不在角色规则内——按观测呈现，加自定义规则用 `--roles` |

## 参考资料

- [数据契约](references/data-contract.md) — 输入（samples.jsonl/topology_raw.txt）与输出（summary.json）的完整 schema
- [报告判读指南](references/report-guide.md) — 字段含义、判据细节、阈值依据、处置路由
- [线程角色模式表](references/thread-role-patterns.md) — PTA/CANN/HCCL/vLLM 线程名→角色映射（真源：cpu-affinity-optimization）
- 角色规则（机器可读）：[roles.json](references/roles.json)
