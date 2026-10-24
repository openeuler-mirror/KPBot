---
name: kptools-ascend-pd-env
description: >
  采集并对比 vLLM-Ascend P/D 容器环境：系统内核、cgroup 配额、CPU/NUMA 拓扑、
  CANN 与驱动版本、npu-smi 快照、进程命令行与环境变量、Python 包清单。一个容器
  出单环境报告，多个容器或搭配已有采集包则自动对比并标差异。当用户需要排查
  P/D 两侧环境不一致、CANN/驱动/torch_npu/vLLM 版本漂移、HCCL 或 ASCEND 变量
  不同、cpuset/cgroup 配额不同、升级前后或跨机跨时间比环境快照、容器环境存档
  时触发；实时监测、进程级与算子级分析、网络诊断不在范围内。
license: MulanPSL-2.0
compatibility: Linux host with docker; Ascend 相关采集项需要昇腾机器，非昇腾环境上这些项标记为跳过而不报错
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.0.0"
---

# kptools-ascend-pd-env — vLLM-Ascend P/D 环境采集与对比

P/D 分离部署里，P 侧和 D 侧跑在不同容器甚至不同机器上，环境一旦漂移（CANN 版本、
torch_npu 版本、HCCL 环境变量、cpuset、启动参数），表现出来的往往是吞吐掉点或
偶发超时，而不是明确的报错。本 skill 把两侧环境拉平到同一张表上对比。

对外只有一个入口，不分「采集」和「对比」两步：传入的每个对象都会被采集，
凑够两个环境即自动对比。

## 工作流程

1. 检查环境：`bash scripts/discover.sh`
2. 采集并分析：`bash scripts/collect.sh <对象> [更多对象...]`
3. 打开输出目录里的 HTML 报告；要定位具体差异看 `对比/对比结果.json` 或 `对比摘要.md`
4. 字段含义、跳过原因、故障排查见 `references/pd-env-guide.md`

> 只读采集：不安装任何软件、不连网、不改容器、不重启服务。

## 采集命令

```bash
# 采一个容器，出单环境报告
bash scripts/collect.sh vllm-p

# 采 P 和 D 两个容器，采完自动对比
bash scripts/collect.sh vllm-p vllm-d

# 现采一个 + 上周留下的采集包，跨时间对比
bash scripts/collect.sh vllm-p /data/上周-d.tar.gz

# 只比两个已有采集包（本机不采集，可用于跨机器对比）
bash scripts/collect.sh /data/机器A.tar.gz /data/机器B.tar.gz

# 标注本机角色，指定输出目录
bash scripts/collect.sh vllm-p vllm-d --role P --out ./pd-env-20261024
```

对象是容器名还是采集包的判据：**已存在的目录**或**已存在的 `.tar.gz`** 当采集包，
其余一律当本机容器名。写成路径的样子却找不到、或那个文件不能当采集包，会直接报错，
不会拿去当容器名采。一个名字同时又是本地路径时会提示按什么处理了，不会静默少采一个环境。

先用 `docker ps --format 'table {{.Names}}\t{{.ID}}\t{{.Image}}'` 查容器真名。

## 采集内容

容器内按 12 个采集项依次采，每项单独计时、单独超时、单独兜底，一项失败不影响其他项，
失败原因记在结果里：

| 采集项 | 内容 |
|--------|------|
| `system` | os-release、uname、glibc、CPU 核数、内存 |
| `kernel` | 内核版本与相关参数 |
| `cgroup` | 容器 CPU/内存配额、cpuset、cgroup 版本 |
| `tools` | gcc/python/pip 等工具链位置与版本 |
| `npu_smi` | NPU 设备列表与状态（非昇腾机器跳过） |
| `lscpu` | CPU 架构与拓扑 |
| `numa` | NUMA 节点与距离 |
| `ascend_versions` | CANN、驱动、固件版本文件 |
| `ascend_dirs` | Ascend 安装目录结构 |
| `git` | 仓库 commit 与 dirty 状态（只读，不写 .git） |
| `processes` | 进程命令行、可执行文件、工作目录、环境变量 |
| `python_profiles` | Python 解释器身份、sys.prefix、包清单 |

对比时摊平成 6 个分类：`基础信息`、`启动参数`、`核心包`、`其他包`、
`进程环境变量`、`Docker配置环境变量`。`核心包`和`启动参数`整类算性能相关，其余按
名字匹配（HCCL、NUMA、cpuset、线程数、cache、batch 等）标记，报告里单独计数。

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `<对象>` | 容器名或采集包路径，可写多个、可混写（必填） | - |
| `--out <目录>` | 输出目录 | `pdenv_<UTC时间>` |
| `--role P\|D\|PD\|other` | 本机角色标记，只写进 meta，不改采集内容 | 不标记 |
| `--python <路径>` | 容器内 Python 路径。路径不存在或不可执行时不报错、退出码仍是 0：探针改用自动找到的解释器，其他采集项照常，但这个容器的 Python 画像会是空的（没有解释器信息和包清单），原因记在 `inventory.json` 的 `collection_status.attempts` 里 | 自动探测 |
| `--help` | 显示帮助 | - |

超时、进程数上限、画像数上限是内部固定值（命令 15s、npu-smi 25s、Python 20s、
包列表 60s、git 10s；单容器总预算 300s），不开放成命令行开关。

