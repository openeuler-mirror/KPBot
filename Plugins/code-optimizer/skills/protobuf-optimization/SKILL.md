---
name: protobuf-optimization
description: >
  C++ 服务中 protobuf 序列化库的性能优化 — 按"生产画像与路径分流 → 画像对齐微基 →
  上游 diff 与形态前提核对 → 单变量 A/B → 生产外推封顶 → SPE 硬件确证"六步流程，
  覆盖 parse（TcParser 调度层）与 map merge（MapMergeFrom）两大热点路径。适用于服务
  火焰图中 protobuf 帧占比偏高（反序列化、消息合并、arena 分配）的库层调优。
  场景入口类触发：protobuf 优化、序列化热点、反序列化优化、proto parse 优化、
  消息合并优化、MergeFrom 热点、arena 分配开销。
  热点画像类触发：TcParser、MiniParse、字段查找开销、repeated field 构造、
  MapMergeFrom、map merge、ClearTable。
  手段类触发：字段查找内联、repeated 构造削减、arena 增长内联、map merge 拷贝构造、
  表预扩容、utf8 校验快路径、protobuf 单变量 A/B。
---

# protobuf 序列化库性能优化

你是 protobuf 库层性能优化专家。你的任务是对以 protobuf 为序列化层的 C++ 服务执行标准化优化流程：**生产画像与路径分流 → 画像对齐微基 → 上游参照与形态核对 → 单变量 A/B → 生产外推封顶 → 硬件级确证**，产出带完整证据链的优化报告。流程知识从一次完整的 protobuf 优化实战提炼（基准 4.25.8，上游参照 37.0-rc1，鲲鹏 AArch64，GCC 12），核心信条：

1. **画像对齐是微基可信度的全部前提**：微基火焰图与服务火焰图的库函数类别构成差 <5pp、且生产上榜关键函数全部出现，双条件过了微基数字才允许引用——否则任何收益数字都只是玩具负载上的方向参考。
2. **上游优化自带形态前提**：新版（如 37.x）的优化普遍针对 arena/大消息/深树形态；生产负载常是 heap/小消息/浅树。跨版本移植第一问永远是"形态对齐吗"——形态不合时整体移植实测 -22%，只取语义收益的自设计补丁拿 +5.6%。
3. **外推有铁律**：微基百分比 × 生产占比 ≠ 生产收益；微基只回答"该改动在镜像负载下的方向与幅度"，生产收益必须以生产占比封顶换算。典型反例：一个微基 +4% 的优化，其函数在生产火焰图占比 0.01%，生产收益≈0。
4. **负结果是资产**：无收益或负收益的尝试连同数字固化，防止后续重复踩坑（本 skill 附完整负结果库）。
5. **语义验收用 DebugString 全量比对**：map 类消息的 entry 顺序未指定，不同构造路径合法输出本就不同——用 `SerializeAsString` 比对会误报，必须用 `DebugString`。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| `repo` | 是 | protobuf 源码树（生产所用的 fork/版本） |
| `service_flamegraph` | 建议 | 生产服务火焰图；缺省时先采集并解析 protobuf 帧家族 |
| `benchmark_cmd` | 建议 | 画像对齐微基命令；缺省时按下文方法自建 |
| `upstream_ref` | 建议 | 上游新版本源码（作优化参照，如 37.x vs 生产 4.25.x） |
| `constraints` | 建议 | 只改库层不改业务代码 / 部署口径（分服务 vs 统一融合库） |

## 执行步骤

### 步骤 1：生产画像与路径分流

解析生产火焰图（self 口径，同名递归帧去重后求和），确定 protobuf 帧家族占比与热点路径。两大典型路径：

| 路径 | 热点帧 | 负载形态特征 | 优化方向 |
|------|--------|-------------|---------|
| parse（反序列化） | `MiniParse`/`MiniParseLoop`（同名递归帧先 de-nest）、字段 entry 查找、repeated field 构造/增长、arena 增长与分配、UTF-8 校验（`IsStructurallyValid`） | 小消息密集、嵌套结构、packed 定长/varint 数组 | 调度层内联系列 |
| map merge（消息合并） | `MapMergeFrom`、hash 查找（FindHelper 系）、节点构造/析构、`ClearTable`、serialize 拷贝 | map 密集、heap 分配、浅树 | miss 路径拷贝构造 + 表预扩容 |
| destruction（析构清理） | `ClearTable`、`DestroyProtos`、节点 delete 链 | 常与 merge 同段 | 一般不优化（见负结果库的指令级证据位） |

分流判据：parse 帧自家族 self 合计 vs merge 帧合计（如 40/30 vs 生产 44/32 的配比即典型 parse 主导负载）。注意区分 parse 路径的自提升上限：同名递归帧 de-nest 后的 self 总量是任何 parse 优化的理论天花板，先算 ceiling 再评估投入——典型生产 parse 路径去重后 ≈3%，此时微基再大的收益对 E2E 的贡献也被封死。

### 步骤 2：画像对齐微基

从生产火焰图读出库的真实用法（消息形态、操作配比、分配模式 heap/arena），微基循环体照抄。设计三律：

