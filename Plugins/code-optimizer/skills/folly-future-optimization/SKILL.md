---
name: folly-future-optimization
description: >
  folly future/promise 异步链路性能优化 — 按"生产画像 → 上游 diff → 单变量 A/B →
  归属判决 → 分配器对照"五步流程优化 C++ 服务中的 folly 依赖热点，覆盖 Core/SharedPromise
  构造分配、类型擦除开销、线程池调度三类热点家族。适用于服务火焰图中 folly future 链路
  （Function/Core/Promise/Future/then 回调）占比偏高的调优。
  场景入口类触发：folly 优化、folly 性能、future/promise 优化、SharedPromise 热点、
  then 回调开销、异步链路优化、folly 火焰图分析。
  热点画像类触发：FunctionTraits、Core 构造、doCallback、CoreCallbackState、
  KeepAliveOrDeferred、future 分配开销。
  优化手段类触发：CoreCallbackState 瘦身、平凡构造内联、tcmalloc/jemalloc 切换、
  ThrottledLifoSem 移植、folly 编译选项。
---

# folly future/promise 链路性能优化

你是 folly 异步链路性能优化专家。你的任务是对以 folly 为异步基础设施的 C++ 服务执行标准化优化流程：**生产画像 → 上游基线 → 候选设计与单变量 A/B → 归属判决 → 分配器与编译层收尾**，产出带完整证据链的优化报告。流程知识从一次完整的 folly 优化实战提炼（基准版本 v2022.11.07.00，鲲鹏 AArch64，GCC 12），核心信条：

1. **类型擦除是架构性成本，不是 bug**：`FunctionTraits` 系热点（典型生产占比 3~4%）是 std::function 式类型擦除的实现税，其帧不应作为优化目标——收益通常在**调用它构造的节点对象与 malloc 面**，见归属判决一节。
2. **收益必须归属判决**：benchmark 变快 ≠ 你以为的那个机制变快。每个采纳的优化点都要用"只含该机制的镜像微基"确认收益归属，机制假设未实测的归属一律存疑。
3. **官方代码也带形态前提**：上游 folly 自己的优化（如 ThrottledLifoSem 唤醒策略）移植到不匹配的负载形态实测 -17%——移植前必须核对负载形态，与库版本新旧无关。
4. **分配器必须三方实测**：glibc/jemalloc/tcmalloc 在同一负载上的差异可达 -69.5% ~ +8%，任何"大家都说 jemalloc 快"的先验都不可信，LD_PRELOAD 三方对照是必做实验。
5. **三层报数口径分离**：benchmark 用例整体提升（如 +8.2%）≠ 热点函数自身提升（≈15~20%）≠ 生产 E2E 预期（≈热点占比×路径改善封顶）——三层混用是汇报事故的第一来源。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| `repo` | 是 | folly 源码树（可构建版本） |
| `service_binary` | 建议 | 使用 folly 的目标服务二进制（用于生产画像与产物验证） |
| `benchmark_cmd` | 建议 | 优先用 folly 自带 `futures_benchmark`；缺省时自建镜像微基（见步骤 3） |
| `flamegraph` | 建议 | 生产服务火焰图（SVG/perf script 均可）；缺省时先采集 |
| `constraints` | 建议 | 是否允许改 folly 源码 / 编译选项 / LD_PRELOAD |

## 执行步骤

### 步骤 1：生产热点画像（self 口径）

对生产服务采火焰图并做帧级 self 解析（`self = 帧样本 − 直接子帧样本和`），产出 folly 热点清单。folly future 链路的热点通常集中在三个家族：

| 家族 | 典型帧 | 生产占比参考 | 性质 |
|------|--------|-------------|------|
| 类型擦除 | `FunctionTraits`、`Function::callBig`、`Function::operator()` | 3~4% | 架构性成本，一般不优化 |
| 节点构造与回调 | `CoreBase::CoreBase`、`Core` 构造/析构、`SharedPromise` get/set、`CoreCallbackState`、`KeepAliveOrDeferred`、malloc/free 面；回调侧 `doCallback`、`Future::then` 桥接、`coro` 帧 | 1.5~3% | **主战场**（瘦身+内联+分配器；回调帧与构造耦合，归属判决后定） |
| 执行调度 | `CPUThreadPoolExecutor`、`ThreadPoolExecutor::run`、`keepAlive`、信号量/条件变量等待 | 0.5~2% | 配置/行为级，按需 |

帧清单速查：`Function` 相关帧看归属（谁构造它），`Core`/`Promise` 构造帧看对象尺寸与分配次数，`doCallback` 看回调链深度。三家族共占 <3% 时优先排查业务侧而非 folly。

画像纪律：帧树 `Σ(self)` 必须 = 100% 自检；同名递归帧先去重再求和（naive 求和可虚高 3 倍以上）；`callBig` 下再挂 `callBig` 是 operator() 内联的双层包装多次计税，不是异常。给每个热点编号（M1、M2…），后续所有数字与归属均挂编号。

