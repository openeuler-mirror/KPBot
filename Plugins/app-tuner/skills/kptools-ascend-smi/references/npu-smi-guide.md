# npu-smi 采集指南

npu-smi（NPU System Management Interface）是昇腾 NPU 设备管理工具。本文档覆盖
采集命令详解、输出字段解读、版本差异与故障排查，供深入诊断时参考。

## 1. 工具与依赖

| 项 | 说明 |
|----|------|
| 安装来源 | NPU 驱动安装包（npu-driver），**不依赖 CANN** |
| 安装路径 | `/usr/local/sbin/npu-smi`、`/usr/local/bin/npu-smi` |
| 驱动固件下载 | https://www.hiascend.com/hdk/firmware |
| 权限 | 查询类命令 root 与运行用户组（如 HwHiAiUser）均可 |
| 并发 | 不支持多线程并发调用 |
| 前置等待 | OS 启动后驱动加载上限 150 秒，建议等 180 秒再查询 |

容器场景：物理机+普通/特权容器可查询（普通容器内仅见本容器进程）；
AVI 场景下 vNPU 相关查询有限制。

## 2. 核心命令与输出解读

### 2.1 npu-smi info — 基本信息

```
| NPU Name | Health | Power(W) Temp(C) Hugepages-Usage(page) |
| Chip     | Bus-Id | AICore(%) Memory-Usage(MB) HBM-Usage(MB) |
```

关键字段：

| 字段 | 解读 |
|------|------|
| Health | `OK` 正常；`Alarm` 告警；`Warning/Fault` 需结合 `health` 子命令与日志进一步定位 |
| Power(W) | 整卡功耗；压测期异常偏低常意味着算子未跑满或进程未绑定到该卡 |
| Temp(C) | 芯片温度 |
| AICore(%) | AI Core 利用率（注意：开启 profiling 时返回 0，无意义） |
| Memory-Usage(MB) | DDR 内存 |
| HBM-Usage(MB) | 片上 HBM；`used / total` 接近上限需检查内存泄漏/切分策略 |

空闲状态 35M~45M 片上内存占用（TaiShan 核启动加载）属正常；无进程时显示
"No running processes found in NPU card_id"；每芯片最多显示 8 个 device 侧
SVM 进程。

### 2.2 npu-smi info -l — 设备列表

输出 NPU ID（供 `-i` 参数使用）与总线信息。本 skill 的 `usages/health/watch`
未指定 `-i` 时自动从该输出枚举设备。

### 2.3 npu-smi info -m — 芯片映射

设备/芯片 ID 映射（供 `-c` 参数使用）。Chip ID 不含 MCU 芯片。

### 2.4 npu-smi info -t usages -i id — 芯片统计

Aicore Usage Rate(%)、Aivector Usage Rate(%) 等统计信息。
**必须携带 `-i id`**。开启 profiling 时 Aicore/Aivector 返回 0 无意义。

### 2.5 npu-smi info watch — 轮询监测

```bash
npu-smi info watch                                  # 所有设备
npu-smi info watch -i 0                             # 指定设备
npu-smi info watch -i 0 -c 0 -d 5 -s ptmbn          # 指定芯片/间隔/类型
```

| watch 字母 | 含义 |
|-----------|------|
| p | 功率 |
| t | 温度 |
| a | AI Core 占用率 |
| i | AI CPU 占用率 |
| c | Ctrl CPU 占用率 |
| m | 内存占用率 |
| b | 内存带宽占用率 |
| n | NPU 综合利用率 |
| u | AI Cube 占用率 |

诊断建议：训练/推理压测时 `n`（综合利用率）与 `b`（内存带宽）是判断
算力瓶颈 vs 带宽瓶颈的核心指标；`a` 高 `b` 低→算力瓶颈，反之→带宽瓶颈。
`-d` 取值 1~100 秒；不支持 MCU 芯片。

