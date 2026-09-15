# hccn_tool 采集指南

hccn_tool（Huawei Collective Communications Network Tool）是昇腾 NPU 集群网络
管理工具。本文档覆盖命令详解、指标解读、RoCE 带宽时延测试方法与集群网络
故障排查，供深入诊断时参考。

## 1. 工具与依赖

| 项 | 说明 |
|----|------|
| 安装来源 | NPU 驱动安装包，**不依赖 CANN** |
| 安装路径 | 软链 `/usr/bin/hccn_tool` → `/usr/local/Ascend/driver/tools/hccn_tool` |
| 驱动固件下载 | https://www.hiascend.com/hdk/firmware |
| 权限 | 查询类命令 root 与运行用户组（如 HwHiAiUser）均可 |
| 并发 | 不支持多线程并发调用 |
| 前置等待 | OS 启动后驱动加载上限 150 秒，建议等 180 秒再执行网络配置类命令 |
| 容器使用 | 需将宿主机 `/usr/bin/hccn_tool` 挂载进容器 |
| AVI | hccn_tool 无昇腾虚拟化实例（AVI）场景 |

## 2. 芯片物理 ID（Phy-ID）

hccn_tool 的 `-i` 是 NPU 模组 AI 芯片物理 Die ID：

| 产品 | 取值范围 |
|------|---------|
| Atlas 9000 A3 SuperPoD 集群算力系统 | 0~7 |
| Atlas 900 A3 / 800T A3 超节点、800I A3 系列、A200T A3 Box8 | 0~15 |
| 910B（现网常见，8 芯片） | 0~7 |

获取方式：`npu-smi info` 回显中的 Phy-ID 列。每芯片对应一个 RoCE 网口设备
（`-d` 参数：Chip 0 → `hns_0`，Chip 1 → `hns_1`，依此类推）。

本 skill 采集脚本不指定 `-i` 时优先从 `npu-smi info -m` 自动推导 Phy-ID
列表（容器分卡等稀疏芯片场景避免无效扫描，stderr 打印本次遍历的芯片列表），
npu-smi 不可用时兜底盲扫 0~`HCCN_MAX_CHIP`（默认 7）。上表 0~15 的产品
（16 芯片）执行 `collect.sh` 前需导出 `HCCN_MAX_CHIP=15`，遍历与
test-reset 范围才会覆盖 0~15（自动推导时同样按该上限过滤）。

## 3. 只读命令与指标解读

所有查询命令格式为 `hccn_tool [-i %d] -<attr> -g`（`-g` = get）。

### 3.1 -ip — IP 地址与子网掩码

集群 RoCE 网段规划核心。多机训练要求各芯片 IP 在同一规划网段且不冲突。
IPv6 用 `-ip -inet6 -g`。

### 3.2 -link — 网口 link 状态

UP/DOWN。**闪断排查**：结合 `-link_hist -g`（link 历史状态）定位闪断时刻。
注意：执行 hccn_tool 配置命令会触发网口 UP→DOWN，iBMC 上报闪断告警属正常。

### 3.3 -stat — 收发包统计（集合通信质量核心）

关键计数器：

| 计数器 | 含义 |
|--------|------|
| roce_tx/rx 包数与字节数 | 收发总量 |
| roce_rx_err_pkt_num | 错包数。**非零需先排除 roce_test 打流残留**（官方已知：大包短时长打流结束时 RoCE 仍在收发包导致误报） |
| roce_unexpected_ack_num / roce_out_of_order_num / roce_verification_err_num / roce_qp_status_err_num | 软件统计的四类异常报文，可能比实际少（统计口径限制） |
| nic_tx/rx_all_pkg_num / oct_num | 网口总量（网口 DOWN 期间返回 0） |

**采集模式**：先 stat 基线 → 业务/打流 → 再 stat 对比增量。

### 3.4 -status — 网口状态信息

比 -link 更全的端口状态汇总。**权限要求高于普通查询**：非 root/非运行
用户组执行返回 rc=12 `permission denied` 属预期，本 skill wrapper 已将其
降级为提示（不计失败、不影响退出码），权限受限时可用 `-ip`/`-link`/
`-stat` 替代采集。

### 3.5 -bandwidth — 实时带宽收发速率

`hccn_tool -i %d -bandwidth -g [time %d]`，窗口 100~10000ms（默认 1000ms），
单位 MB/sec。训练/推理运行期采样，用于确认跨机流量是否达到预期量级；
流量未达峰值时测量值小于理论值属正常。

### 3.6 -net_health — 网络健康状态

本端与所配置检测对象 IP 的连通状态。前置：驱动侧已配置网络检测对象 IP
（`-detect_ip -s`，配置类命令不在本 skill 范围）。
状态显示 `Init` 通常表示检测对象 IP 未配置（前置条件未满足），而非网络故障。
`Receive timeout` 常见原因：检测 IP 配到了板内 vNIC 网段
（非超节点 192.168.0.x~192.168.7.x，超节点 192.x.x.x 均不支持配置）。

