---
name: cross-version-patch-porter
description: "跨软件版本移植补丁文件，通过理解语义意图而非机械应用差异来完成移植。当用户提到移植补丁、回移、前移、适配补丁到不同版本或分支，或补丁因上下文不匹配而无法应用时使用。也适用于：'将补丁应用到不同版本'、'在版本间迁移变更'、'将差异翻译到另一个代码库'、'补丁无法干净应用'、'适配差异到分支'、'跨版本回合'、'补丁回合'、'回合补丁'。在 git am/apply 会失败的跨版本场景中必不可少。不适用于同一版本的简单补丁应用或从零创建新补丁。"
---

# 跨版本补丁移植器

你正在跨软件版本移植补丁。机械式的 `git apply` 会失败，因为上下文行无法匹配。你的任务是理解*每个补丁的意图*，并在目标版本的代码中重新实现该意图。

## 核心原则

**读补丁理解意图，读目标了解结构，为目标版本的实际情况而编写。** 绝不要将补丁的 hunk 上下文复制粘贴到目标中——目标已有自己的上下文，那才是真正重要的。

## 输入

你需要从用户处获取：
- **补丁文件**：一个或多个 `.patch` 文件（unified diff 格式）
- **目标源码目录**：要移植进去的代码库
- **源版本标识**：例如 "rocksdb v6.26.1"
- **目标版本标识**：例如 "frocksdb 6.20.3"
- （可选）**构建命令**：如何编译目标。如果未提供，在目标目录中查找 `local_build.sh` 或 `Makefile`。

## 工作流程

**按顺序**处理补丁（0001、0002……）。后续补丁可能依赖前序补丁。对于每个补丁：

> **效率提示**：大型补丁（15+ 文件，50+ 变更点）不需要逐一规划每个 hunk。采用"编译驱动"策略：先做核心逻辑变更 → 编译 → 根据错误列表迭代修复。详见第 5 步。

### 第 1 步：分析（ANALYZE）—— 将补丁分解为原子意图

阅读整个补丁文件。对于每个 hunk（差异段），识别：
- **文件**：修改了哪个文件
- **意图**：为什么需要这个变更（而不仅仅是做了什么）
- **依赖**：这个 hunk 是否依赖本补丁或前序补丁中的其他 hunk
- **副作用**：还可能影响什么

按功能子系统对 hunk 分组。输出结构化摘要：

```
0001_autumn.patch:
  意图：添加 autumn_c 选项（层级乘数的标量因子）
  分组：
    A) 数据模型：advanced_options.h, cf_options.h/cc — 添加字段
    B) 计算：version_set.cc — 修改 CalculateBaseBytes
    C) C API：c.h, c.cc — 添加 get/set
    D) JNI：options.cc, Options.java — 添加绑定
    E) 工具：db_stress, db_bench — 添加标志
    F) 构建：build.sh — 修改 UT_LIST, run_ut
    G) 测试：options_settable_test.cc
    H) 校验：column_family.cc — 添加参数检查
```

**自动化辅助**：可以先用 `scripts/patch_inventory.py <target_dir> <patch...>` 生成补丁清单（触及文件、hunk 数、意图信号、补丁间依赖）作为起点，再人工补充意图分析。注意该脚本只做启发式分类，真正的意图仍需你阅读补丁确认。

### 第 2 步：侦察（RECON）—— 读取目标版本的文件

对于补丁触及的每个文件，读取**目标版本**的副本。搜索：
- 相同的函数名、类名、变量名
- 行号偏移（它们一定不同）
- 函数签名差异
- 类型系统差异（裸指针 vs 智能指针等）
- 补丁假定存在但目标中缺失的字段或方法
- 如果文件不存在，搜索重命名或等价的文件

**关键：建立 API 映射表**。同一个概念在不同版本可能使用不同的 API。在侦察阶段主动发现这些差异：

