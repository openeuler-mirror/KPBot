# M9 场景化调优

各场景特有约束。引用均指向 DPDK 官方文档（版本溯源见 UPSTREAM.md，
目标工作区 dpdk 源码仓的 `doc/guides/` 为版本精确来源）。

## vhost / virtio 虚拟化

- vhost-user 依赖大页共享内存：主进程必须以可共享大页运行（--in-memory 模式不支持
  secondary/vhost 共享，需挂载 hugetlbfs + --huge-dir）[官方 sys_reqs 推论；细节查
  目标环境版 `prog_guide/vhost_lib.rst`]。
- vhost 特性开关（zero-copy、client mode、packed ring、in-order）影响性能与内存语义，
  **逐项以目标环境版 vhost_lib 与 virtio 驱动文档为准，不要凭记忆开**。
- virtio 收发路径（vector/scalar、indirect desc）有启用条件，查驱动文档 [官方指引]。
- [经验] VM 场景先排除宿主机调度干扰：vhost worker 线程绑核与 guest vCPU pin 同等对待。

## OVS-DPDK

- DPDK 侧约束同 M1-M5（大页、核隔离、NUMA 对齐）；OVS 侧参数（pmd-cpu-mask、
  dpdk-lcore-mask、socket-mem）归 OVS 文档管。
- 排查时先分清层：OVS PMD 线程调度状态（pmd stats show）→ 队列→vhost 口 → VM。
- [经验] OVS 升级/换内核后先复测基线，不要沿用旧数字。

## 多进程 primary/secondary

- 大页配置必须一致；不同业务组用 `--file-prefix` 隔离 [官方 EAL]。
- secondary 不能分配/释放共享内存：dev_configure/start/queue_setup 等仅在 primary
  合法 [官方 testpmd 多进程文档实测核对]。
- testpmd 官方队列分配规则：`start = proc_id * nb_q / num_procs`，
  队列数须为进程数倍数，否则冗余队列 + RSS 场景丢包 [官方，实测核对]。

## Kunpeng 920 / aarch64（鲲鹏主场景）

- 交叉编译：[cross_build_dpdk_for_arm64.rst](https://doc.dpdk.org/guides/linux_gsg/cross_build_dpdk_for_arm64.html)。
- PMD 文档：[hns3.rst](https://doc.dpdk.org/guides/nics/hns3.html)
  （能力/约束版本精确）。
- 常见环境形态 [经验，以现场为准]：openEuler 24.03（kernel ≥6.6）、Kunpeng 920
  5250/5258 系列（aarch64，SMT2）、Hi1822 4x25GE / HNS RDMA 网络控制器。
- 官方 platform 指南目录（26.11）不含 Kunpeng 专页——SoC 级调优参考供应商指南，
  DPDK 通用层（M1-M8）结论仍然适用 [官方边界]。
- [经验] ARM 侧注意：页大小（4K/64K 内核）、NEON/SVE vector 路径可用性、
  `RTE_MAX_LCORE` 编译值与实际核数匹配（AMD 官方文档同项要求 [官方类比]）。

## 容器

- DPDK 支持容器化构建（`linux_gsg` 官方）；容器内跑 DPDK 还需大页挂载、
  /dev 挂载与 cpuset 透传，逐项核对 [经验；官方仅覆盖构建]。

## 硬件 offload 加速场景

- checksum/TSO/tunnel offload、flow director/rte_flow、regex/compress/crypto：
  属功能取舍而非性能免费项——每项 offload 重新测收益（M5/M8 方法）。
- 查 [rte_flow 文档](https://doc.dpdk.org/guides/howto/rte_flow.html) 与对应硬件文档。

## 场景速查表

| 场景 | 首查 | 常见根因 |
|------|------|----------|
| vhost VM 网络慢 | vhost 线程绑核 + guest 队列对 | 跨 NUMA、packed ring 未启用 |
| OVS 转发率低 | OVS PMD 统计 | pmd-cpu-mask 与隔离核不一致 |
| 多进程吞吐异常 | 队列分配规则 | 队列数不是进程数倍数 |
| 鲲鹏 920 板 PPS 低 | hns3 文档约束 | 描述符上限/汇编路径未启用 |
