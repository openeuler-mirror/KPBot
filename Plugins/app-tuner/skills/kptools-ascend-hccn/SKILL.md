---
name: kptools-ascend-hccn
description: >
  使用 hccn_tool 采集昇腾 NPU 集群网络状态：RoCE 网卡 IP、网口 link 状态、
  收发包统计、错包计数、实时带宽速率、网络健康状态，以及 RoCE 带宽时延
  主动测试。当用户需要 NPU 集群网络诊断、RoCE 网络检查、NPU 网口状态、
  集合通信慢排查、allreduce 慢、HCCL 通信瓶颈、NPU 网卡 IP 查询、
  RoCE 带宽测试、RoCE 时延测试、NPU 网口带宽、错包统计、网络闪断排查、
  hccn_tool 命令时触发。
license: Proprietary
compatibility: aarch64/x86_64, requires Ascend NPU driver (hccn_tool ships with driver)
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.0.0"
---

# kptools-ascend-hccn — 昇腾 NPU 集群网络采集

hccn_tool（Huawei Collective Communications Network Tool）是 NPU 集群网络
管理工具，随 NPU 驱动安装（软链 `/usr/bin/hccn_tool`，实际位于
`/usr/local/Ascend/driver/tools/`），无需单独部署。

## 工作流程

1. 检查环境就绪：`bash scripts/discover.sh`
2. 采集只读数据：`bash scripts/collect.sh <subcommand> [options]`
3. 需要 baseUrl 打流验证时，谨慎使用 `test` 子命令（见安全约束）
4. 深入诊断参考 `references/hccn-tool-guide.md`

> hccn_tool 不支持多线程并发；本 skill 所有子命令顺序执行。

## 重要概念：芯片物理 ID（Phy-ID）

hccn_tool 的 `-i` 参数是**芯片物理 ID**（NPU 模组 AI 芯片物理 Die ID），
取值范围 0~7（SuperPoD 集群）或 0~15（超节点/Box 系列），通过
`npu-smi info` 回显获取。本 skill 中：

- 不指定 `-i` 时默认遍历：优先从 `npu-smi info -m` 自动推导 Phy-ID 列表
  （容器分卡等稀疏芯片场景避免无效扫描），npu-smi 不可用时兜底盲扫
  0~HCCN_MAX_CHIP，stderr 会打印本次遍历的芯片列表
- `-i <chip>` 指定单芯片（无效芯片号显式报错，不静默跳过）
- 910B 常见配置为 8 芯片（0~7）
- 16 芯片产品（Atlas 900 A3 / 800T A3 超节点、800I A3、A200T A3 Box8，
  Phy-ID 0~15）：执行前导出 `HCCN_MAX_CHIP=15`，遍历/reset 范围同步扩展到 0~15

## 采集命令

### 只读采集（默认安全）

```bash
# RoCE 网卡 IP 地址与子网掩码（遍历芯片 0~7）
bash scripts/collect.sh ip
bash scripts/collect.sh ip -i 0             # 指定芯片

# 网口 link 状态（UP/DOWN，闪断排查）
bash scripts/collect.sh link -i 0

# 收发包与错包统计（roce_rx_err_pkt_num 等，集合通信质量核心指标）
bash scripts/collect.sh stat -i 0

# 网口状态信息
bash scripts/collect.sh status -i 0

# 实时带宽收发速率（默认 1000ms 窗口，可指定 100~10000ms）
bash scripts/collect.sh bandwidth -i 0 -t 1000

# 网络健康状态（需驱动侧预配网络检测对象 IP）
bash scripts/collect.sh net-health -i 0

# RoCE 配置聚合（网关/MTU/速率/限速；网关未预设与 eth_rate 不受支持属预期，不计失败）
bash scripts/collect.sh config -i 0

# 只读全家桶（ip/link/stat/status/config）
bash scripts/collect.sh all
```

### RoCE 带宽时延测试（主动打流，⚠️ 慎用）

```bash
# 客户端：对端地址 192.168.100.101 做读带宽测试
bash scripts/collect.sh test -m ib_read_bw -i 0 192.168.100.101 --force

# 写时延测试（包大小 65536B，默认超时 60s）
bash scripts/collect.sh test -m ib_write_lat -i 0 -s 65536 192.168.100.101 --force

# 服务端模式（对端执行，address 留空等待连接）
bash scripts/collect.sh test -m ib_read_bw -i 1 --force

# 测试异常挂死时复位测试进程
bash scripts/collect.sh test-reset -i 0
```

test 模式：`ib_read_bw`（读带宽）、`ib_write_bw`（写带宽）、`ib_send_bw`
（发送带宽）、`ib_read_lat`（读时延）、`ib_write_lat`（写时延）、
`ib_send_lat`（发送时延）。