| 类别 | 示例差异 |
|------|----------|
| 函数名 | `CreateFromString()` vs `ParseSliceTransform()` |
| 方法名 | `AsString()` vs `Name()` vs 直接 `.compare()` |
| 类型名 | `ImmutableOptions` vs `ImmutableCFOptions` |
| 比较方式 | `!=` operator vs `.compare() != 0` vs `AsString() !=` |
| 指针风格 | `const T*` vs `shared_ptr<T>` vs `unique_ptr<T>` |

**方法**：对于补丁中涉及的每个关键函数调用，在目标版本中 `grep` 相同模块的符号。例如补丁调用 `SliceTransform::CreateFromString()`，则在目标中搜索 `SliceTransform::` 相关的创建方法。

记录你的发现——这就是你的适配映射。

### 第 3 步：评估（ASSESS）—— 对每个 hunk 分类

| 类别 | 含义 | 操作 |
|------|------|------|
| DIRECT_PORT | 目标代码结构相似 | 调整行号，直接应用 |
| ADAPTED_PORT | 意图相同，结构不同 | 以目标风格重新实现 |
| MISSING_INFRA | 目标缺少前置基础设施 | 先添加基础设施，再移植 |
| SKIP_NO_FILE | 文件不存在且无等价文件 | 记录原因，跳过 |
| SKIP_FORK_CONFLICT | 目标分支有冲突逻辑 | 决定：覆盖或合并，记录决策 |

**特殊规则：框架/构建特有文件**——如果补丁修改的是框架或构建系统特有的文件（例如上游的 `build.sh`），而目标代码库中不存在对应文件，一律归为 `SKIP_NO_FILE`。这类文件是框架特有的，在目标分支中通常没有等价物；改用目标自己的本地构建脚本（如 `local_build.sh`）作为编译参考。

详细评估示例见 `references/assessment-guide.md`。

### 第 4 步：移植（PORT）—— 应用适配后的修改

这是核心。规则：

1. **绝不粘贴 hunk 上下文**——始终根据目标文件的实际内容编写编辑。读取目标，然后编辑目标。
2. **匹配目标风格**——缩进、命名约定、include 顺序、花括号风格。
3. **合理放置新项**——新字段放在相关字段旁，新方法放在相关方法旁。
4. **适配类型**——如果补丁使用 `shared_ptr<T>` 但目标使用 `T*`，做相应适配。决策框架见 `references/type-migration.md`。
5. **顺序重要**——先移植基础变更（头文件、数据模型），再移植依赖变更（逻辑、API）。在补丁内，按依赖顺序移植分组。
6. **跨补丁依赖**——前序补丁可能创建后续补丁需要的基础设施。如果后续补丁依赖尚未移植的内容，先移植该依赖。

复杂补丁的分解策略见 `references/patch-anatomy.md`。

#### 处理大规模机械变更

当补丁在同一模式上修改 10+ 个文件时（如类型迁移、签名变更），逐文件 Edit 效率极低。策略：

1. **优先 `sed` 批量处理**。机械模式（如 `const SliceTransform* → const std::shared_ptr<const SliceTransform>&`）用 `sed -i 's/old/new/g' file1 file2 ...` 一次性完成。这是安全的——如果改错了，编译器会告诉你。

2. **区分"传参链"和"用户面 API"**。类型迁移只需改内部传参链上的函数签名，**不要改用户面 API**。例如 `TableReader::NewIterator()` 仍接受裸指针，但 `TableCache::NewIterator()` 改为接受 `shared_ptr`。

3. **`replace_all: true`**。当一个文件中出现多处相同模式时，用 Edit 工具的 `replace_all: true` 一次性替换。

4. **编译后迭代修复**。批量替换后必然有编译错误。按错误信息分两类处理：
   - "cannot convert shared_ptr to raw pointer" → 这处需要 `.get()`
   - "cannot convert raw pointer to shared_ptr" → 这处需要去掉 `.get()`
   再用 `sed` 批量处理每一类。

### 第 5 步：编译验证（COMPILE VERIFY）—— 编译并修复