### 3.7 config 聚合 — 网关/MTU/速率/限速

- `-gateway -g`：RoCE 默认网关（rc=2 "no gateway was preset" 为未预设，
  点对点直连组网下属合法状态，wrapper 不计失败）
- `-mtu -g`：MTU（跨机组网两端不一致是经典故障源）
- `-speed -g` / `-eth_rate -g`：网口速率/限速。**注意**：部分驱动版本
  已无 `-eth_rate` 子命令（rc=3 "Get invalid major cmd"），wrapper 自动
  跳过不计失败，限速检查可用 `-speed` 替代

## 4. RoCE 带宽时延测试（主动打流）

### 4.1 命令语法

```
hccn_tool [-i %d] -roce_test <mode> [-s <bytes>] [-D <sec>] [-b] [address]
```

| 模式 | 测试内容 |
|------|---------|
| ib_read_bw | 读操作带宽 |
| ib_write_bw | 写操作带宽 |
| ib_send_bw | 发送操作带宽 |
| ib_read_lat | 读操作时延（仅客户端返回 bytes/iterations/t_min[usec]/t_max[usec] 等） |
| ib_write_lat | 写操作时延 |
| ib_send_lat | 发送操作时延 |
| reset | 复位：终止该 device 侧所有 roce_test 进程 |

关键参数：`-s` 报文大小（1~874736000 B）、`-D` 打流时长（秒，1~86400）、
`-b` 双向带宽（仅 bw 模式）、`address` 对端 IP（**留空即服务端模式**）。
wrapper 的 `--timeout` 超时保护默认 60 秒；**未显式指定 `--timeout` 而给了
`-D` 时自动放宽为 `-D + 30` 秒**（打流时长 + 收尾余量），避免长时打流被
wrapper 提前杀死。

### 4.2 测试方法（跨机两芯片）

```bash
# 机器 B（服务端，芯片 1）
bash scripts/collect.sh test -m ib_read_bw -i 1 --force

# 机器 A（客户端，芯片 0 → 对端 B 的芯片 IP）
bash scripts/collect.sh test -m ib_read_bw -i 0 <B芯片IP> --force
```

1. 先在两端 `stat` 采集基线
2. 服务端先行启动（无 address），客户端连接
3. 测试完成后两端再 `stat`，对比 `roce_rx_err_pkt_num` 增量
4. 异常挂死：`test-reset` 复位 device 侧进程

### 4.3 结果解读

- 时延结果看客户端的 t_min/t_max/t_avg（usec）
- 带宽明显低于链路标称：检查 `-speed`（及 `-eth_rate`，部分驱动版本无此
  子命令）限速、MTU、交换机侧配置
- `roce_rx_err_pkt_num` 测试后非零：先按打流残留解释（大包短时长场景官方
  已知），复测前 `test-reset` + 观察增量

### 4.4 安全红线

- 测试注入流量占用 RoCE 链路，**生产业务运行期禁用**，必须 `--force` 显式确认
- 铜缆不支持 NPU RoCE 端口间环回对接，需经外部交换机
- 光模块/铜缆/AOC 拔插间隔不低于 2 秒

## 5. 故障排查场景

### 5.1 allreduce/HCCL 集合通信慢

1. `collect.sh stat` → 错包/乱序计数是否增长（排除物理层）
2. `collect.sh bandwidth` 训练期采样 → 实际带宽量级
3. `collect.sh ip` + 交换机侧核对 → 网段规划与限速
4. 空闲期 `collect.sh test` 做基准带宽/时延 → 与标称对比
5. 结合 `npu-smi info -t topo` 确认卡间走 HCCS 还是 RoCE 跨机

### 5.2 训练偶发卡顿/中断

1. `collect.sh link` 全芯片扫 DOWN 状态
2. 驱动侧 `-link_hist -g`（本 skill 未封装，可直接执行）看闪断历史
3. `-net_health -g` 检测连通性
4. iBMC 告警对照（注意配置变更引发的正常闪断告警）

### 5.3 discover 状态与处置

| 状态 | 含义 | 处置 |
|------|------|------|
| TOOL_MISSING（容器内） | 容器未挂载工具 | 挂载宿主机 /usr/bin/hccn_tool |
| TOOL_MISSING（物理机） | 驱动未装/损坏 | 重装驱动 |
| DRIVER_LOADING | 驱动加载中 | 等 180 秒 |
| DRIVER_ERROR 且提示设备不存在 | 芯片 Phy-ID 范围不符 | 核对产品芯片数（0-7 或 0-15） |

## 6. 官方文档

- hccn_tool 接口参考：
  https://www.hiascend.com/document/detail/zh/HDK/latest/A3/A3HCCN/hccntool_001.html
- HDK 驱动固件下载：https://www.hiascend.com/hdk/firmware