### 步骤 2：上游基线与版本 diff

选定与生产最接近的开源版本作基线，diff 上游新版本定位**官方已吸收的优化**（避免重复造轮子，也避免提交已过时知识的 PR）；同时从 diff 挖候选优化（上游有、本地版本没有的）。注意：官方优化仍需形态核对（信条 3）。

### 步骤 3：候选优化设计 + 单变量 A/B

对每个候选做**单变量** patch，用 benchmark 跑 **5 轮 AB/BA 交替**（不是先跑完所有 base 再跑所有 opt——共享机器的慢漂移会伪装成差异）。健康度三级判定：轮内方差 ≈0.2% 内、逐轮比值稳定无单调漂移、5/5 轮同方向。自带 `futures_benchmark` 时按下表选用例（每个用例对应不同链路形态，别只跑一个就下结论）：

| 用例 | 链路形态 | 验证什么 |
|------|---------|---------|
| `twoThens` | 两个 then 的 Core 全生命周期（构造→set→回调→析构） | Core 瘦身/内联的最敏感指标 |
| `OnThreadInline` | inline executor 上的 then 链 | 同步链路的构造+回调开销 |
| `complexBlob` | 大数据经 future 传递 | 拷贝/移动路径 |
| `spGetSetOne` | SharedPromise 单读单写 | SharedPromise + 分配器 |
| `promiseAndFuture` | 裸 Promise/Future（无 Shared） | 基线对照（不测 SharedPromise 机制） |

参考量级：twoThens 基线 ≈240ns/次（鲲鹏 GCC12 -O2）；5 轮交替后代码层优化典型收在 +8% 上下。

### 步骤 4：归属判决实验（本流程的关键环节）

对每个"变快了"的优化点，构建**只含该机制的镜像微基**并对比优化前后该机制的帧占比变化。镜像微基的构造纪律：

1. **运行时循环拼接**：then 链长度 N 必须是运行时变量（如从 argv 读入），**禁止模板编译期常量**——编译期展开会让编译器跨节点优化掩盖真实收益（实测可把 +8% 掩盖成 +0.6%）
2. **只挂目标机制**：微基里除了被验证的机制不留其他 folly 调用，排除交叉污染
3. **折叠聚合按每栈样本数加权**：栈条数 ≠ 样本数（一个折叠栈可能代表几十个样本），误用可把 1.9% 的帧算成 0.2%
4. **对照读法**：
   - 帧占比不变（如 1.72% → 1.74%）→ 收益**不在**该机制，在下游（构造/malloc 面）——修正归因，收益记账到真实机制
   - 帧占比下降 → 收益确实在该机制

完整判决案例（含镜像微基源码形态与加权坑的逐步复盘）见[归属判决案例档案](references/attribution-verdict.md)。**没有这一步，"A 快了所以 A 是对的"的归因错误会直接带偏下一步优化方向。**

### 步骤 5：分配器三方对照与编译层收尾

1. **分配器**：`LD_PRELOAD=` 同一负载下 glibc / jemalloc / tcmalloc 三方对照（协议脚本见 `scripts/allocator_ab.sh`，实测数据与部署前检查清单见[分配器三方对照档案](references/allocator-benchmark.md)）。实测参考：folly future 链路（小对象高频构造）tcmalloc 收益 +8%、glibc 持平、jemalloc **-69.5%**（页回收策略与小对象负载严重不合）。部署前先确认生产进程当前分配器（`cat /proc/<pid>/maps | grep -E 'jemalloc|tcmalloc'`）——已在用 jemalloc 的结论完全不同。
2. **编译选项**：`-O3` + 按目标 CPU 选 `-mcpu`（映射与 `compiler-flag-tuning` skill 的 canonical 保持一致：读 `cpu_part_id`，0xd01 → `-mcpu=tsv110`，0xd03/0xd06 → `-mcpu=native`（GCC 12 对鲲鹏后端尚未提供独立代号）；LLVM 侧工具链映射见 `llvm-mca-analysis` 的 `mca_analyze.py`（0xd03 → hip10c、0xd06 → hip12a），勿与 GCC 参数混用）。确认与构建系统的全局继承链一致——配置声明值 ≠ 产物生效值，用产物反推（`DW_AT_producer` 字段含实际编译参数指纹）——工具已拆分至 `binary-compile-forensics` skill（comment 版本/参数指纹/SIMD 兜底三链通用层）。

### 步骤 6：终极验证与产物级确认

