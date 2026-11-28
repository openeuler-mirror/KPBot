---
name: mysql-vm-deployment
description: >
  MySQL 虚机形态部署子技能（KVM/libvirt，Kunpeng/aarch64 与 x86_64）：宿主机依赖与网桥、
  qcow2 镜像、libvirt XML（hugepages、numatune、vcpupin、emulatorpin/iothreadpin）、
  virtio 磁盘/网络、磁盘/网卡/VF 直通（SMMU 与 iommu.passthrough）、宿主机与 guest IRQ
  布局、guest 内 MySQL 绑核、验收与回滚。用于 8U32G 等 VM POC 与性能验收；不负责编译
  （→ mysql-build-deployment）、容器（→ mysql-container-deployment）、参数深度调优
  （→ mysql-app-config-optimization）。触发词：MySQL 虚机、KVM、libvirt、virsh、
  hugepages、vcpupin、numatune、virtio、网卡直通、磁盘直通、SR-IOV、虚拟机性能。
license: MulanPSL-2.0
compatibility: Linux aarch64/x86_64；openEuler 20.03/22.03 实测；libvirt/qemu-kvm/edk2；host 需 root
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: relational_database
  version: "1.0.0"
---

# MySQL 虚机形态部署

## 简介

在 KVM/libvirt 宿主机上创建并调优 MySQL 虚机：网桥、qcow2、libvirt XML、大页/NUMA、
vCPU 绑核、virtio 设备与直通，guest 内运行 MySQL 并固定绑核，使 8U32G 等 VM POC 的
性能可复现、可与容器/物理机对照。

**实测基线**：Kunpeng 920 系列 + openEuler 20.03/22.03，8U32G 虚机，MySQL 8.0.25
（Percona 5.7.44 流程类似）；远端压测。

## 何时触发

- 需要搭建 8U32G（或其他规格）MySQL 虚机并做功能/性能验收。
- 需要配置大页、NUMA 绑定、vCPU 亲和、virtio 或设备直通。
- 虚机性能波动大、CPU 打不满、网络/磁盘异常，需要核对宿主机与 guest 布局。
- 需要可复现的 XML/启动参数与验收记录。

**不适用**：源码编译/初始化 → `mysql-build-deployment`；容器 → `mysql-container-deployment`；
my.cnf 深度调参 → `mysql-app-config-optimization`；压测口径 → `mysql-test-verification`。

## 变量约定

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${VM_NAME}` | 虚机名 | `mysql-8u32g` |
| `${VM_XML}` | libvirt XML | `/etc/libvirt/qemu/${VM_NAME}.xml` |
| `${VM_IMAGE}` | qcow2 镜像 | `/data/vm/${VM_NAME}.qcow2` |
| `${VM_CPUS}` | vCPU 数 | 8 |
| `${VM_MEM_GB}` | 内存 | 32 |
| `${VCPU_PINS}` | vCPU→host 核 | 80-87 |
| `${VM_NODE}` | host NUMA 节点（大页分配与 domain 级 numatune 的目标） | 1 |
| `${PAGE_KB}` | XML `<page size>` 的单页大小（KiB；1048576=1 GiB，默认页通常 2048） | 1048576 |
| `${HUGEPAGE_COUNT}` | 按 `${VM_MEM_GB}` / `${PAGE_KB}` 计算的所需大页数 | 32 |
| `${BRIDGE}` | 网桥 | `br0` |
| `${HOST_ETH}` | 桥接物理网卡 | `eth0` |
| `${MYSQL_HOME}` | guest 内安装前缀 | `/opt/mysql-8.0.25` |
| `${DATA_ROOT}` | guest 内数据目录 | `/data/mysql` |
| `${MYSQL_CONF}` | guest 内 my.cnf | `/etc/my.cnf` |
| `${CLIENT_HOST}` | 压测客户端 | `<客户端地址>` |

## 工作流总览

```
宿主依赖/网桥 → qcow2 镜像 → libvirt XML（大页/NUMA/vcpupin/virtio）
  → virsh define/start → guest 网络/大页确认 → guest 内启动 MySQL
  → 宿主 IRQ 与 guest 绑核 → 验收 → 压测/记录 → 回滚
```

原则：

- 虚机 CPU/内存必须落在同一 host NUMA；大页按 node 分配。
- 宿主 IRQ 绑到与虚机同 NUMA 的空闲核；guest 网络中断绑到 guest 0 号核。
- virtio 性能波动大于直通；需要稳定极值时评估直通/SR-IOV。

## 宿主准备

```
yum install -y libvirt libvirt-client qemu-kvm virt-install edk2* \
  libaio-devel libcap-devel libiscsi-devel gtk3-devel vte291-devel
systemctl start libvirtd

