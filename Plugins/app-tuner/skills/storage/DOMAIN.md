# 存储域（app-tuner 侧）

存储**引擎与服务层**的性能诊断、调优与构建部署支撑：LSM 引擎的读写路径、compaction、写入停顿与尾延迟，以及分布式存储（Ceph）的参数调优、源码编译、安装部署与扩容等工程化环节。

与 `code-optimizer` 侧的同名域分工：那边负责**系统存储栈**（块层调度、文件系统挂载、NVMe 多队列与 IRQ 亲和、
页缓存回写、io_uring/libaio、分布式存储客户端），本侧负责**引擎自身机制**（LSM/compaction/block cache）。
两侧 `DOMAIN.md` 互相指路。

## 成员

| Skill | 职责 |
|---|---|
| [`rocksdb-perf-diagnostics`](rocksdb-perf-diagnostics/SKILL.md) | RocksDB 性能诊断与优化：读 LOG / properties / Statistics / perf 上下文，定位慢读慢写、p99 尾延迟、写停顿（write stall）、compaction 抖动与 backlog，给出 ColumnFamilyOptions / DBOptions、block cache、bloom filter 等参数动作 |
| [`rocksdb-deploy-baseline`](rocksdb-deploy-baseline/SKILL.md) | RocksDB 基线部署：安装依赖、构建 RocksDB jar 与 YCSB distribution，产出 `deploy.env` |
| [`rocksdb-test-baseline`](rocksdb-test-baseline/SKILL.md) | RocksDB 100W 数据量基线测试：load、warmup、workload a-f、报告和数据备份 |
| [`rocksdb-workflow-baseline`](rocksdb-workflow-baseline/SKILL.md) | 基线测试总工作流：串联 deploy 和 test，支持只部署、只测试和完整流程 |
| [`ceph-parameter-tuning`](ceph-parameter-tuning/SKILL.md) | Ceph 存储集群参数调优：OS 系统层、ceph.conf 全量参数、PG 分布与 balancer、OSD NUMA 绑核，含备份/持久化/逐节点/重启窗口/回退变更流程 |
| [`ceph-build`](ceph-build/SKILL.md) | Ceph 14.2.8 源码编译：三路径依赖环境、isa-l 升级与 22.03 补丁、rpmbuild 编译、编译产物本地源安装与验证、故障排除三则 |
| [`ceph-install`](ceph-install/SKILL.md) | Ceph 16.2.7 repo 安装（openEuler 22.03 本地源/在线源）与 ceph -v 版本验证 |
| [`ceph-ansible-deployment`](ceph-ansible-deployment/SKILL.md) | ceph-ansible 自动化部署：all.yml/hosts 清单、MON/MGR/OSD/RGW 编排与 ansible-playbook 验收 |
| [`ceph-block-storage-deployment`](ceph-block-storage-deployment/SKILL.md) | RBD 块存储集群部署：MON/MGR/OSD、pool/capability 配置与 rbd 映射验收 |
| [`ceph-object-storage-deployment`](ceph-object-storage-deployment/SKILL.md) | RGW 对象存储部署（ceph-deploy / cephadm 双路径）：rgw 前端部署、radosgw-admin 用户创建与网关服务验证 |
| [`ceph-file-storage-deployment`](ceph-file-storage-deployment/SKILL.md) | CephFS 文件存储部署：MDS 部署、文件系统创建与 mount 验收 |
| [`ceph-block-expansion`](ceph-block-expansion/SKILL.md) | 块存储扩容：鲲鹏节点加入既有 x86 集群（Monitor/OSD 扩容与验证） |
| [`ceph-file-expansion`](ceph-file-expansion/SKILL.md) | 文件存储扩容：鲲鹏节点加入既有 x86 集群（MDS/CephFS 扩容与验证） |
| [`ceph-object-expansion`](ceph-object-expansion/SKILL.md) | 对象存储扩容：鲲鹏节点加入既有 x86 集群（RGW 实例扩容与验证） |

## 域内路由

- 问题落在引擎参数与读写路径（stall、compaction、cache 命中）→ 本侧 `rocksdb-perf-diagnostics`
- 需要基线构建、部署产物或 `deploy.env` → `rocksdb-deploy-baseline`
- 需要执行 100W 基线测试、报告或数据备份 → `rocksdb-test-baseline`
- 需要完整基线流程或只部署/只测试调度 → `rocksdb-workflow-baseline`
- Ceph 集群参数/PG/绑核/OS 层调优 → `ceph-parameter-tuning`
- Ceph 14.2.8 源码编译 → `ceph-build`
- Ceph 16.2.7 repo 安装 → `ceph-install`
- Ceph 集群 ceph-ansible 自动化部署 → `ceph-ansible-deployment`
- Ceph RBD 块存储部署 → `ceph-block-storage-deployment`
- Ceph RGW 对象存储部署 → `ceph-object-storage-deployment`
- Ceph CephFS 文件存储部署 → `ceph-file-storage-deployment`
- Ceph 块存储扩容 → `ceph-block-expansion`
- Ceph 文件存储扩容 → `ceph-file-expansion`
- Ceph 对象存储扩容 → `ceph-object-expansion`
- 问题落在系统栈（磁盘 IO 调度、挂载参数、页缓存、NVMe 队列、网络存储）→ `code-optimizer` 侧 `storage/`
- 问题落在业务代码的存储访问模式 → `code-optimizer` 现有 skill

## 准入判据

新增成员须满足：结论落在**存储引擎或存储服务自身机制**上（LSM/cache/compaction/副本与一致性），
且带可复现的基线口径（数据集规模、读写比例、SLA 目标）；落在系统栈或业务代码的，去对应域。

本组基线口径：100W 记录、YCSB 默认 value、固定 RocksDB 6.26.1 / YCSB commit `19e885f7`，报告包含分操作延迟和数据备份路径。

构建/部署类成员另须满足：构建动作可重建、产物可清理（附产物清单与清理路径），写入/部署类操作必须附回退方案与现场证据留存口径；纯只读体检类成员按各自目标系统定义验收口径——RocksDB 族沿用 100W 记录 / YCSB / RocksDB 6.26.1 基线；Ceph 参数体检以目标集群版本（ceph -v）、ceph -s 健康状态与 ceph daemon config show 配置快照为验收口径，不跨族套用基线。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/storage/**`
