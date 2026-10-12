---
name: kptools-ascend-smi
description: >
  使用 npu-smi 采集昇腾 NPU 设备状态：健康状态、功耗、温度、HBM/AICore
  利用率、内存带宽、轮询监测、芯片映射、拓扑结构和 HCCS 链路信息。
  当用户需要 NPU 状态检查、昇腾设备健康检查、NPU 利用率、AICore 占用率、
  HBM 使用率、NPU 温度、NPU 功耗、NPU 内存带宽、昇腾芯片监测、NPU 拓扑、
  HCCS 链路、NPU 亲和性、device 权限检查、npu-smi 命令时触发。
license: Proprietary
compatibility: aarch64/x86_64, requires Ascend NPU driver (npu-smi ships with driver)
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.0.0"
---

# kptools-ascend-smi — 昇腾 NPU 状态采集

npu-smi（NPU System Management Interface）是 NPU 设备管理工具，随 NPU 驱动
安装（`/usr/local/sbin/`、`/usr/local/bin/`），无需单独部署。

## 工作流程

1. 检查环境就绪：`bash scripts/discover.sh`
2. 采集数据：`bash scripts/collect.sh <subcommand> [options]`
3. 诊断失败时查看 `references/npu-smi-guide.md` 的输出解读与故障排查

> npu-smi 不支持多线程并发，本 skill 所有子命令顺序执行。

## 采集命令

### 设备信息与健康检查

```bash
# 基本信息 + 设备列表（Health/Power/Temp/HBM/AICore）
bash scripts/collect.sh info

# 芯片统计信息（Aicore/Aivector Usage，遍历所有设备）
bash scripts/collect.sh usages
bash scripts/collect.sh usages -i 0          # 指定设备

# 健康状态（遍历所有设备）
bash scripts/collect.sh health
bash scripts/collect.sh health -i 0

# 注意：设备号可能稀疏（容器分卡场景如 NPU ID=3），先用 info -l 查真实 NPU ID

# 版本信息（npu-smi + CANN + torch_npu，后两项失败不阻断）
bash scripts/collect.sh version

# /dev/davinci* 设备权限（排查容器/用户组权限问题）
bash scripts/collect.sh permission
```

### 轮询监测（推理/训练压测时持续观察）

```bash
# 每 1 秒轮询一次，默认 600 秒超时自动停止（Agent/脚本场景务必带 -T 控制时长）
bash scripts/collect.sh watch -i 0 -T 60

# 每 5 秒采样 120 秒，监测功耗/温度/AICore/内存带宽/NPU 综合利用率
bash scripts/collect.sh watch -i 0 -d 5 -T 120 -s ptmbn

# 指定芯片监测；-T 0 表示不限时（交互式场景，Ctrl+C 停止）
bash scripts/collect.sh watch -i 0 -c 0 -d 1 -s a -T 0
```

watch 类型字母（可组合）：`p`=功率 `t`=温度 `a`=AI Core `i`=AI CPU
`c`=Ctrl CPU `m`=内存占用率 `b`=内存带宽占用率 `n`=NPU 综合利用率
`u`=AI Cube

> watch 为无限轮询命令，wrapper 以 SIGINT 超时保护（默认 600s，`-T 0` 关闭），
> 超时/Ctrl+C 停止时已采集数据会完整输出，退出码按成功处理；
> 输出实时流式打印（不缓冲至命令结束），长时间轮询期间可见增量数据。

> 开启 profiling 时 AI Core(%) 返回 0 无实际意义。

> watch 的 `m`（Memory %）与 `usages` 的 HBM Usage Rate 采样与取整口径不同，
> 同一状态下可有 ±1% 差异（如 watch 显示 5%、usages 显示 4%）；需要精确值时
> 以 `info`（MB 绝对值）或 `usages`（百分比）为准。

### 拓扑与 HCCS 链路