# 网桥（nmcli）
nmcli con add type bridge con-name "${BRIDGE}" ifname "${BRIDGE}" autoconnect yes
nmcli con add type bridge-slave ifname "${HOST_ETH}" master "${BRIDGE}"
nmcli con down "${HOST_ETH}" && nmcli con up "${BRIDGE}"
```

- 桥接会短暂中断网络；远程操作需 BMC/串口或维护窗口。
- 禁用 docker0 等冲突网桥（如宿主同时跑容器）。
- 镜像可直接下载发行版 qcow2，或由现有虚机克隆。

## libvirt XML 要点

```
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='GiB'>${VM_MEM_GB}</memory>
  <memoryBacking>
    <hugepages>
      <page size='1048576' unit='KiB'/>
    </hugepages>
  </memoryBacking>
  <vcpu>${VM_CPUS}</vcpu>
  <iothreads>1</iothreads>
  <cputune>
    <vcpupin vcpu='0' cpuset='80'/>
    <vcpupin vcpu='1' cpuset='81'/>
    <emulatorpin cpuset='88-89'/>
    <iothreadpin iothread='1' cpuset='88-89'/>
  </cputune>
  <numatune>
    <memory mode='strict' nodeset='${VM_NODE}'/>
  </numatune>
  <cpu mode='host-passthrough'>
    <topology sockets='2' cores='4' threads='1'/>
  </cpu>
  <os>
    <type arch='aarch64' machine='virt'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw</loader>
    <nvram template='/usr/share/edk2/aarch64/vars-template-pflash.raw'>/var/lib/libvirt/qemu/nvram/${VM_NAME}.fd</nvram>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' iothread='1'/>
      <source file='${VM_IMAGE}'/>
      <target dev='vda' bus='virtio'/>
      <boot order='1'/>
    </disk>
    <interface type='bridge'>
      <source bridge='${BRIDGE}'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
```

- `hugepages` 大小按 host 支持（1G/2M）；页数按 `<page size>` 与 `${VM_MEM_GB}` 计算，在 `${VM_NODE}` 上预留，启动前核对 `free_hugepages`（见 `references/vm-deploy.md` §4）。
- `<page nodeset>` 指向 guest NUMA cell，不是 host 节点：单 cell guest 不写 nodeset，宿主节点由 domain 级 `<numatune><memory nodeset='${VM_NODE}'/>` 指定；`<memnode cellid>` 只有先声明 `<cpu><numa><cell>` 才能使用。
- `<iothreads>1</iothreads>` 必须先声明，才能被 `<iothreadpin iothread='1'>` 与磁盘 `<driver iothread='1'/>` 引用；否则 domain 无法定义/启动。
- `vcpupin` 必须与 `cpuset` 一一对应；`emulatorpin`/`iothreadpin` 同 NUMA，且必须用 vCPU 与宿主 IRQ 核之外的空闲核（示例 88-89 假设与 80-87 同 NUMA 且空闲；实际用 `lstopo`/`numactl -H` 确认，不要照抄成 `80-87` 与 vCPU 抢核，见 `references/vm-deploy.md` §3 常见错误表）。
- `host-passthrough` 让 guest 看到 host CPU 特性（含 LSE/CRC），便于性能对照。
- x86_64 用对应 `qemu-kvm`/OVMF 路径，`arch='x86_64'`。

## 启动与 guest 配置

```
virsh define "${VM_XML}"
virsh start "${VM_NAME}"
virsh console "${VM_NAME}"      # 默认账号以镜像发布说明为准，首次登录后修改

# guest 内确认
lscpu | grep -E 'NUMA|Model name'
# 大页由 host 侧 <memoryBacking><hugepages> 承载，对 guest 内核不可见：
# guest 的 HugePages_Total 只反映 guest 自身大页池，不能用于验收 host 大页；
# 宿主侧核对方法见下方「验收」。
ip addr
```

guest 网络（静态示例，按实际网段）：

```
BOOTPROTO=static
DEVICE=eth0
ONBOOT=yes
IPADDR=${VM_IP}
NETMASK=${VM_NETMASK}
GATEWAY=${VM_GATEWAY}
```

## IRQ 布局

- 宿主：关闭 irqbalance，把物理网卡 IRQ 绑到与虚机同 NUMA 的空闲核（不要占用 vCPU 核）。
- guest：网络 IO 中断（`virtio*-input/output`）通常绑到 guest 0 号核：

```
grep -E 'virtio|eth' /proc/interrupts
echo 0 > /proc/irq/<IRQ_NUM>/smp_affinity_list
```

- 8U32G：guest 0 号核承担网络中断，业务 1–7；核少时 0 号核也跑业务。
- 直通网卡时 guest 看到的是物理/VF 中断，需在 guest 内重新绑定。

## guest 内启动与绑核

```
numactl -C 0-7 "${MYSQL_HOME}/bin/mysqld" --defaults-file="${MYSQL_CONF}" &