移植每个补丁后，编译目标项目。使用目标目录中本地构建脚本（如 `local_build.sh`）或用户提供的构建命令。可用 `scripts/verify_compilation.sh <target_dir> [build_command]` 自动运行构建并提取错误摘要（完整日志写入 `build_error.log`）。

- **编译失败**：仔细阅读错误。常见原因：
  - 缺少 `#include` → 添加
  - 类型不匹配 → 适配到目标的类型系统
  - API 签名变更 → 读取目标的声明
  - 缺少字段/方法 → 可能需要先添加基础设施
- 在目标文件中修复错误，重新编译，重复直到干净。
- 当前补丁编译通过之前，**不要**继续下一个补丁。

**编译驱动策略**（适用于大型补丁）：不要试图一次性完美移植所有变更。更高效的流程是：

```
核心变更（头文件 + 实现）→ 编译 → 根据错误列表迭代修复 → 编译通过
```

每次编译后，按错误信息分类：
1. **"cannot convert X to Y"** → 批量 `sed` 修复调用点的 `.get()` 添加/移除
2. **"no matching function for call"** → 函数签名遗漏，编辑声明
3. **"no declaration matches"** → header/cc 签名不一致，对齐
4. **"defined but not used"** → 旧函数已无调用者，删除或标记 `inline`

每一轮编译-修复循环后，错误数应显著减少。以 0 errors 为完成标志。

**构建命令发现**：如果用户在目标目录中没有提供构建命令：
- 优先查找目标自带的本地构建脚本（如 `local_build.sh`，或 fork 自带的其它构建脚本）
- 否则查找 `Makefile`，使用 `make static_lib -j$(nproc)` 编译静态库
- 如果链接失败（如 `typeinfo` 未定义），先尝试 `make clean && make ...`
- 验证 db_bench 工具可构建：`make db_bench -j$(nproc)`（用于后续性能测试）

### 第 5.5 步：功能验证（FUNCTIONAL VERIFY）—— 验证补丁意图是否真正落地

编译通过只证明「代码能编译」，不证明「补丁想做的事真的发生了」。这一步验证后者：**补丁声称的行为，在目标版本里是否达成**。

#### 先明确要验证什么

回到第 1 步的分析结果——每个补丁都有一个核心意图。把它翻译成**可观察的行为**，作为验证目标：

| 意图类型 | 可观察行为（即验证目标） |
|----------|--------------------------|
| 添加选项/字段 | 该选项能被设置、读取，并按预期影响计算 |
| 修改算法/逻辑 | 在特定输入下，输出/结果按预期变化 |
| 新增能力/API | 新接口可调用，返回符合预期的结果 |
| 修复缺陷 | 缺陷场景下不再出错，或结果正确 |

若补丁自带测试用例（第 1 步分组里的「测试」类 hunk），它们直接编码了期望行为，应优先移植并跑通——这是最可靠的验证目标。

#### 选择验证手段（按可用性从高到低，不预设工具）

目标项目有什么就用什么，缺什么就自己搭最小的：

1. **项目自带的测试**：先按文件名、模块、被改符号在测试目录里定位相关测试，只跑相关的，不必全量跑。
2. **目标自带的命令行工具或示例**：能复现该行为的可执行程序（CLI、benchmark 工具、example）。在改动前后各跑一次，对比输出。
3. **最小复现**：前两者都不适用时，写一个几十行的小驱动/脚本触发被改的代码路径，检查输出是否符合预期；用完即弃，不必保留。
4. **人工确认**：确实无法自动化（如依赖特定硬件、外部服务）时，通读目标代码确认逻辑落地，并如实标注「未自动化验证」。

#### 两个方向都要覆盖

- **新行为成立**：补丁引入的改动按预期工作。
- **旧行为不回归**：被改模块原本的行为没被打坏——跑相关既有测试，或对比改动前后行为。

#### 力度分级（不必每个补丁同等待遇）

