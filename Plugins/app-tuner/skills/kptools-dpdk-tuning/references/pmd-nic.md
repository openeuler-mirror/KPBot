# M5 PMD / 网卡驱动层调优

数据面性能大头。版本敏感度最高的模块——**具体驱动行为一律先查对应版本驱动文档**
（hns3/i40e/ice/mlx5/virtio 等，doc.dpdk.org/guides/nics/<driver>.html）。

## 队列与核的映射（第一杠杆）

- RX 队列数 ≥ 参与 RX 的 worker 数；理想形态：**每 worker 独占一条队列，同 socket**。
- 队列数 > worker 数：闲置队列浪费设备资源；队列数 < worker 数：多核共享队列产生
  原子争抢 [经验；共享 rxq 需显式 `--rxq-share` + shared-rxq 引擎，testpmd 官方]。
- RSS 负载均衡：`--rss-ip` / `--rss-udp`（testpmd 官方）；业务流数少时 RSS 会导致
  单队列倾斜——先看每队列收包分布（`show port xstats`）。

## 描述符与 burst

| 参数 | testpmd 默认 [官方] | 调优方向 |
|------|--------------------|----------|
| `--rxd` | 128 | 加大吸收突发/降低 imissed；代价内存与缓存足迹 |
| `--txd` | 512 | 加大支持大 burst 发送/减少 TX 重试 |
| `--burst` | 32 | 吞吐场景 32~64；过大增加 cache miss；时延场景更小 |
| `--rxfreet` | 0 | RX 释放阈值（部分驱动支持批量回环描述符） |
| `--txfreet` | 0 | TX 释放阈值，减少写回次数（常见 32/64 起试） |

- 描述符数量通常须为 2 的幂、且有上限（硬件相关）——以目标环境驱动文档为准 [官方要求位置]。
- **判定描述符不足**：`rx imissed`（无描述符而丢弃）持续增长 [官方 rte_eth_stats.imissed 语义]。
- **判定 TX 阻塞**：worker 周期性等待 tx done 清理 → 加 txd/txfreet 或减少单次 burst。

## 阈值寄存器（pt/ht/wt）

testpmd 默认值 [官方]：rxpt=8 rxht=8 rxwt=4 rxfreet=0；txpt=36 txht=0 txwt=0 txfreet=0 txrst=0。
- 语义：预取/宿主/写回阈值（pthresh/hthresh/wthresh）。
- **注意**：这类寄存器主要在部分 Intel 类驱动生效；hns3 等驱动可能不支持或忽略
  ——先查驱动文档，无效参数不要反复试 [经验]。

## Vector 路径

- 全局开关：`--force-max-simd-bitwidth`（x86 上限 512 / aarch64 256 / 128 / 64，0 恢复默认）[官方 EAL]。
- 各驱动 vector 路径的启用条件（编译选项、CPU flag、队列配置、offload 约束）**随驱动和
  版本变化**：查驱动文档与驱动源码 `vector` 相关文件。
- 验证收益：上限位宽 vs 64 双基线（M8 方法）；同时核对 EAL 启动日志中 PMD 报告的实际路径。

## Offload 裁剪

- 每个 RX/TX offload 都可能把收发包路径从 vector 降级到 scalar（驱动文档有 per-offload
  约束）[官方机制]。
- 原则：用不到的 offload 全部关闭（testpmd：`--rx-offloads=0x..` `--tx-offloads=0x..`，
  运行时 `port config <id> rx_offload xxx on/off`）[官方]。
- checksum 场景：硬件校验 vs 软件校验的选择要按业务正确性约束评估。

## RSS / 流分流

- flow isolate 模式：`--flow-isolate-all`（全部流量按 flow rule 进指定队列，绕过 RSS）
  [官方 testpmd]。
- 精确分流到指定核：rte_flow + 队列动作；多 NIC 多队列场景配合每核独立 mempool。
- **经验**：业务做五元组分流时，先确认哈希字段覆盖业务流量特征（全是同源 IP 的流量
  RSS 无法打散——需要 flow 规则或应用层分发）。