注意：watch 的 `m`（内存占用率）与 `usages` 的 HBM Usage Rate 采样/取整
口径不同，同一状态下可有 ±1% 差异；精确值以 `info`（MB）或 `usages`（%）为准。

### 2.6 npu-smi info -t health -i id — 健康状态

独立查询健康状态，比 info 主表更详细（含各级故障码）。

### 2.7 npu-smi info -t topo — 拓扑

CPU 与 NPU 亲和性关系 + 多 NPU 间拓扑（HCCS/PCIe 互联矩阵）。
**亲和性是 CPU 绑核优化的依据**：跨 NUMA 访问 NPU 会显著增加时延。
旧驱动（HDK 23.x 及部分 910B 驱动）无此命令，本 skill 自动降级为
`info -m` + `lspci -d 19e5: -v` 组合。

### 2.8 npu-smi info -t hccs / -t hccs-bw — HCCS 链路

```bash
npu-smi info -t hccs -i 0 -c 0                     # 速率/状态/收发包/误码
npu-smi info -t hccs-bw -i 0 -c 0 -time 1000       # 链路带宽（采样 1~1000ms）
```

HCCS（Huawei Cache Coherence System）是 NPU 间片间互联。多卡训练中
`hccs` 的误码计数增长或 `hccs-bw` 远低于标称值，指向卡间通信瓶颈。
PCIe 卡需区分：多芯片卡（如 Atlas 300I Duo，实测 2×Ascend910）卡内片间
HCCS 查询有效、数据可用于诊断（对端不存在的 lane 计数为 0 属正常）；查询
报不支持/失败或全 0 才视为该卡型无有效 HCCS。旧驱动无 `-t hccs`，
本 skill 自动降级 `-t board`；设备/芯片号无效（Invalid card/chip id）
时不降级，直接报错并提示运行 `info`/`info -m` 查询有效 ID。

## 3. 版本差异对照

| 能力 | 新驱动（HDK 26.x） | 旧驱动（HDK 23.x，现网 910B 常见） |
|------|--------------------|-------------------------------------|
| 拓扑 | `info -t topo` | 无，用 `info -m` + `lspci` |
| HCCS | `info -t hccs` / `-t hccs-bw` | `info -t board`（板卡信息含 HCCS） |
| 统计 | `info -t usages -i id` | 同左（均有） |
| 监测 | `info watch` | 部分版本无 |

本 skill 的降级逻辑自动适配两者，降级发生时 stderr 有提示。

## 4. 故障排查

### 4.1 discover 状态与处置

| 状态 | 含义 | 处置 |
|------|------|------|
| NO_ASCEND_HARDWARE | lspci 无 19e5: 设备 | 确认机型/插槽；无 NPU 则不适用本 skill |
| DRIVER_MISSING | 无 /dev/davinci* 节点 | `lsmod \| grep -i davinci`；重装驱动 |
| TOOL_MISSING | npu-smi 不在 PATH | 重装驱动或 `--tool` 指定路径 |
| DRIVER_LOADING | 驱动加载中 | 等待 180 秒重试 |
| PERMISSION_DENIED | 用户无权限 | `sudo usermod -aG HwHiAiUser $USER` 后重新登录 |

### 4.2 常见问题

- **AICore(%) 恒为 0**：确认是否开启了 profiling（官方已知限制）。
- **容器内看不到宿主机进程**：普通容器仅显示本容器进程，特权容器可见全部。
- **HBM-Usage 异常增长**：结合 `info -t memory -i id`（内存信息）与
  `info -t common -i id`（常用信息）进一步定位。
- **info 显示 Alarm 但不知原因**：跑 `health` 子命令拿详细状态码，配合
  `/var/log/ascendsec/` 与 dmesg 排查。
- **多线程并发调用失败**：npu-smi 不支持并发，顺序重试。

## 5. 官方文档

- npu-smi 命令参考（A2 系列）：
  https://www.hiascend.com/document/detail/zh/HDK/latest/A2/A2npu/npusmi_0004.html
- HDK 驱动固件下载：https://www.hiascend.com/hdk/firmware