- **纯机械/小改动**（常量调整、加字段、签名等价替换）→ 编译通过 + 相关既有测试冒烟即可。
- **算法/逻辑改动** → 必须有针对性验证（测试或最小复现），不能只靠编译。
- **热路径/性能优化** → 功能验证之外，另走第 5.6 步性能验证。

#### 失败怎么办

功能验证失败说明移植有语义问题，常见原因：类型适配错了、漏了某个 hunk、fork 冲突决策错误、或补丁依赖的前置基础设施没补全。回到第 2 步（侦察）与第 4 步（移植）排查，不要用「代码看起来对了」自我说服。

#### 记录结果

把验证方式与结论记入第 7 步移植报告的「功能验证」栏。无法自动化验证的，明确标注验证到了什么程度，不要含糊写「通过」。

### 第 5.6 步：性能验证（可选的基准测试）

如果补丁声称是性能优化，或者涉及热路径变更，应在提交前进行性能对比验证。

**使用 Git Worktree 进行基线对比**：

```bash
# 1. 创建基线 worktree（基于移植前的 commit）
git worktree add /tmp/<project>_baseline <baseline_commit>

# 2. 在 worktree 中编译 db_bench
make -C /tmp/<project>_baseline clean && make -C /tmp/<project>_baseline -j$(nproc) db_bench

# 3. 用基线和新代码分别填充 DB 并运行 seekrandom
# 基线：
/tmp/<project>_baseline/db_bench --db=/tmp/bench_base --benchmarks=fillrandom ...
/tmp/<project>_baseline/db_bench --db=/tmp/bench_base --benchmarks=seekrandom ... | grep seekrandom

# 新代码：
./db_bench --db=/tmp/bench_new --benchmarks=fillrandom ...
./db_bench --db=/tmp/bench_new --benchmarks=seekrandom ... | grep seekrandom

# 4. 清理
git worktree remove /tmp/<project>_baseline
```

**基准测试要点**：
- 跑 3 次取平均值，消除噪音
- 使用 `--use_existing_keys=true` 确保只读存在的 key
- 记录每次移植后的性能变化（+X%或-Y%）
- 性能测试结果应写入移植报告的"性能影响"部分

**测试完成后清理 worktree**：`git worktree remove <path>`

### 第 6 步：提交（COMMIT）—— 为每个补丁创建 git commit

**编译通过后、处理下一个补丁前**，必须将当前补丁的所有变更提交到 git。这是版本管理的关键步骤，确保每个补丁对应一个可追溯的 commit，便于回滚和审查。

**提交规则**：

1. **一个补丁一个 commit**——每个补丁的所有变更（包括适配修改和编译修复）合入同一个 commit，不要拆分。
2. **先 stage 再 commit**——使用 `git add` 将所有被修改的文件加入暂存区，然后 `git commit`。
3. **提交信息格式**：

```
[目标版本标识] 回合: {补丁文件名}

源版本: {源版本标识}
补丁意图: {一句话描述该补丁的核心意图}
适配说明: {简要说明哪些 hunk 做了适配修改、哪些被跳过}

Co-Authored-By: Claude <noreply@anthropic.com>
```

示例：

```
[frocksdb-6.20.3] 回合: 0001_autumn.patch

源版本: rocksdb v6.26.1
补丁意图: 添加 autumn_c 选项（层级乘数的标量因子）
适配说明: CalculateBaseBytes 循环结构不同，使用目标版本的 num_levels 替代 num_levels_in_use_；build.sh 变更已跳过

Co-Authored-By: Claude <noreply@anthropic.com>
```

4. **验证提交**——commit 后运行 `git log -1 --stat` 确认提交内容和文件列表正确。
5. **编译失败则不提交**——如果编译未通过，继续修复，直到编译干净后才提交。绝不为编译不通过的代码创建 commit。
6. **功能验证尽量在提交前完成**——功能验证（第 5.5 步）失败的补丁，先排查语义问题并修复后再提交；确实因环境或工具缺失无法验证的，在提交信息中注明验证程度，不要含糊当作已通过。
7. **部分移植也提交**——如果补丁只有部分 hunk 成功移植（其余被跳过），仍然提交，但在提交信息中明确说明跳过了哪些以及原因。