## 中断 vs 轮询

- 纯轮询是 DPDK 默认高性能路径；中断路径（LSC 除外）一般不用于高吞吐。
- 低流量省电场景走 PMD power management（M7）。
- `--no-lsc-interrupt` / `--no-rmv-interrupt`：测试时可关闭链路/设备事件中断减少干扰
  [官方 testpmd]。

## 链路与对端

```bash
ethtool -a <ifname>          # 绑定 DPDK 前查流控；吞吐测试建议对端与本端流控关闭 [经验]
ethtool -S <ifname>          # 绑定前采集内核计数基线（CRC/丢弃）
```

- 流控开启时，拥塞会以 pause 帧反压，表现为"不丢包但吞吐低"——两类症状要分清。

## hns3 / Kunpeng 920 特有

- 驱动文档：[hns3.rst](https://doc.dpdk.org/guides/nics/hns3.html)
  （能力、约束、限制以目标环境版本为准）。
- 常见硬件形态：Kunpeng 920 + Hi1822（4x25GE，内核 hinic / DPDK hinic PMD）/
  HNS RDMA 网络控制器 [经验，现场核对]。
- [经验] Hi1822/hinic PMD 绑到 vfio-pci 后，若 EAL 日志报
  `NET_HINIC: Check card workmode failed` → `Create nic device failed`
  （PMD 初始化失败），多与卡固件工作模式/管理通道状态相关，属机器级问题：
  先核对卡固件与工作模式，勿在 PMD 参数上反复试。openEuler 24.03 + Kunpeng 920
  实测 SP570 4x25GE 出现过该现象（kernel hinic 正常、DPDK hinic 初始化失败）。
- ARM/aarch64 交叉编译与构建：
  [cross_build_dpdk_for_arm64.rst](https://doc.dpdk.org/guides/linux_gsg/cross_build_dpdk_for_arm64.html)；
  官方平台指南（26.11）目录无 Kunpeng 专页——SoC 级调优参考供应商指南，
  DPDK 通用层（M1-M8）结论仍然适用 [官方边界，见 UPSTREAM.md]。

## mlx5 / NVIDIA ConnectX 特有

- 驱动文档：[mlx5.rst](https://doc.dpdk.org/guides/nics/mlx5.html)
  （devargs 名称与默认值已按 26.11.0-rc2 本地工作区文档核对；
  **随版本变化最频繁的模块之一**，引用前以目标环境版本文档为准）。

### 部署形态：bifurcated driver（与其他 PMD 根本不同）[官方]

- **不做 vfio-pci 绑定**：设备保留内核 `mlx5_core` 驱动，PMD 经 rdma-core/ibverbs
  接管数据面。M1/M2 里的 `dpdk-devbind -b vfio-pci` 流程对 mlx5 **不适用**。
- 内核网口（ethtool/iproute）与 DPDK 共存：bifurcation 可把指定流分给内核、其余进
  PMD。rdma-core 与内核 mlx5_core 版本须与 PMD 匹配（官方 prerequisites 核对）。
- [经验·实测] openEuler 24.03（Kunpeng 920，aarch64）构建期依赖：需要
  `rdma-core-devel`（devel 头文件合并在该包，**没有**独立的 libibverbs-devel/libmlx5
  包）。只装运行库（rdma-core/libibverbs/librdmacm）时 meson 会**静默禁用** mlx5
  PMD——构建无任何报错，只是产物里没有 PMD 库，极易漏查。核对方法：
  `ls build/drivers/librte_net_mlx5.a` 或 `strings dpdk-testpmd | grep rte_net_mlx5`。
  实测通过的组合：rdma-core 50.0-44 + libmlx5 1.24.50.0 + libibverbs 1.14.50.0 +
  gcc 12.3.1，DPDK 26.11.0-rc2 的 net/regex/crypto 三类 mlx5 驱动均编译成功并链接
  进 testpmd（设备级 probe 仍需真实 ConnectX 硬件）。
- 常规核对不变：PCIe LnkSta、`numa_node`（worker/网卡/mempool 三点一线，回 M4）。

### 收包侧（省 PCIe 带宽是主杠杆）[官方]

| 手段 | devargs | 说明 |
|------|---------|------|
| CQE 压缩 | `rxq_cqe_comp_en` | **默认已开**；压缩 CQE 省 PCIe；第 8 位=1 为增强布局（更优时延与软件利用率，需 DevX） |
| Multi-Packet RQ | `mprq_en=1`（默认关） | Striding RQ 单大 buffer 收多包，**小包吞吐收益的主要来源**；`rxqs_min_mprq` 设启用队列数阈值，stride 数/大小经 `mprq_log_stride_num/size` 调，`mprq_max_memcpy_len`（默认 128）控制非内联拷贝上限 |
| Selective Rx | 见文档 selective_rx 小节 | 只收需要的报文部分，进一步省 PCIe |

### 发包侧（inline / eMPW：latency 与 PCIe vs CPU 的折衷）[官方]

| 手段 | devargs | 说明 |
|------|---------|------|
| 数据 inline | `txq_inline_max`（默认 290，驱动上限 930）/ `txq_inline_mpw` | 短包整包拷入 WQE：显著改善 PCIe 利用与时延，**费 CPU cycles**——PCIe 反压场景是收益，CPU 紧张场景可能负收益 [官方明示] |
| inline 启用阈值 | `txqs_min_inline`（默认 BlueField=16，其他=8） | TX 队列数超过阈值才自动启用 inline；设 0 强制启用 |
| 最小 inline | `txq_inline_min` | 官方建议**省略该参数用默认**；ConnectX-4 需 ≥18（L2+VLAN），Q-in-Q 场景 22；与 eMPW 互斥。**设不一致的值可能直接导致发不出包** [官方警告] |
| 旧参数 | `txq_inline`/`txqs_max_vec`/`tx_vec_en`/`txq_max_inline_len`/`txq_mpw_hdr_dseg_en` | **已弃用并被忽略**（26.11 实测核对）——网上旧教程常见，勿再调 [官方] |

- vector 收发路径自动选择（特性兼容时走 SIMD），核对方法：EAL 启动日志看实际选中
  函数 + M8 的 `--force-max-simd-bitwidth` 双基线对比。

### flow steering（flow 密集场景的主杠杆）[官方]

- `dv_flow_en=1`：SWS（Direct Verbs/Rules 软件 steering），常规推荐。
- `dv_flow_en=2`：**HWS 硬件 steering**（WQE-based），大规模 flow 场景配合
  **异步 flow API（template API）**，官方明示为大规模 offload 最优路径
  （规则每秒百万级插入）。
- `dv_flow_en=0`：legacy Verbs 路径，仅兼容用途。
- 运维注意：**port stop 后该口所有 flow 自动失效**，应用必须重建，勿持旧引用 [官方]。

### offload / RSS / LRO [官方]

- RSS 类型覆盖广（内外层 L3/L4 等）+ rte_flow RSS action 精细分流。
- LRO 需 DevX + DV flow（`dv_flow_en>=1`），并与 `lro_min_mss_size` 等约束联动——
  开 LRO 前核对约束与业务正确性（大包重组影响包计数口径）。
- offload 与 vector 路径兼容性随版本变化：每个 offload 改动后回看 EAL 日志确认
  收发函数没有退回 scalar（呼应 M8）。

### 内存与运维 [官方+经验]

- mlx5 经 MR（memory region）向网卡注册内存：大页留足（M2/M4）+ 内存足迹稳定；
  运行期频繁新增内存区会触发重复注册开销 [机制/经验]。
- [经验] 固件与工具链：`mlxconfig`（MFT）查改卡配置、`mlxfwmanager` 升固件；
  VXLAN 等 tunnel 解析依赖 FLEX_PARSER profile 时先核对固件能力。
- [经验] 参数不生效先查目标环境版本文档（mlx5 是 devargs 演进最快的模块），
  勿照搬网上旧教程反复试。