```bash
# CPU/NPU 亲和性 + 多 NPU 拓扑（旧驱动或设备不支持时自动降级为 -m + lspci）
bash scripts/collect.sh topology

# HCCS 链路信息：速率/状态/收发包/误码（旧驱动自动降级为 -t board；
# 设备/芯片号无效时不降级，直接报错并提示查询有效 ID）
bash scripts/collect.sh hccs -i 0 -c 0

# HCCS 链路带宽（-time 为采样间隔 ms，1~1000）
bash scripts/collect.sh hccs-bw -i 0 -c 0 -time 1000
```

> PCIe 卡不一定无 HCCS：多芯片 PCIe 卡（如 Atlas 300I Duo，实测 2×Ascend910）
> 的卡内片间 HCCS 查询有效且数据可用于诊断（对端不存在的 lane 计数为 0 属正常）；
> 查询报不支持/失败或全 0 时才视为该卡型无有效 HCCS，跨卡互联场景另需结合
> topology 与 hccn 侧数据判断。

### 全量采集

```bash
# 顺序执行 info/usages/health/topology/permission/version
bash scripts/collect.sh all
```

## 参数速查

| 子命令 | 参数 | 说明 |
|--------|------|------|
| info / permission / topology | - | 无参数 |
| usages / health | `-i <id>` | 设备 ID（`info -l` 中的 NPU ID），缺省遍历所有 |
| watch | `-i <id>` | 设备 ID，必填 |
| watch | `-c <chip>` | 芯片 ID（`info -m` 中的 Chip ID），可选 |
| watch | `-d <sec>` | 轮询间隔 1~100 秒，默认 1 |
| watch | `-T <sec>` | 超时保护 0~86400 秒，默认 600；0=不限时 |
| watch | `-s <types>` | 监测类型字母组合，默认 `ptain`（不含 MCU 芯片） |
| hccs / hccs-bw | `-i <id> -c <chip>` | 设备 ID + 芯片 ID |
| hccs-bw | `-time <ms>` | 采样间隔 1~1000 ms |
| 所有 | `--tool <path>` | 显式指定 npu-smi 路径 |

## 输出契约

- stdout：底层命令原始输出（带 `===` 分节标题），不做格式转换
- stderr：诊断信息（工具定位失败、降级提示、单条命令失败原因）
- 退出码：`0` 成功；`2` 环境不可用；`3` 参数错误；`4` 命令执行失败

## 约束

- npu-smi 随驱动安装，本 skill 无 deploy 脚本；环境不可用时按 discover 提示安装驱动
- 查询类命令 root 和运行用户组（HwHiAiUser）均可执行
- 虚拟机/容器场景部分子命令受限，见 `npu-smi -t <type>` 各命令"部署场景支持情况"表
- 本 skill 仅封装只读查询；固件升级、复位、vNPU 配置等写操作不在范围内

## 错误处理

- `discover.sh` 返回 `NO_ASCEND_HARDWARE` → 无昇腾 PCIe 设备（lspci -d 19e5:）
- `discover.sh` 返回 `DRIVER_MISSING` → 驱动未安装/未加载，检查 /dev/davinci* 与驱动安装
- `discover.sh` 返回 `DRIVER_LOADING` → 驱动加载中，等待 180 秒后重试（OS 启动后驱动加载上限 150 秒）
- `discover.sh` 返回 `PERMISSION_DENIED` → 当前用户不在运行用户组，参考指南中的权限排查
- collect 报 `TOOL_MISSING` → npu-smi 不在 PATH，用 `--tool` 显式指定或重装驱动
- topology 降级提示 → 驱动版本较老（无 `-t topo`），输出为 `-m` + `lspci` 组合
- `usages`/`health`/`hccs` 等带 `-i` 查询失败 → stderr 提示运行 `info` 查有效
  NPU ID（设备号可能稀疏，如容器分卡 NPU ID=3）
- `hccs` 报 Invalid card/chip id → 设备/芯片号无效，直接报错不触发 board 降级

## 参考资料

- [npu-smi 采集指南](references/npu-smi-guide.md) — 命令详解、输出字段解读、版本差异、故障排查
