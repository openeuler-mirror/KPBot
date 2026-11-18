# 存储域（占位 · 当前 0 成员）

块设备 / 文件系统 / NVMe / 分布式存储与对象存储这一层的性能优化技能归属地。

## 当前状态

本仓库目前**没有**存储专项 skill。存储经验只以三种形态零散存在，均**不搬**：

| 现存位置 | 内容 | 为什么不搬 |
|---|---|---|
| `sra-dag-tuning`（方法总表 4.8 / 1.20 / 6.2 与 3.x 节） | 磁盘 IO 调度器与文件系统、在线特征存储、分级存储、`libaio`/`io_uring` 批量提交示例 | 它们是搜推 DAG 全栈方法里的**一层**，脱离业务链路没有独立使能流程 |
| `use-analysis` | USE 分析法里的磁盘 IO 使用率/饱和度/错误率检查（`iostat`/`vmstat`） | 它是跨资源的**排查器**，不承担某一层的优化方法 |
| `Plugins/app-tuner/skills/kpbot-app-tuner/subskills/io-memory-network-bottleneck-analysis`、`hardware-upgrade-analysis` | 磁盘 IO 瓶颈分支、`fio`/IOPS 选型 | 是 `kpbot-app-tuner` 主框架的**子技能**，独立成 skill 会切断主框架的候选路由 |

压缩层（`openzl-compression-tuning`、`openzl-bench-protocol`）属于数据格式 codec 吞吐，**不属于本域**。

## 准入判据（新 skill 落本域的条件）

1. 结论落在**存储栈的某一层**：块层调度与队列（`nr_requests`、`mq-deadline`/`none`）、文件系统与挂载参数（ext4/xfs、`noatime`、`data=`）、
   NVMe 多队列与中断/IRQ 亲和、页缓存与回写（`dirty_ratio`、`dirty_expire`）、异步 IO 接口（`io_uring`/`libaio` 批量提交）、
   分布式存储客户端（Ceph RBD/CephFS、NFS、对象存储 SDK 并发）；
2. 有可复现的基线口径：`fio` 三档（4K 随机读 IOPS/时延、顺序带宽、混合读写 70/30）+ 设备型号/固件/文件系统/挂载参数；
   没有基线先立 issue 写方法论，不先落 skill；
3. 与邻域的边界：结论落在**业务代码**归 `code-optimizer` 现有 skill 或 `microservice/`；落在**业务配置**归 `app-tuner`；
   落在 **HBM→Host→SSD 分层**归 `aplusk/`（跨 NPU/CPU 的存储分层由该域裁定）；落在**向量索引落盘/预取**归 `vectordb/`。

## 候选成员方向（未立项，等素材）

- `storage-io-stack-tuning`：块层 → NVMe 多队列/中断亲和 → 挂载参数 → 页缓存/回写 → `io_uring` 批量提交，配 `fio` 基线口径。
- `feature-store-tiering`：从 `sra-dag-tuning` 的 1.20/6.2 抽出特征库内存/SSD 分层的独立使能流程。

两条都需要真实压测记录（设备型号、`fio` 原始输出、业务侧收益）才能写成 skill；只有方法论骨架的不建。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/storage/**`
- 首个成员落地时，把「当前状态」一节替换为成员路由表。
