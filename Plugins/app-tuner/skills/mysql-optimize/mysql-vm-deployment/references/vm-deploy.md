# 虚机部署细节

## 1. 依赖与镜像

```
yum install -y libvirt libvirt-client libvirt-devel virt-install qemu-kvm \
  edk2* nfs-utils libaio-devel libcap-devel libiscsi-devel \
  gtk3-devel vte291-devel glib2-devel zlib-devel pixman-devel
systemctl start libvirtd
systemctl enable libvirtd
```

- `edk2` 提供 aarch64 UEFI 固件；路径可能是 `/usr/share/edk2/aarch64/` 或
`/usr/share/edk2.git/`，以实际为准。
- qcow2 镜像来自发行版 `virtual_machine_img` 或内部模板；解压后放入宿主数据盘。
- 首次启动前确认镜像大小与磁盘余量；扩容见 §6。

## 2. 网桥

```
nmcli con add type bridge con-name br0 ifname br0 autoconnect yes
nmcli con add type bridge-slave ifname "${HOST_ETH}" master br0
# 编辑 ifcfg-br0：BOOTPROTO=static、IPADDR/NETMASK/GATEWAY、STP=no
nmcli con down "${HOST_ETH}" && nmcli con up br0
nmcli con show
ip addr show br0
```

- 桥接会短暂断网；远程操作需 BMC/串口。
- 若宿主有 docker0，先停 docker 或避免桥接冲突。
- 多网卡时先 down 其他网卡，必要时 `systemctl restart NetworkManager`。

## 3. XML 关键字段

| 字段 | 作用 | 常见错误 |
| --- | --- | --- |
| `<memoryBacking><hugepages>` | 1G/2M 大页 | host 未分配 hugepages，guest 启动失败；`nodeset` 是 guest cell 而非 host node |
| `<numatune><memory nodeset>` | 虚机内存绑 host NUMA 节点 | 误填 guest cell 编号，或与 vcpupin 不在同一节点导致抖动 |
| `<numatune><memnode>` | 多 cell guest 的 cell→host 节点映射 | 未声明 `<cpu><numa><cell>` 就写 `cellid`；单 cell guest 无需此项 |
| `<iothreads>` | 分配 IO 线程数（如 1） | 未声明却被 `iothreadpin`/磁盘 `iothread` 引用，domain 无法启动 |
| `<vcpupin>` | vCPU→host 核 | 核重叠/越界/与 IRQ 核冲突 |
| `<emulatorpin>` | QEMU 主线程核 | 未绑，QEMU 线程抢业务核 |
| `<iothreadpin>` | IO 线程核 | 与 vCPU 抢核；引用未声明的 iothread |
| `<cpu mode='host-passthrough'>` | 暴露 host 特性 | 用 custom 丢失 LSE/CRC，性能不可比 |
| `<disk bus='virtio'>` | 半虚拟化磁盘 | 用 IDE/SATA 性能差 |
| `<interface model='virtio'>` | 半虚拟化网络 | 用 e1000 性能差 |

## 4. 大页与 NUMA

页数按 XML 的 `<page size>`（KiB）与 `${VM_MEM_GB}` 计算，不写死：

```
VM_MEM_GB="${VM_MEM_GB:-32}"
VM_NODE="${VM_NODE:-1}"
PAGE_KB=1048576                          # 与 XML size='1048576' unit='KiB' 一致；默认大页通常 2048
HUGEPAGE_COUNT=$(( (VM_MEM_GB * 1024 * 1024 + PAGE_KB - 1) / PAGE_KB ))
echo "需要 ${HUGEPAGE_COUNT} 个 ${PAGE_KB} KiB 大页"   # 32GiB/1GiB=32；32GiB/2MiB=16384

# 在目标 host 节点（VM_NODE）按对应页大小预留
echo "${HUGEPAGE_COUNT}" > "/sys/devices/system/node/node${VM_NODE}/hugepages/hugepages-${PAGE_KB}kB/nr_hugepages"
grep Huge /proc/meminfo
cat "/sys/devices/system/node/node${VM_NODE}/hugepages/hugepages-${PAGE_KB}kB/free_hugepages"
```

- 启动前核对目标节点的 `free_hugepages` ≥ `HUGEPAGE_COUNT`，不足不要 `virsh start`；32 个默认大小（2 MiB）页只有 64 MiB，远不够 32 GiB guest。
- `/proc/sys/vm/nr_hugepages` 调整的是全局默认大小页池；1 GiB 页要写节点的 `hugepages-1048576kB/nr_hugepages`，两者不能混用。
- `<page nodeset>` / `<memnode cellid>` 指 guest NUMA cell，不是 host 节点：单 cell guest 用无 nodeset 的大页规则 + domain 级 `<numatune><memory nodeset='${VM_NODE}'/>`；只有声明 `<cpu><numa><cell>` 的多 cell guest 才用 memnode 逐 cell 映射。
- 1G 页需要连续内存；碎片化时先回收/重启。
- 大页由 host 侧 `<memoryBacking><hugepages>` 提供，host 大页 backing 对 guest 内核不可见：guest `/proc/meminfo` 的 `HugePages_Total` 只反映 guest 自身的大页池，不能用它验收 host 大页是否生效。启动前后在 host 核对节点 `hugepages-${PAGE_KB}kB/free_hugepages` 差值 ≥ `HUGEPAGE_COUNT`，或用 `virsh dommemstat` 确认 guest 实际获得大页内存。若确实要在 guest 内用大页（MySQL `large_pages=1`），须另在 guest OS 分配大页并让 buffer pool 与大页匹配；本工作流不含该步骤。
- THP 与标准大页是两套机制；虚机场景优先标准大页。

