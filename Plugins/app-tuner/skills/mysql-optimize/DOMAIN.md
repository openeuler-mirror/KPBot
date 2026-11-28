# MySQL 优化域（app-tuner 侧）

MySQL 8.0（8.0.25 / 8.0.28，兼容 Percona Server 5.7/8.0 常见路径）从**架构理解 → 编译部署 →
正确性/性能验证 → 热点归因 → 配置/源码优化**的完整调优链路。域内成员各自独立可调用，
组合起来覆盖一次完整的 MySQL 性能优化闭环。

>
> `mysql-optimize/` 本身是源码树中的领域分组（以本 DOMAIN.md 登记），不是独立 skill；
> `install.sh` 递归进入本分组后，把**已合入**的成员平铺安装到 `<tool>/skills/<skill-name>/`，
> 由 Claude Code / OpenCode 一层发现。十个成员在本源码树中即以平铺目录存放，
> 安装后目录名与源码树一致。

## 成员

| Skill | 层级 | 职责 | 典型入口 |
| --- | --- | --- | --- |
| `mysql-architecture` | 一级 | Server 层/InnoDB 分层、连接与线程模型、查询/写入全链路、MVCC/Read View、redo/binlog group commit、Buffer Pool/LRU/刷脏、锁与等待、排序与 collation 路径、配置地图、源码路径（被动只读知识库） | “一条 point select 怎么走到 Buffer Pool”“redo 和 binlog 谁先落盘”“为什么 ORDER BY 慢” |
| `mysql-build-deployment` | 一级 | 依赖与 Boost/OpenSSL/libaio 作用、CMake 基线/`-O3` 构建、初始化与基线配置、物理机 1P 部署、验收、备份/升级/回滚；含只读 `preflight_check.sh` | “从源码编译”“初始化 datadir”“换二进制升级” |
| `mysql-container-deployment` | 二级（部署子技能） | supermin 镜像、docker 导入、cpuset/mems/memory、host 网络、host IRQ 与容器核映射、容器内绑核与线程数、写场景 IO 等待 | “8U32G 容器怎么搭”“容器里 MySQL 绑哪些核” |
| `mysql-vm-deployment` | 二级（部署子技能） | libvirt/qemu、网桥、qcow2、hugepages/NUMA、vcpupin、virtio、磁盘/网卡直通、host/guest 中断布局、验收 | “虚机怎么配大页和绑核”“virtio 性能波动” |
| `mysql-test-verification` | 一级 | 构建验证、MTR（全量/单例/suite、失败定位、用例编写）、功能冒烟、sysbench point/RO/RW/WO、指标口径、A/B 与噪声判定；含只读 `smoke_test.sh` | “优化有没有效果”“功能有没有坏”“MTR 怎么跑” |
| `mysql-hotspot-analysis` | 一级 | perf/火焰图/off-CPU/Topdown/PMU、`performance_schema`、`SHOW ENGINE INNODB STATUS`、慢日志；Point/RO/RW/WO 热点迁移、锁/刷脏/排序/group commit 归因；含 dry-run `collect_profile.sh` | “慢在哪”“为什么 CPU 高但 TPS 低”“ut_delay 高怎么办” |
| `mysql-performance-optimization` | 一级 | 决策矩阵、配置/编译/源码三类杠杆、场景化参数、负结果与回滚；含 `verify_build_opts.sh` | “怎么调”“改哪里”“收益多少”“负收益怎么回滚” |
| `mysql-os-optimization` | 二级（优化子技能） | BIOS 相邻项、IRQ/网卡、THP/大页、ulimit/SELinux、内核启动参数、sysctl、磁盘/文件系统；dry-run 默认、执行需审批 | “中断绑核”“THP 开还是关”“sysctl 怎么给” |
| `mysql-app-config-optimization` | 二级（优化子技能） | my.cnf 参数原理与场景矩阵（Point/RO vs RW/WO、8U32G 20t/64t、2C8G/8C16G/32C256G 容器）、在线 vs 重启生效、渲染与回滚 | “buffer pool 设多少”“log file 设多大”“AHI 开关” |
| `mysql-source-optimization` | 二级（优化子技能） | 源码优化点挖掘与已交付 patch：fast-tuple、Read View version、space-skipping+NEON collation、binlog ordered、`rec_get_offsets`、binlog preallocate；机制/适用/风险/验证/回滚 | “源码还能改哪”“这个 patch 收益和风险” |