# 建连后把 mysqld 线程绑到 guest 核 1-7（0 号核留给网络中断）。
# taskset 对 PID 只改主线程，必须逐 TID 设置；多实例时先按 socket 核对目标 PID，禁止拼接。
MYSQLD_PID="$(pidof mysqld)"
[ -n "${MYSQLD_PID}" ] || { echo "未找到 mysqld" >&2; exit 1; }
[ "$(printf '%s\n' "${MYSQLD_PID}" | wc -w)" -eq 1 ] || { echo "多个 mysqld 实例，需先核对 PID" >&2; exit 1; }
for tid in /proc/"${MYSQLD_PID}"/task/*; do
  taskset -cp 1-7 "${tid##*/}" >/dev/null
done
taskset -pc "${MYSQLD_PID}"   # 回读核验
```

- 绑核用系统自带 `taskset` 逐线程设置，不依赖外部脚本；`numactl -C 0-7` 只约束启动瞬间，新建连接线程需按上述循环覆盖。

线程数基线（8U32G 虚机，virtio）：

| 场景 | 基线 |
| --- | --- |
| Point | 100 |
| RO | 100 |
| RW | 300 |
| WO | 300 |

- 虚机核少但线程数高，是因为 virtio 路径的等待/软中断更多；以 CPU 打满且 IRQ 核不饱和为准。
- 读写/只写每轮结束等磁盘 IO 归零；virtio 下刷脏更慢，等待时间更长。

## 直通（可选）

| 类型 | 前提 | 配置要点 |
| --- | --- | --- |
| 磁盘直通 | host SMMU 开、盘未挂载、`iommu.passthrough=1` | `<disk type='block'>` 或 PCI hostdev；BDF 正确 |
| 物理网卡直通 | host SMMU 开、网卡未占用、`iommu.passthrough=1` | vfio-pci 绑定；删除桥接 interface |
| SR-IOV VF | 网卡支持 VF、host 内核 VFIO、guest 驱动 | 创建 VF、vfio-pci 绑定、hostdev 传 VF |

直通提升 IO/网络性能，但牺牲迁移/共享能力；生产需评估运维复杂度。

## 验收

| 检查 | 命令 | 通过标准 |
| --- | --- | --- |
| 虚机 | `virsh list` | running |
| vCPU | `virsh vcpupin ${VM_NAME}` | 与规划一致 |
| 大页 | 宿主 `cat /sys/devices/system/node/node${VM_NODE}/hugepages/hugepages-${PAGE_KB}kB/free_hugepages`（启动前后各取一次） | 启动前后差值 ≥ `${HUGEPAGE_COUNT}`（或 `virsh dommemstat` 确认 guest 实际获得大页内存）；不要用 guest `HugePages_Total`，它只反映 guest 自身大页池 |
| 网络 | `ping ${CLIENT_HOST}` / `ip addr` | 通 |
| MySQL | `mysql --socket=... -e 'SELECT 1'` | 返回 1 |
| 绑核 | 逐 TID `taskset -pc <tid>` | 业务线程在 1–7（0 号核留给 guest 网络中断）；未绑核时默认 affinity 恰好也是 0-7，故 0–7 不算通过；核少时 0 号核也跑业务的例外需单独注明 |
| 压测 | 客户端 sysbench | 3 轮均值，记录 TPS/P95/CPU |

## 常见问题

| 症状 | 处置 |
| --- | --- |
| 虚机起不来/找不到 edk2 | 核对 pflash/nvram 路径与 edk2 安装位置 |
| 大页不足 | 按 XML 页大小/内存算页数，在 `${VM_NODE}` 写 `hugepages-<size>kB/nr_hugepages`，核对 `free_hugepages` |
| 跨 NUMA 性能差 | 检查 vcpupin 与 numatune 是否同节点 |
| 网络不通 | 桥接 slave/bridge 状态、防火墙、IP/网关 |
| virtio 波动大 | 固定 vCPU/IRQ、隔离宿主负载、增加轮数；必要时直通 |
| 写场景越跑越慢 | 等 IO 归零；检查 host 盘与 virtio 缓存模式 |
| 直通后 guest 无设备 | 确认 SMMU/IOMMU、vfio-pci 绑定、BDF |

## 安全与边界

- 直通/桥接/大页都需要 root 与停机窗口；变更前备份 XML 与 guest 数据。
- 不在 XML/脚本写密码；guest 账号用镜像发布说明，首次登录后修改。
- 不修改 host BIOS；需要时交给 `bios-optimization`。
- 删除虚机前确认磁盘镜像已备份；`virsh undefine --nvram` 不会删镜像，但仍需确认。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/vm-deploy.md` | 依赖、网桥、镜像、XML 字段、直通、扩容、故障与回滚细节 |
| `scripts/preflight_vm.sh` | 虚机部署前只读检查（libvirt/大页/NUMA/网桥/镜像/端口） |