**为什么要逐补丁提交**：
- 每个补丁是独立的逻辑单元，单独提交便于代码审查
- 如果后续补丁引入问题，可以精准 `git revert` 到任意补丁版本
- `git bisect` 可以精确定位哪个补丁引入了回归
- 适配补丁文件（输出 2）需要基于每个 commit 生成干净的 diff

### 第 7 步：报告与输出（REPORT & OUTPUT）—— 生成交付物

所有补丁移植完成（或尝试完成）后，产生三个输出：

#### 输出 1：移植报告

```markdown
# 补丁移植报告

## 概要
- 已处理补丁：N
- 直接移植：X 个 hunk
- 适配移植：Y 个 hunk
- 已跳过：Z 个 hunk（含框架/构建特有文件）
- 编译结果：通过/失败
- 功能验证：通过 / 部分验证 / 未验证（注明方式）

## 各补丁详情

### 0001_autumn.patch
**意图**：[描述]
**状态**：[已移植 / 部分移植 / 已跳过]

| 文件 | 类别 | 备注 |
|------|------|------|
| include/rocksdb/advanced_options.h | DIRECT_PORT | 添加了 autumn_c 字段 |
| db/version_set.cc | ADAPTED_PORT | CalculateBaseBytes 循环结构不同 |
| build.sh | SKIP_NO_FILE | build.sh 变更一律忽略 |
```

（可选）若在移植过程中维护了结构化日志（JSON 结构见 `scripts/port_report.py` 内嵌示例），可用 `scripts/port_report.py <log.json> <报告.md>` 自动生成上述报告；否则直接手写该模板即可。

#### 输出 2：适配补丁文件

对于每个成功移植的补丁，生成适配补丁文件：
- 命名：`{原始名称}_{目标版本}.patch`（如 `0001_autumn_frocksdb-6.20.3.patch`）
- 方法：基于第 6 步的 commit，用 `scripts/generate_adapted_patch.sh <target_dir> <补丁名> <目标版本> [commit_ref] [output_dir]` 生成该补丁对应的干净 diff（脚本内部用 `git show <commit>` 提取单个 commit 的差异，不会混入其它补丁或未提交的改动）
- 存放于原始补丁所在目录，或用户指定的位置

`commit_ref` 默认为 `HEAD`（即刚提交的那个补丁），一般无需显式传入。

#### 输出 3：使用说明 README

为每个适配补丁创建 `{名称}_{目标版本}.README`：
- 源补丁和版本
- 目标版本
- 应用说明（`git apply --check` 然后 `git apply`）
- 依赖链（哪些补丁必须先应用）
- 修改摘要
- 与原始版本的差异（适配过程中修改了什么）

## 适配模式

### 字段添加
向结构体/类添加字段需要触及多个位置：
1. 头文件中的声明
2. 构造函数初始化
3. 序列化/dump 运算符
4. C API get/set（如适用）
5. JNI 绑定（如适用）
6. 工具标志（db_bench、db_stress）
7. 校验逻辑

### CPU 特性优化
添加架构特定代码（SIMD、SVE 等）：
1. 构建系统检测（`build_detect_platform` 或 `CMakeLists.txt`）
2. 新源文件或内联代码
3. 运行时分发（如 ARM 的 `getauxval`）
4. 回退路径（必须始终存在）

### 缺失基础设施
当目标缺少补丁所需的字段/类型时：
1. 检查是否可以独立添加（简单字段通常可以）
2. 添加最少必要的基础设施
3. 然后移植依赖变更
4. 记录为 MISSING_INFRA → ADAPTED_PORT

### 类型迁移
当补丁变更指针类型（如 `T*` → `shared_ptr<T>`）时：
- 这是最难的模式。完整决策框架见 `references/type-migration.md`。
- 三种策略：完全迁移、选择性适配、跳过类型变更
- 选择依据：后续补丁依赖、目标现有类型用法、投入与收益