## 域内路由

```
只想理解系统                  → mysql-architecture
要从零装/初始化/升级          → mysql-build-deployment
  容器形态                    → mysql-container-deployment
  虚机形态                    → mysql-vm-deployment
要证明对/证明快               → mysql-test-verification
要知道慢在哪/为什么慢         → mysql-hotspot-analysis
要把证据变成改动              → mysql-performance-optimization
  OS/BIOS/中断/大页/sysctl    → mysql-os-optimization
  my.cnf/参数/连接/缓存       → mysql-app-config-optimization
  补丁/容器/拷贝/比较路径     → mysql-source-optimization
```

一次典型的优化闭环：

```
architecture（理解链路与线程）
  → build-deployment（起环境、初始化、验收）
  → test-verification（MTR/冒烟 + 基线 + 正确性门禁）
  → hotspot-analysis（进程/线程/函数/机制归因）
  → performance-optimization（单变量：OS / 配置 / 编译 / 源码）
  → test-verification（同口径 A/B + 正确性门禁）
  → 保留或回滚
```

## 与插件内其他 skill 的边界

| 问题 | 去哪里 |
| --- | --- |
| 通用 OS governor/THP/大页/irqbalance/ulimit | 先用本域 `mysql-os-optimization`（MySQL 场景画像），通用原理回到 `kpbot-app-tuner` 的 `os-optimization` |
| BIOS SMT/C-State/NUMA/内存频率/预取 | `bios-optimization`；本域只列 MySQL 实测相关项并交回 BIOS skill 执行 |
| 网卡队列/RSS/RPS/中断通用调优 | `network-optimization`；本域给 MySQL 绑定布局与验证口径 |
| CPU 绑核/NUMA 布局/线程亲和 | `cpu-affinity-optimization`；本域给 mysqld/worker/log 线程的具体识别方法 |
| 编译选项/LTO/PGO/工具链通用评估 | `compiler-optimization`；本域给 MySQL `-O3 -march=...` 基线与已证伪项 |
| 通用 C/C++ 微架构/向量化开发 | `kpbot-code-optimizer`；本域只承接 MySQL 专属补丁与验证 |
| 数据库通用状态判断（AHI 决策等） | `kpbot-app-tuner` 的 `database-workload-analysis`；本域提供更深的 MySQL 实现与参数落地 |
| MySQL 专属架构/部署/验证/热点/配置/源码 | 本域对应成员 |

## 准入判据

新增成员须满足：

- 结论落在 **MySQL 应用自身**（架构、部署、验证、热点、配置/源码优化），不是通用
OS/网络/编译理论，也不是泛数据库方法论。
- 有明确的输入/输出契约、停止条件、失败处理与回滚方式。
- 涉及变更的内容必须区分只读采集、dry-run、低风险在线变更、需重启变更、需重编译变更。
- 有可复现的验证入口（evals 或 skill 内 `cases/`）。
- 不写入个人环境绝对路径、私网 IP、凭据；示例统一用 `${MYSQL_HOME}`、`${DATA_ROOT}`、
`${CLIENT_HOST}` 等变量占位。
- 所有性能数字标注环境、数据集身份、轮次与场景；不同文档/不同数据集的数字不可直接横比。
- 参数与补丁结论必须说明“来自哪份实测/经验”，并给出适用条件与反例。

## 安装与校验

- 领域分组只存在于源码树；`install.sh` 将一级成员与二级 `skills/` 子包平铺安装。
- 完整性校验（结构、链接、锚点、evals、敏感信息）：
`bash mysql-architecture/cases/skill-integrity/run.sh`
- 所有成员均提供 `evals/evals.json`；变更后应重跑完整性用例与仓库测试。
- 领域内脚本默认只读或 dry-run；任何写操作必须显式 `--execute`，高风险变更还要
`--approved-change-id`、备份与回滚说明。

## 版本与实测基线

- 主要覆盖 MySQL 8.0.25 / 8.0.28；容器/虚机流程验证过 Percona Server 5.7.44 与 MySQL 8.0.25。
- 平台：Kunpeng 920/920B/920G（aarch64，96C/192T、80C/160T×2 等）、openEuler 22.03/24.03，
以及 x86_64 压测客户端。其他机型只改变量纲与 NUMA 布局，不改变方法。
- 文档中的线程数、参数值、百分比均为特定实测，使用前必须按目标环境复测。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/mysql-optimize/**`