## 5. 直通

### 5.1 磁盘直通

```
<disk type='block' device='disk'>
  <driver name='qemu' type='raw'/>
  <source dev='/dev/nvme0n1'/>
  <target dev='vdb' bus='virtio'/>
</disk>
```

- host 盘不能挂载；SMMU 开；grub 加 `iommu.passthrough=1`。
- 直通盘性能接近物理，但不可快照/迁移。

### 5.2 物理网卡直通

```
modprobe vfio-pci
echo 0000:7d:00.1 > /sys/bus/pci/devices/0000:7d:00.1/driver/unbind
echo vfio-pci > /sys/bus/pci/devices/0000:7d:00.1/driver_override
echo 0000:7d:00.1 > /sys/bus/pci/drivers_probe
```

XML 用 `<hostdev mode='subsystem' type='pci' managed='yes'>`，删除桥接 interface。
恢复 host 网卡：unbind vfio-pci，bind 原驱动（如 hns3）。

### 5.3 SR-IOV VF

```
echo 2 > /sys/devices/pci0000:7c/0000:7c:00.0/0000:7d:00.3/sriov_numvfs
echo vfio-pci > /sys/bus/pci/devices/0000:7d:03.5/driver_override
echo 0000:7d:03.5 > /sys/bus/pci/drivers_probe
```

前提：SMMU 开、host 内核 `CONFIG_ARM_SMMU_V3/VFIO_PCI`、guest 驱动支持 VF。
VF 数量受网卡规格限制，写入前确认最大值。

## 6. 磁盘扩容、新增与 swap

### 6.1 扩容已有虚机磁盘（承载 MySQL 数据，禁止格式化）

先关虚机并备份镜像，再扩镜像；guest 内只扩分区并**原地**扩文件系统，不得 `mkfs`/`mkswap`：

```
virsh shutdown "${VM_NAME}"
qemu-img resize "${VM_IMAGE}" +500G
virsh start "${VM_NAME}"
# guest 内先确认设备/文件系统/挂载点，再扩分区，最后原地扩文件系统
lsblk -f
findmnt -no SOURCE,FSTYPE /data/mysql
growpart /dev/vda 1              # 扩分区；也可 parted /dev/vda resizepart 1 100%
resize2fs /dev/vda1              # ext4；xfs 用 xfs_growfs /data/mysql
```

- 对存有 MySQL 数据的分区执行 `mkfs`/`mkswap` 会重建文件系统并丢失全部数据，严禁。
- 先用 `lsblk -f`/`blkid`/`findmnt` 确认目标设备、文件系统与挂载点，确认已识别新容量后再扩文件系统。
- qcow2 只能增大不能缩小；扩容前必须备份。

### 6.2 新增空盘（允许格式化）

```
qemu-img create -f qcow2 /data/vm/disk2.qcow2 600G
# XML 加 <disk>，target dev='vdb'，BDF 不冲突；启动后确认目标设备确实是空盘
lsblk -f /dev/vdb                # 无 FSTYPE、无挂载，且非 MySQL 数据盘
findmnt /dev/vdb || echo '未挂载，可安全分区格式化'
parted /dev/vdb mklabel gpt mkpart primary 0% 100%
mkfs.ext4 /dev/vdb1              # 只对已确认的空盘执行
```

- 格式化前必须用 `lsblk`/`blkid`/`findmnt` 核对设备名、文件系统与挂载点：只对确认未挂载、无数据的空盘执行 `mkfs`。
- 设备名可能随 `<disk>` 加入顺序变化，误认会抹掉在用数据盘；操作前备份并二次确认设备映射。

### 6.3 swap（可选）

- swap 文件：`dd`/`fallocate` 创建、`mkswap`、`swapon`；MySQL 场景尽量不用 swap。只在确认是独立空文件/空盘后才执行 `mkswap`。

## 7. 性能注意事项

- virtio 的 IO/网络路径有额外软中断与调度；波动大于直通，必须多轮取均值。
- host 侧其他负载（编译、容器、迁移）会污染结果；测试期间隔离。
- vCPU 超分会导致 steal time；`top` 看 `st`，必要时减少 vCPU 或独占核。
- guest 内 `nproc` 是 vCPU 数；绑核列表不要超过。
- 读写/只写后等 IO 归零再下一轮；virtio 刷脏更慢。

## 8. 回滚与清理

```
virsh shutdown "${VM_NAME}"
virsh undefine "${VM_NAME}" --nvram   # 不删除磁盘镜像
# 恢复 host 网桥/IRQ/大页/直通绑定
```

- 删除镜像前确认已备份；`undefine` 后 XML 可从备份恢复。
- 直通设备恢复原驱动，避免 host 失联。
- 大页/IRQ 改动记录原值，测试结束恢复。
