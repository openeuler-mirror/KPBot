# 缓冲区与锁优化

> 机制基础见 姊妹 skill pgsql-architecture 的 buffer-manager-and-locks.md（按域路由定位）；
> 本页只谈处置。实测数字口径：ARM aarch64、48 核（超线程 96T）、单 NUMA、
> HNSW 检索 + pg_prewarm 预热、数据集 80GB（pgvector 场景轮）。

## 1. NUM_BUFFER_PARTITIONS 扩容（编译期补丁）

### 1.1 机制与数学（为什么 128→512）

- BufMappingLock 分区数（`src/include/storage/lwlock.h`，编译期常量，上游默认
  128）：两个 backend 的页面哈希落同分区才争用**共享**分区锁。
- 生日碰撞估算（N 个并发持有者、P 个分区，至少一对同分区的概率）：
  48 核同时活跃时，128 分区碰撞概率 ≈99.997%（几乎必然）、512 分区 ≈89.5%；
  预期同分区配对数 C(48,2)/分区：128 分区 ≈8.8 对、512 分区 ≈2.2 对。
  **注意这只是哈希碰撞估算**：两个查询落同分区、但都是**共享**锁请求时可同时
  持有，配对数 ≠ 实际竞争/等待数——"竞争对数下降 75%"不是等待下降 75%。
  收益验证必须走等待事件数据（LWLock/BufferMapping 占比）与排他请求的实际
  等待，不能只凭碰撞数学宣布有效。
- 关键认知：这是**锁等待（off-CPU）优化**——阻塞等待时间 CPU 空闲、不进
  on-CPU 火焰图、不产生 cache miss；火焰图与 cache miss 无变化不代表无效
  （忙自旋帧除外，见 hotspot-analysis 的睡眠/自旋区分）。

### 1.2 实施与回退

```bash
# 补丁流程（build-deployment 同款纪律）
cd "${PG_SRC}"
git status --short                                   # 干净树（已跟踪改动）；有本地改动先处理
# 补丁原件与审计记录必须在源码树外（PATCH_DIR 默认 ${PG_INSTALL}/patches，不在 ${PG_SRC} 内）
test -f "${PATCH_DIR}/num_buffer_partitions.patch" \
  || { echo "补丁原件缺失：${PATCH_DIR}/num_buffer_partitions.patch，禁止继续" >&2; exit 2; }
git apply --check "${PATCH_DIR}/num_buffer_partitions.patch" || exit 2
git apply "${PATCH_DIR}/num_buffer_partitions.patch"
# 审计记录写源码树外，避免被清理误删；不用 tee——管道会吞 git diff 退出码
git diff --stat > "${PATCH_DIR}/applied_$(date +%Y%m%d_%H%M%S).log"
# 编译期宏：从**干净构建产物**整树重编——只改 lwlock.h 后直接 make 可能复用旧对象，
# 实验版仍用原分区数（未启用 --enable-depend 时 make 不感知头文件依赖）
# 只清构建产物（make clean），**禁止 git clean -xfd**：后者会删除未跟踪的补丁原件、
# 审计记录与本地文件，使下方回退路径失效（破坏性清理前先显式确认）
make clean && make -j"$(nproc)" && make install
# 安装后重启目标实例换用对应产物，再采 A/B 数据（产物身份核验见
# references/compiler-flags-and-verification.md 的 verify_build_opts.sh）
# 回退（同样干净重编，撤回头文件后直接 make 可能仍安装补丁版对象、回滚「看似成功」）：
#   回退前校验补丁原件存在，缺失即 exit，禁止无补丁硬回退；
#   若已安装补丁版二进制且补丁原件不可恢复，先从 ${BIN_BACKUP_DIR} 恢复二进制
#   test -f "${PATCH_DIR}/num_buffer_partitions.patch" \
#     || { echo "补丁原件缺失，无法 -R 回退" >&2; exit 2; }
#   git apply -R "${PATCH_DIR}/num_buffer_partitions.patch" \
#     && make clean && make -j"$(nproc)" && make install && <重启实例>
```

- **禁止 `git clean -xfd` 清整棵源码树**：`-x` 连被 .gitignore 忽略的未跟踪文件一并删除，
  会删掉补丁原件与刚写的 applied_*.log，使 `git apply -R` 因文件不存在而失败、本地回滚
  能力丧失；要换干净构建目录只清构建产物（`make clean` / 删 obj 目录）。补丁与审计记录
  必须置于源码树外（默认 `${PG_INSTALL}/patches`）或已提交进 git；如确需清理，先用
  `git status --short --ignored` 列出将删除项并显式确认。

### 1.3 验证协议（收益如何显形）

| 通道 | 期望变化 |
|------|---------|
| 等待事件采样 | LWLock/BufferMapping 占比下降（**直接证据**） |
| offcputime | BufTableLookup 一带的累计离核时长下降 |
| 自研扩展 cas_hotspots | 相关热点重试计数下降 |
| 吞吐 | A/B ≥3 轮 TTS/TPS（同口径） |
| on-CPU 火焰图 / cache miss | **可能无变化**（不构成否决证据） |

## 2. shared_buffers 的元数据代价（不是越大越好）

| 指标 | 256G | 96G | 缩小 |
|------|------|-----|------|
| NBuffers（8KB 页） | 33,554,432 | 12,582,912 | 2.67× |
| 哈希表条目数 | 33,554,560 | 12,583,424 | 2.67× |
| BufferDescriptors | ~2.0 GB | ~768 MB | 2.67× |
| 哈希表总内存 | ~2.3 GB | ~860 MB | 2.67× |

- 机制：随机访问（HNSW 遍历类）下 PinBuffer 要随机访问 BufferDescriptors 数组
  中的不同项——**元数据工作集**随池线性放大，跨核缓存行乒乓上升；池大于
  热数据集后命中率收益归零、只剩元数据代价。
- 适用条件：数据集全热（预热后工作集 < 池），缩池不产生实际读 I/O——
  缩池前先用 pg_buffercache 确认命中率与热集大小；有冷数据的环境不适用本条。
- 实测口径：80GB 数据集，256G→96G 池收益为正（同 HNSW 轮）。

## 3. 候选优先级与反例

| 候选 | 适用 | 反例/风险 |
|------|------|-----------|
| 分区扩容 | 高并发随机读 + BufferMapping 等待可测 | 无等待证据时不动；重编成本与二进制漂移 |
| 缩池 | 数据全热 + 大池元数据代价 | 有冷数据（命中率会塌）；规划器 effective_cache_size 同步调 |
| PinBuffer CAS 竞争缓解 | cas_hotspots 显示同页重试聚集 | 属源码级深挖（LWLOCK_STATS 诊断构建），先观测再动 |

负结果纪律：任何候选判噪声/劣化都要记录数字与口径，防止下轮重复试错。
