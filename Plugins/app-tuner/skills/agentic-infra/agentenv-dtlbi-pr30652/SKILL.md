---
name: agentenv-dtlbi-pr30652
description: AgentENV coding 负载 KVM TLB 切换开销优化（KVM 软件定向 stage-2 失效，非 DVMBM/cluster-tlbi 的硬件位图方案）：先用 perf 确认宿主机存在 __tlb_switch_to_host 热点，再按 openEuler PR #30652（KVM arm64 directed stage-2 TLB invalidation）构建 6.6.0-pr30652-dtlbi 内核并重启生效（有重启风险，须提示使用者），重启后二选一使能 kvm.direct_tlbi=1（cmdline）或 echo 1 > /sys/module/kvm/parameters/direct_tlbi（运行时）。触发词：__tlb_switch_to_host 热点、directed TLBI、kvm.direct_tlbi、6.6.0-pr30652-dtlbi、AgentENV coding 负载 TLBI 优化。
---

# AgentENV directed TLBI 优化（PR #30652）

## 目标与原理

将 KVM arm64 广播式 stage-2 TLB 失效改为定向失效：不再对整个
inner-shareable 域发 TLBI 广播，而是通过 `KVM_REQ_TLB_FLUSH` 只 kick
本 VM 的 vCPU，各 vCPU 在重入 guest 前用 NSH（pCPU 本地）指令失效
自己的 stage-2 上下文。AgentENV coding 负载的收益大小取决于宿主机
`__tlb_switch_to_host`（arch/arm64/kvm/hyp/vhe/tlb.c）热点占比，
因此本 skill 以热点确认为门禁，先测后改。

流程：**确认热点 → 换核（PR #30652 → 6.6.0-pr30652-dtlbi）→ 重启
（风险提示+授权）→ 二选一使能 directed TLBI**。

## 阶段 0：环境前提

1. 宿主机为 aarch64 且 KVM VHE 运行。可靠判据（不要用 cpuinfo Features
   猜 flags）：
   `grep -w __tlb_switch_to_host /proc/kallsyms`（VHE 专属符号，存在即证明
   VHE KVM 已运行）或 `dmesg | grep 'kvm \[1\]'`。
   本优化只改 Host 内核，与 Guest 内核无关。
2. 已部署 AgentENV 且 coding 负载可运行；perf 可用
   （`perf record -a` 需 root 或 perf_event_paranoid 足够低）。
3. 可获取 openEuler OLK-6.6 内核源码及 PR #30652 的 5 个补丁，
   有构建工具链（gcc、make、bc、flex、bison、pahole（dwarves 包）、dracut/grubby）。
4. 区分 DVMBM（`agentenv-cluster-tlbi`，硬件位图广播范围）与本 skill
   （KVM 软件定向失效）：机制不同，可共存，但对照测试时固定其一，
   不得把另一机制的开/关混入本轮结论。

## 阶段 1：确认 `__tlb_switch_to_host` 热点（门禁）

在 coding 负载稳态运行时全系统采样（口径参考 `sandbox-bench-flame`，
禁止 -C 绑核采样）：

```bash
perf record -a -g -F 99 -o perf-coding-base.data -- sleep 60
perf report -i perf-coding-base.data --stdio --sort symbol | \
  grep -w __tlb_switch_to_host
```

- 占比判定走门禁脚本（可回归，配 tier-A 用例
  `cases/dtlbi-gate-contract/`）：
  `python3 scripts/check_hotspot.py report-base.txt`（`--threshold` 默认 1.0）
  输出 JSON 的 `decision`：
  - `pass`（≥1%）：继续阶段 2。
  - `fail`（<1% 或无样本）：向使用者报告热点占比与预期收益有限，
    **由使用者决定**是否仍换核；不要替用户默认继续。
- 手工口径（脚本不可用时）：`grep -w __tlb_switch_to_host` 后累计
  Overhead 列，规则同上。
- 空闲机器采不到热点，采样窗必须覆盖负载忙期。

## 阶段 2：按 PR #30652 构建 `6.6.0-pr30652-dtlbi`

1. 取源码与补丁。补丁来源二选一：
   - 邮件列表（推荐，含完整 5 补丁系列）：
     `https://mailweb.openeuler.org/archives/list/kernel@openeuler.org/message/GAGXIF2Q4K5P6UQ7HDCOGQ2WE5DNKH6Z/`
   - 若已有设备做过本流程，直接复用其补丁序列（5 个 commit：
     76ecb77a814f / 4bf097cd09c2 / ed9f63af9b62 / 723c5744d2e3 / 88aec9de6412）。