1. **段比例粗调**：微基各操作段（parse/merge/destroy…）比例按生产类别配比粗调，±10pp 内即可——**段内函数集合对齐比比例精确更重要**（生产上榜的关键函数一个都不能缺，配比差几个百分点不影响结论方向）
2. **可采样性**：循环 ≥ 数秒（997Hz 采 ≥5k 样本）；计时段外的辅助代码（cache 污染循环、数据生成）会污染采样——画像采样用无污染版本
3. **验收双条件**（过了才允许引用微基数字）：微基自采火焰图 vs 生产火焰图的库函数类别构成差 <5pp + 生产上榜关键函数集合全部出现

### 步骤 3：上游参照与形态核对

diff 上游新版本，逐项核对"上游有、本地没有"的优化，每项填形态前提核对表：

| 核对项 | 上游隐含前提 | 生产实际形态 | 结论 |
|--------|-------------|-------------|------|
| 批量分配类优化（如三段批量 MapMergeFrom） | arena 连续 bump（n 次分配 O(1) 摊销） | heap（malloc 系痕迹） | ❌ 退化为 n 次 malloc + 双倍遍历 |
| destruction 循环预取 | destroy 是 miss-bound（深树大消息） | 浅树小消息、指令/分配器主导 | ❌ 预取无收益（先做 cache 污染实验验证 miss-bound 假设） |
| 数据预取 API（MaybePrefetch 系） | 对比本地版本是否已含同度实现 | — | ⚠️ 先查本地：版本间常是改名而非新增 |
| 逐元素拷贝构造 | 无形态前提（局部语义收益） | 任意 | ✅ 可移植 |

核对不过的放弃整体移植；可移植的也优先"只取语义收益的自设计"（保留 element-wise 单遍，避免上游方案的形态假设）——实战对照：整体移植 -22% vs 只取拷贝构造语义 +5.6%。逐项核对清单的展开版（含每项的实测数字与判断依据）见[形态前提核对清单](references/shape-premise-checklist.md)。

### 步骤 4：单变量 A/B（5 轮交替协议）

每个优化点独立成 patch，微基跑 5 轮 base/opt 交替（**绝不**先跑完所有 base 再跑 opt——共享机器慢漂移会伪装成差异）。绑核固定、warmup 不计时、轮间清理状态。协议框架见 `scripts/alternating_ab.sh`（base/opt 命令交替执行、逐轮比值与方向一致性自动判定）。健康度三级判定：轮内方差 ≈0.2% 内、逐轮比值稳定、5/5 轮同方向。任何 <5% 预估增量的优化必须实测后才允许进入实现评审——resize 链先验估计 25~35%、实测 0.46%（50 倍高估）是真实案例，完整案例见[负结果档案](references/negative-results.md)。

### 步骤 5：生产外推封顶

库内自提升 = E2E 提升 ÷ 库时间占比（口径必须声明，如 E2E +2.4% ÷ 库占比 50.8% = 自提升 +4.7%）。生产预期 = 微基路径提升 × 生产占比，**封顶**计算，禁止反向外推。融合部署决策矩阵（两个优化系列在同库共存时）：

| 部署口径 | 做法 | 实测参考 |
|---------|------|---------|
| 分服务部署 | parse 重负载服务用 parse 系列、merge 重负载用 merge 系列 | 各取全量收益（+7.4% / +2.4% 量级） |
| 统一融合库 | 两系列合入同一库全量部署 | 跑融合 A/B：典型零干扰，或编译级残差 ~0.5%（新路径代码布局扰动）——量化后取舍 |

### 步骤 6：SPE 硬件级确证

优化后负载在 ARM SPE 下采样，确认已无微架构级瓶颈。**阈值判定与字段命名统一采用 `arm-spe-analysis` skill 的 canonical 分级体系**（`l1_miss`/`llc_miss`/`branch_mispred`，SEVERE/MODERATE/MILD/OK 四级：l1_miss >30% SEVERE、>10% MODERATE、>5% MILD；llc_miss 同级；branch_mispred >20% SEVERE、>5% MODERATE、>1% MILD；tlb_miss 与 llc_miss 合并分类）——本 skill 不另立健康值口径，确证标准 = `arm-spe-analysis` 的 `spe-hotspot.sh`（`classify_bottleneck`）输出全 OK 或 ≤MILD。

```bash
perf record -e arm_spe/period=65536/ -- <bench>   # 默认间隔 20s 产 14GB 数据，必须设 period
bash scripts/spe_aggr.sh <perf-script 输出>       # 事件统计 + miss 按函数聚合
```

本场景实测参考（分级口径下均为 OK~MILD 区）：l1_miss 0~1.8%、llc_miss ≈0、branch_mispred 0.15~1.5%；miss/branch 集中在 hash 表查找（FindHelper/ClearTable）属预期结构成本。两个坑：**SPE 样本按操作量加权 ≠ 时间占比**（逐字节循环的函数样本虚高数十倍，时间分布用 cycles 采样交叉验证）；报出的陌生热符号 + 大偏移先反汇编验明正身（perf 把无 dynsym 的局部函数归位到最近前置导出符号，libc 内部函数常见错位）。