1. **三层叠加终验**：代码层（瘦身后 benchmark）→ 编译层（叠加 -O3 -mcpu 后）→ 分配器层（叠加 tcmalloc 后），每层都全指标复跑（不能只看目标用例——优化叠加后其他用例可能被拖累，出现任何负值都要回到归属判决排查）。参考量级：twoThens 240→216ns（代码层 +8.2%）→ 叠加编译与分配器后 +19.4%、深层链路（OnThreadInline）+27%。
2. **产物指纹验证**：编译进二进制的改动应可从产物观测——用 `scripts/verify_folly_patch.sh` 检查（`nm -C <bin> | grep 'CoreBase::CoreBase'` 应为 0：内联生效则 out-of-line 符号消失；`strings` 可见新增标记/flag 名）。构建系统的配置声明值 ≠ 产物生效值，始终以产物为准。
3. **回归全过**：优化库跑 folly 自带测试套件（`build/folly/test/future_test` 等），语义无回归才允许交付。

## 脚本用法

```bash
# 产物级 patch 使能验证（nm/strings/debug-info 三探针，含动态/静态库定位）
bash scripts/verify_folly_patch.sh /path/to/服务二进制

# 实际编译参数反推（.comment 版本 + DW_AT_producer 指纹 + SIMD 兜底三链）→ 已拆分至 binary-compile-forensics skill：
binary-compile-forensics skill 的 inspect_build_flags.sh（见该 skill 的 SKILL.md 获取安装后入口）

# 分配器三方对照（glibc/jemalloc/tcmalloc LD_PRELOAD 交替，输出每轮 ns/次与比值）
bash scripts/allocator_ab.sh -c "<benchmark 命令>" -r 5
```

脚本依赖与边界说明见 `scripts/README.md`。

## 常见误区

| 误区 | 真相 |
|------|------|
| "类型擦除帧占比 4%，优化它能拿大头" | 架构性成本，实测把优化火力集中于此多轮无产出；收益在构造/malloc 面（归属判决实证） |
| "新版 folly 修了这些，升级就行" | 官方优化带负载形态前提（ThrottledLifoSem 移植实测 -17%），升级 ≠ 收益，形态核对先行 |
| "benchmark 快了 8%，生产也快 8%" | 生产预期 ≈ folly 热点占比 × 路径改善，封顶换算；热点占比 <2% 的服务 E2E 预期通常 <0.5% |
| "jemalloc 是服务器标配" | 本场景实测 -69.5%；分配器结论不可跨负载形态搬运 |
| "重构顺手清理 Core 内部结构" | 结构清理类 patch 实测 -3~8%，非瓶颈路径上只增加扰动 |

## 优化知识库

| 优化点 | 做法 | 参考收益 |
|--------|------|---------|
| CoreCallbackState 瘦身 | 状态对象改存裸 `Core*` 指针而非完整 `Promise`（Promise 2 指针宽 → Core 1 指针宽，lambda 捕获更小、更易落进 Function 小尺寸优化） | twoThens +8.2%，深层链路 +19~27% |
| 平凡构造内联 | `CoreBase`/`KeepAliveOrDeferred` 等空函数体 out-of-line 符号强制 inline（产物级可验证：符号消失） | 与瘦身叠加生效 |
| 编译选项 | `-O3 -mcpu=<微架构>`（继承链确认 + 产物反推） | 独立小增益 |
| 分配器 | tcmalloc（future 小对象构造负载） | benchmark +8%（malloc 面收益） |

## 负结果库（防重复踩坑）

| 尝试 | 实测 | 教训 |
|------|------|------|
| ThrottledLifoSem（上游官方优化）移植 | -17% | 官方优化带负载形态前提，非匹配负载必翻车 |
| LTO / ThinLTO | -9.8% | 全库 LTO 的重排收益抵不过内联爆炸的 icache 损失 |
| Core 内部整理系列（cleanup/常量初始化等 3 项） | -3~8% | 结构清理在非瓶颈路径上只增加代码扰动 |
| small_vector 替换 Core 内部容器 | +1%（不显著） | 容量极小的容器换 small_vector 无意义 |
| jemalloc | -69.5% | 分配器选择没有普适答案，必须三方实测 |

## 部署验收清单

- [ ] 生产进程分配器现状确认（`cat /proc/<pid>/maps | grep -E 'jemalloc|tcmalloc'`）——与实验结论的前提一致
- [ ] 编译选项产物级验证（`readelf --debug-dump=info` 的 `DW_AT_producer` 含预期 -O3/-mcpu）
- [ ] 改动指纹在产物中可见（符号消失/新增标记）
- [ ] 优化库 vs 基线库的回归测试全过
- [ ] 5 轮交替 A/B 原始数据归档（含轮内分布，不只报均值）
- [ ] 汇报数字声明口径层级（benchmark / 热点自身 / 生产 E2E）

## 报数纪律

统一"提升 +X%"表述（禁混用耗时"-X%"引发符号歧义）；每场景一个代表值；三层口径（benchmark 用例 / 热点自身 / 生产 E2E）声明后再报；生产预期以热点占比封顶换算，禁止反向外推。