2. 应用补丁：

```bash
git clone https://atomgit.com/openeuler/kernel.git -b OLK-6.6 && cd kernel
git checkout -b pr30652-dtlbi
git am /path/to/00*.patch    # PR 因与 OLK-6.6 冲突被关闭，am 失败时按 3-way 重放：git am -3
```

3. 配置（**关键**：`CONFIG_KVM_ARM_DIRECT_TLBI` 默认 n 时该补丁系列
   编译必然失败——头文件无条件引用仅此 CONFIG 下存在的 vcpu 字段，
   CI 已复现；必须显式 =y）：

```bash
make openeuler_defconfig
./scripts/config -e CONFIG_KVM_ARM_DIRECT_TLBI
echo '-pr30652-dtlbi' > localversion     # kernelrelease → 6.6.0-pr30652-dtlbi
make -j"$(nproc)" olddefconfig && make -j"$(nproc)"
```

4. 安装（保留旧内核与旧启动项，禁止覆盖）：

```bash
make kernelrelease                      # 应为 6.6.0-pr30652-dtlbi
sudo make modules_install
sudo cp arch/arm64/boot/Image /boot/vmlinuz-6.6.0-pr30652-dtlbi
sudo dracut --force /boot/initramfs-6.6.0-pr30652-dtlbi.img 6.6.0-pr30652-dtlbi
```

按当前发行版实际启动布局核对（openEuler 为 Image + grub2 + grubby；
若现有内核是 rpm 安装，手工安装测试内核即可，不制作 rpm）。
安装后 `ls /boot` 与 `grubby --default-kernel` 确认新旧条目并存。

## 阶段 3：重启（必须先做风险提示并取得授权）

**执行重启前必须向使用者明确提示，获得显式确认后才能执行：**

- 重启会中断宿主机上全部服务与沙箱（AgentENV 业务中断）。
- 新内核未经完整 CI 验证（该 PR 在 openEuler CI 中 aarch64 编译失败、
  kABI 待审，虽 =y 可编译通过，仍有启动失败风险）。
- 必须保留旧内核启动项可回退；建议具备 BMC/串口/物理控制台访问，
  以及非生产单测试节点。

若已决定用 cmdline 方式使能，可在重启前一并配好（见阶段 4 方式 A），
避免二次重启。

```bash
sudo grubby --set-default /boot/vmlinuz-6.6.0-pr30652-dtlbi
sudo reboot
# 重启后核验：
uname -r        # 必须是 6.6.0-pr30652-dtlbi，否则停止并回退旧内核
```

## 阶段 4：使能 directed TLBI（二选一）

方式 A —— cmdline（需配合一次重启；建议在阶段 3 重启前配好）：

```bash
sudo grubby --update-kernel=/boot/vmlinuz-6.6.0-pr30652-dtlbi --args="kvm.direct_tlbi=1"
```

方式 B —— 运行时 sysfs（立即生效，无需再重启）：

```bash
echo 1 | sudo tee /sys/module/kvm/parameters/direct_tlbi
```

核验生效：

```bash
cat /sys/module/kvm/parameters/direct_tlbi    # 应输出 1（或 Y）
```

注意事项：

- kvm 为内建时参数路径才是 `/sys/module/kvm/parameters/direct_tlbi`；
  先 `ls /sys/module/kvm/parameters/` 确认存在，不存在即内核/配置不对。
- 运行时切换存在已知竞态（开关切换瞬间可能静默丢一次失效）：
  **只在没有 VM 运行/负载停止时切换**，切完再起负载。
- 带直通设备（SMMU 共享 S2）的 VM 由补丁 5 自动排除、回退广播，
  属预期行为，不是缺陷。

## 已知问题与边界（来自 PR #30652 评审，如实告知使用者）

- PR 未合入 openEuler 主线（CI aarch64 失败 + 与 OLK-6.6 冲突关闭），
  属验证性质补丁，不建议生产环境。
- `CONFIG_KVM_ARM_DIRECT_TLBI=n` 编译必失败（必须 =y，阶段 2 已处理）。
- pending range 字节/页单位错位：大范围失效退化为整 VMID 本地失效，
  属过度失效，正确性不受损，精度下降。
- kABI 有结构体布局变更；新内核上如需加载厂商预编译模块需复验。
- 收益上限受 `__tlb_switch_to_host` 原始热点占比约束（阶段 1 数据）。