## 优化知识库

### parse 路径（调度层内联系列）

| 优化点 | 做法 | 参考收益 |
|--------|------|---------|
| 字段查找内联 | table 字段 entry 查找内联进 MiniParse 主循环 | parse 段 +7.4%（797→738ns 级，5 轮交替） |
| repeated 构造削减 | 逐元素构造/增长调用开销内联化 | 同系列叠加 |
| arena 增长内联 | fresh-field arena 增长路径 + 分配包装 | 同系列叠加 |
| repeated-message Add 快路径 | Add 系列快路径 | 同系列叠加 |
| UTF-8 校验 ASCII 快路径 | ASCII 直通内联 | 微基 +4%，**生产收益看占比**（见负结果库） |

### map merge 路径

| 优化点 | 做法 | 参考收益 |
|--------|------|---------|
| miss 路径拷贝构造 | `MapMergeFrom` miss 时 copy-construct 直接构造（跳过默认构造+赋值的 clear） | merge 段 +5.6%，E2E +2.4% |
| 表预扩容 | 合并循环前一次性 reserve | 增量 +0.46%（实测先验高估 50 倍的教训案例） |

## 负结果库（防重复踩坑）

| 尝试 | 实测 | 教训 |
|------|------|------|
| 37.x 三段批量 MapMergeFrom 整体移植 | merge -22% | 批量分配只对 arena 形态成立，heap 形态退化为 n 次 malloc+双倍遍历 |
| destruction 循环预取 | -1.2~1.4% | 非_miss-bound 的 destroy 预取无收益，先做 cache 污染实验再动手 |
| 超前 reserve（读边界的批量预留） | 零收益 | 输入分布不保证 chunked 形态时，预留成本无处回收 |
| SVE 化 varint 编码变体 | 零收益 | 指令层优化被调用频率/带宽吸收，先验收益高估 |
| utf8 校验快路径生产外推 | 微基 +4%，生产≈0 | 该函数生产占比 0.01%——外推铁律的标准反例 |

## 脚本用法

```bash
# 5 轮交替 A/B 协议（base/opt 交替执行，自动输出逐轮比值与方向一致性）
bash scripts/alternating_ab.sh -b "<base 命令>" -o "<opt 命令>" -r 5

# SPE 事件聚合（arm_spe 采样后：事件类型统计 + l1/llc/branch-miss 按函数聚合，过滤内核帧）
bash scripts/spe_aggr.sh <perf-script 输出文件>

# 产物级 patch 使能验证（新增函数名特征法：strings 扫 debug info，静态库自动抽成员）
bash scripts/verify_pb_patch.sh /path/to/业务二进制 [/path/to/protobuf源码根]
```

脚本依赖与边界说明见 `scripts/README.md`。

## 语义验收矩阵

| 项 | 规则 |
|----|------|
| map 类语义比对 | 用 `DebugString()` 全量比对，**绝不能用 `SerializeAsString()`**——map entry 顺序未指定，不同构造路径合法输出本就不同，会误报 |
| 覆盖路径 | hit 路径（key 已存在→赋值）与 miss 路径（新节点→构造）分开测；融合前后各跑一轮 |
| A/B checksum | 优化前后消息内容哈希一致；构造路径差异导致的 entry 顺序差异属合法，不算回归 |
| 生成代码兼容 | 不改 wire 格式与生成接口；只动 runtime 内部路径 |

## 常见误区

| 误区 | 真相 |
|------|------|
| "上游 37.x 的优化抄过来就好" | 上游优化普遍带 arena/大消息/深树前提，heap/小消息负载移植实测 -22%——形态核对永远第一问 |
| "微基快 4%，生产也快 4%" | 微基 % × 生产占比 ≠ 生产收益；该函数生产占比 0.01% 时生产收益≈0 |
| "destroy 段热就加预取" | 先用 64MB cache 污染实验验证是否 miss-bound——实测非 miss-bound 的 destroy 预取 -1.2~1.4% |
| "resize 肯定贵，先优化" | 短 key rehash 实测 ~0.2µs/merge，先验估计高估 50 倍——先测再投入 |
| "把 hash 查找换成更快结构" | 25.8 已是 Fibonacci hashing + 掩码寻址、查找全内联，每次 merge 一查是必要成本 |

## 部署验收清单

- [ ] 微基画像对齐双条件验收通过（类别构成差 <5pp + 关键函数集合齐）
- [ ] 5 轮交替 A/B 原始数据归档（轮内分布 + 逐轮比值）
- [ ] 语义矩阵全过（DebugString 比对、hit/miss 分路径）
- [ ] SPE 确证无新增微架构瓶颈
- [ ] 融合部署时融合 A/B 已跑（零干扰或残差已量化）
- [ ] 报数口径声明（parse 段 / E2E / 库内自提升三选一为代表值）

## 报数纪律

统一"提升 +X%"表述；每场景一个代表值并声明口径（parse 段提升 / E2E 提升 / 库内自提升=E2E÷库占比）；微基百分比与生产预期分开陈述；5 轮原始数据随附。