## 边缘情况

- **补丁间的循环依赖**：按顺序处理。如果补丁 B 需要补丁 A 中尚未移植的内容，先移植 A 的那部分。
- **分支特定冲突**：比较两个实现。优先使用分支的方式，除非补丁的变更正是移植的核心目的。记录决策。
- **仅测试变更**：如果测试在目标中存在则移植；否则跳过。
- **新文件**：如果补丁添加了全新文件，按原样移植，仅根据目标版本调整类型/API 差异。

## 脚本

- `scripts/patch_inventory.py` — 解析补丁、与目标交叉引用、识别依赖
- `scripts/verify_compilation.sh` — 运行构建、捕获错误、返回结构化结果
- `scripts/generate_adapted_patch.sh` — 从单个 commit 的 git show 生成适配的 .patch 和 .README
- `scripts/port_report.py` — 从过程日志生成结构化移植报告

## 参考文件

- `references/assessment-guide.md` — 每个评估类别的详细示例
- `references/type-migration.md` — 指针类型迁移的决策框架
- `references/patch-anatomy.md` — 如何阅读和分解复杂补丁

## 实战经验教训

以下是从 rocksdb v6.26.1 和 frocksdb 6.20.3 两次完整移植中总结的关键教训：

### 1. 补丁间依赖必须显式文档化

PR #9407（prefix_extractor 快路径）依赖 PR #8692（MetaBlockIter 重构）对 `block_based_table_reader.cc` 的变更。如果在分析阶段就识别并记录这种依赖，后续移植时可以避免上下文混淆。

**做法**：在第 1 步分析时，检查补丁是否修改了前序补丁已变更的文件。如果是，记录依赖关系。

### 2. 安全凭证不要出现在命令行

`echo "token" | gh auth login` 会将 token 暴露在 shell 历史、进程列表和对话记录中。一旦泄露，自动模式安全策略会持续阻止后续 Bash 命令。

**做法**：始终通过 `gh auth login` 交互式认证，或通过 `GITHUB_TOKEN` 环境变量（从文件读取，不 echo）。

### 3. 目标版本 API 差异是最大的陷阱

frocksdb 6.20.3 使用 `prefix_extractor->Name()` 而非 `AsString()`、`ParseSliceTransform()` 而非 `SliceTransform::CreateFromString()`。这些 API 差异在编译阶段才暴露，但如果在侦察阶段主动 grep 目标的关键符号，可以提前发现。

**做法**：在第 2 步侦察时，对补丁中每个关键函数调用，在目标中搜索同名符号。如果不存在，搜索模块内的类似方法名。

### 4. 编译-修复循环比完美规划更高效

第一次移植 PR #9407 时尝试逐文件规划所有变更，耗时巨大且容易遗漏；第二次改用编译驱动策略，效率显著提升。这正是第 5 步「编译驱动策略」的来源——先做核心变更、让编译器报错驱动修复。

### 5. `sed` 批量处理机械变更是正确的

类型迁移（`const T*` → `shared_ptr<T>`）涉及 50+ 个调用点的 `.get()` 添加/移除，用 `sed` 批量处理 + 编译验证比逐文件 Edit 快约 10 倍，关键是利用编译器作为安全网。具体策略见第 4 步「处理大规模机械变更」。

### 6. 性能收益与基线版本密切相关

PR #9407 在 rocksdb v6.26.1 上收益 +29.4%（因为 6.26 引入了 `AsString()` 性能回退），但在 frocksdb 6.20.3 上仅 +2.7%（6.20.3 没有这个回退）。这说明对于性能优化补丁，需要理解优化的前提条件在目标版本中是否存在。

**做法**：在第 1 步分析时，识别补丁修复的"问题"在目标版本中是否存在。如果目标版本没有这个问题，性能优化的收益会很小，但移植仍然有价值（为后续版本升级保留快路径）。
