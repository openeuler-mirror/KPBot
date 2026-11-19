---
name: agentenv-snapshot-fs
description: 评估 AgentENV 快照与批量落盘的文件系统写入路径性能。当快照创建慢、pwrite 吞吐低、work_dir 位于 ext4（非 openEuler 内核）或用户要求迁移 XFS 时使用；判断写入路径档位（buffer_head 慢路径 vs iomap 大 folio 快路径），给出 XFS 迁移、内核 backport 或 tmpfs 取舍与端到端验证方案。
---

# AgentENV 快照写入路径与文件系统档位

## 输入与共享约定

需要实际节点、发行版与运行内核、`work_dir` / `AENV_HOME_PATH` 实际所在文件系统、
kallsyms 符号证据、写入微基准与 folio 构成、AgentENV 快照用例与授权范围。
读取共享 `references/validation.md`；源码布局下为 `../../references/validation.md`。
共享优化流程见 `references/optimization-process.md`；源码布局下为
`../../references/optimization-process.md`。
补采使用共享 `scripts/collect_environment.sh`，源码布局为
`../../scripts/collect_environment.sh`。

## 诊断

1. 确认 `work_dir` / `AENV_HOME_PATH` 实际所在文件系统（`df -T <路径>`）。
   该路径决定写入性能档位——先核对路径再谈文件系统优化。
2. 检查 kallsyms：`ext4_iomap_buffered_write_ops` 存在表示 openEuler 类
   iomap 大 folio 缓冲写快路径；不存在表示经典 buffer_head 路径。
   Ubuntu/上游 ext4 该路径未合入、目标代码未编译进内核——是代码缺失，
   没有挂载参数或 journal 调优可以开启，不要给出这类操作建议。
3. 判据顺序以 folio 构成为首要证据（kpageflags COMPOUND 位）：全独立
   4K 页 = 慢路径；出现复合 16KB folio = 快路径。写入微基准
   （pwrite 50MB，@4KB 与 @16KB 对照）作旁证：经典 buffer_head 路径
   按页处理，16KB 写也会拆成同样数量的 4KB 页，两种粒度吞吐接近说明
   每页固定开销主导（慢路径特征）；@16KB 显著快于 @4KB 说明 folio
   粒度摊薄了每字节成本（iomap 大 folio 快路径特征）。据此，仅提高
   应用写入粒度不减少页数，在慢路径上几乎不带来收益。
4. 排除非因素后再归因写路径：同盘型号、页大小、ext4 块大小、journal 特性、
   挂载参数、内存带宽——这些对齐时差异才指向内核写路径实现。
5. 跨机器对比前对齐系统税（如 `CONFIG_RANDOMIZE_KSTACK_OFFSET` 的
   syscall 开销），残差不解释为文件系统能力差异。

tmpfs 也有逐 4KB 页分配开销，批量写可能慢于大 folio 文件系统；仅用于
可接受数据丢失的临时快照，不能默认等价于 XFS 方案。

## 实施

1. 首选 XFS（内核原生 iomap 架构，与 openEuler ext4 快路径同构）：
   准备裸设备或逻辑卷 → `mkfs.xfs` → 挂载 → `/etc/fstab` 持久化 →
   `config/default.toml` 的 `work_dir` 指向 XFS 路径。保留原 work_dir
   配置与数据可回退。
2. 备选按代价排序：tmpfs（临时快照、无持久化）；把 openEuler 的 ext4
   iomap backport 系列（约 54 个提交，fs/ext4 + fs/iomap）合入自编内核
   ——版本匹配、模块签名、initrd 风险高，无强需求不选。
3. 变更只动 `work_dir` 指向（或文件系统），不改 AgentENV 版本、负载与
   并发口径，便于归因。

## 验证与回退

1. 迁移后复跑微基准：@16KB 预期进入快路径档位（已验证机器的参考区间约
   7~10ms/50MB，以当前机器基线为准），folio 构成应出现复合 16KB。
2. 端到端快照用例 A/B：同负载、同并发、同预热，采集迁移前后的
   snapshot_creation 系列指标，保留原始数据。参考区间：三台 Ubuntu 24.04
   实测快照创建提速约 30%~43%，当前项目收益以实测为准，未实测时
   gain_pct 为 null，不引用参考区间代替实测。
3. 功能回归：快照/resume 循环、文件系统持久化与 fstab 重启生效。
4. 出现功能失败或性能回退时，将 `work_dir` 指回原路径并复验。

按共享结果契约输出当前数据支持的结论；写入路径档位判定与端到端收益
分别提供证据。