## 输出契约

```
<输出目录>/
├── 采集/                    只在采了本机容器时才有；只给采集包时不生成
│   ├── 环境报告.html        这次采到的环境，只采一个也能直接打开看
│   ├── SUMMARY.txt          文本摘要（每项成功/跳过/失败及原因）
│   └── <容器名>/            结构化结果（inventory.json / raw.jsonl / docker_inspect_safe.json）
├── 采集.tar.gz              同上，只在采了本机容器时才有；可拷到其他机器上继续对比
└── 对比/                    只在凑够两个及以上环境时才有
    ├── 环境对比报告.html    带差异高亮和「仅看差异」开关
    ├── 环境简表.csv         每个环境一行的概览
    ├── 完整总表.csv         全部可比较项的矩阵
    ├── 对比摘要.md          文本摘要
    └── 对比结果.json        结构化结论，供 agent 直接消费
```

- stdout：进度、结论行（环境数 / 对比组数 / 差异数 / 性能相关差异数）、报告路径
- stderr：诊断（采集包读不进来的原因、跳过原因、中断提示）；失败提示先于结论打印
- 退出码：`0` 成功（只采一个容器也算成功，出单环境报告）；`1` 参数错误、输出目录问题，
  或给的采集包有没读进来的（这时只要还剩环境可报就是 1）；`2` 环境不可用（没有 python3，
  或要采本机容器却连不上 docker）；`3` 没有任何对象采集成功、采集包全都读不出来，
  或给的对象本身就凑不出两个环境；`130` 被 Ctrl-C 中断（已采到的部分仍会落盘）

## 约束

- 宿主机要有 `python3 >= 3.6`，不需要任何第三方库；采本机容器还要能连上 docker
- 容器里要有 Python 才能采完整画像；没有时该容器只能拿到 docker inspect 层面的信息
- 非昇腾机器上 `npu_smi`、`ascend_versions`、`ascend_dirs` 标记为跳过，不算失败
- 一次只能比一台机器上的容器；跨机器要先在各自机器上采，把 `.tar.gz` 拷到一起再比
- 输出目录必须为空或不存在；同一个输出目录不允许两个采集同时写（有锁文件保护）
- 只读采集：不写容器、不装包、不连网；git 项用 `--no-optional-locks` 且不写 `.git/index`
- 取值一律原样记录，不做遮盖；采集结果里会有完整的环境变量和启动参数，采集包按内部资料保管，对外分享前请自查

### 和同类 skill 的分界

- NPU 实时状态（利用率、温度、功耗、HBM 的持续监测）由 `kptools-ascend-smi` 覆盖；
  本 skill 只在采集那一刻取一次 `npu-smi` 快照存档
- 进程级性能分析（线程-CPU 分布、上下文切换、CPU 迁移、亲和性掩码）由
  `kptools-proc-collector` 覆盖；本 skill 的 `processes` 项只记命令行、可执行文件和环境变量
- 网络链路（HCCN 网卡配置、链路状态、连通性）由 `kptools-ascend-hccn` 覆盖；
  本 skill 不调用 `hccn_tool`
- 算子级 profiling 由 `ascend-vllm-profiler` 覆盖，参数调优由 `kpbot-app-tuner` 覆盖；
  本 skill 只回答两侧环境的差异

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| `discover.sh` 返回 `PYTHON_MISSING` | 宿主机没有 python3 >= 3.6，装系统自带的即可 |
| `discover.sh` 返回 `DOCKER_MISSING` | 没装 docker，本机容器采不了；已有采集包仍可对比 |
| `discover.sh` 返回 `DOCKER_UNAVAILABLE` | dockerd 没起，或当前用户不在 docker 组 |
| `下面这些写成了路径，但不能当采集包用` | 路径敲错了，或那个文件既不是目录也不是 `.tar.gz`；容器名里不要带 `/` 和 `.tar.gz` |
| `连不上 docker，本机容器采不了` | dockerd 没起、或 `DOCKER_HOST` 不对；退出码 2，不会写出"成功 0 项"的空报告 |
| `提示：X 是本地已存在的目录，已按采集包处理` | 同名目录把容器名抢走了。要采容器就换个工作目录再跑 |
| `提示：X 本地有同名文件，已按容器名处理` | 只有目录和 `.tar.gz` 算采集包，普通文件不算，所以按容器名采 |
| `输出目录不是空的` | 换一个 `--out`，或先清空 |
| `输出目录正在被另一个 collect 使用` | 上次异常退出残留锁文件，按提示删掉再试 |
| `有 N 个采集包没有读进来` | 包损坏、格式无法识别或不是采集包；退出码 1，报告只含读进来的那些 |
| `只读到 1 个环境，没法对比` | 不够两个环境；单份结果看 `采集/环境报告.html` |
| 某个采集项显示「跳过」 | 该机器上没有这一项对应的来源（如非昇腾机器的 npu-smi），原因见 SUMMARY.txt |
| 某个采集项显示「超时」 | 容器负载高或命令卡住；重跑一次，仍超时看 `references/pd-env-guide.md` |

## 参考资料

- [P/D 环境采集与对比指南](references/pd-env-guide.md) — 采集项详解、字段含义、
  跨机跨时间用法、安全与数据、故障排查