## 安全约束（test 子命令）

- **必须显式 `--force`**：测试会注入流量占用 RoCE 链路，影响在跑业务
- 建议先 `stat` 采集基线，测试后再 `stat` 对比（官方已知：大包短时长打流
  会造成 `roce_rx_err_pkt_num` 非零，属打流残留而非网络故障）
- 默认 60 秒超时保护（`--timeout <sec>` 调整），防止命令挂死；未显式指定
  `--timeout` 而给了 `-D` 时自动放宽为打流时长 + 30s，避免长时打流被提前杀死
- 测试异常时用 `test-reset`（`-roce_test reset`）终止 device 侧测试进程
- 本 skill 不封装任何配置写入类命令（-ip -s、-mtu -s 等），只读 + 测试

## 参数速查

| 子命令 | 参数 | 说明 |
|--------|------|------|
| ip/link/stat/status/net-health/config | `-i <chip>` | 芯片物理 ID，缺省自动推导/遍历 |
| bandwidth | `-i <chip>` `-t <ms>` | 采样窗口 100~10000ms，默认 1000 |
| test | `-m <mode>` | 测试模式（ib_read_bw 等 6 种），必填 |
| test | `-i <chip>` | 芯片物理 ID |
| test | `-s <bytes>` | 报文大小 1~874736000，默认 65536 |
| test | `-D <sec>` | 打流时长 1~86400（秒） |
| test | `-b` | 双向带宽测试（仅 bw 模式） |
| test | `--timeout <sec>` | 超时保护 5~86430，默认 60；未显式指定且给了 `-D` 时自动为 `-D+30` |
| test | `<address>` | 对端 IP（服务端模式留空） |
| test | `--force` | 确认注入流量（必填） |
| test-reset | `-i <chip>` | 复位该芯片 device 侧测试进程 |
| 所有 | `--tool <path>` | 显式指定 hccn_tool 路径 |

## 输出契约

- stdout：底层命令原始输出（带 `===` 分节标题），不做格式转换；多芯片遍历时
  每个芯片分节带 `### chip N` 标记，失败时紧跟 `### chip N: ... rc=N` 诊断行
  （与分节同流输出，`2>&1` 合并查看时顺序不交错）；预期内失败（网关未预设、
  eth_rate 不支持、status 权限受限）标注 `expected on this system`
- stderr：诊断信息（工具定位失败、超时提示、芯片列表自动推导结果）
- 退出码：`0` 成功（含预期内降级）；`2` 环境不可用；`3` 参数错误；`4` 命令执行失败

## 错误处理

- `discover.sh` 返回 `TOOL_MISSING` 且位于容器内 → 需挂载宿主机
  `/usr/bin/hccn_tool` 进容器（参考 NPU 驱动安装指南"容器内安装"章节）
- `DRIVER_LOADING` → 驱动加载中，等 180 秒重试
- `stat` 显示 `roce_rx_err_pkt_num` 非零 → 先确认是否刚做过 roce_test 打流
  （打流残留），再判定网络异常
- `net-health` 报 `Receive timeout` → 检测对象 IP 配置为板内 vNIC 网段
  （192.168.0.x~192.168.7.x 或超节点 192.x.x.x 不支持配置）
- `net-health` 状态为 `Init` → 驱动侧未配置网络检测对象 IP（`-detect_ip -s`，
  配置类命令不在本 skill 范围），属前置条件未满足而非网络故障
- `link` 显示 DOWN 且刚做过配置变更 → 执行配置命令会触发网口 UP→DOWN 闪断，
  iBMC 会上报告警，属正常现象
- `status` 返回 `permission denied`（rc=12）→ 该命令权限要求高于普通查询，
  非 root/非运行用户组用户受限属预期（wrapper 已降级为提示，不计失败、
  不影响退出码），可用 `ip`/`link`/`stat` 替代采集
- `config` 中 `-gateway` 返回 rc=2 "no gateway was preset" → 网关未预设，
  点对点直连组网下属合法状态，wrapper 不计失败
- `config` 中 `-eth_rate` 返回 rc=3 "invalid major cmd" → 该驱动版本无此
  子命令（较新驱动已移除/改名），wrapper 自动跳过不计失败；限速检查可用
  `-speed` 替代
- 芯片遍历时个别芯片报错 → stdout 该芯片分节后紧跟 `### chip N: ... failed rc=N`
  记录，其余芯片照常采集；遍历范围见 stderr 的芯片列表提示（自动推导或 0~MAX_CHIP）

## 参考资料

- [hccn_tool 采集指南](references/hccn-tool-guide.md) — 命令详解、指标解读、
  RoCE 测试方法、集群网络故障排查
