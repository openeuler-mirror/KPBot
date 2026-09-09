# 序列化（L4）与构建（L5）优化

> **何时读**：分段计时显示反序列化/序列化占比可观；请求是特征类 proto（大量 repeated string / map 字段）；要调编译 flags（-march/-O3）；要管理依赖版本与离线构建；推理服务容器化。

这两层常被遗忘，但各有硬收益：特征类推理请求的 protobuf 编解码能吃掉两成以上 CPU；构建 flags 决定 L2 层所有 SIMD 代码是否真的编进了产物。

---

## 1. protobuf 热路径优化清单（L4）

以下七类优化来自对 protobuf 解析/序列化热路径的逐函数改造，可作为候选方向，但内部 API、分配对齐、解析边界与容器不变量依赖具体版本；按锁定源码验证后再打补丁（依赖管理见 §2）：

| # | 优化 | 模式 | 为什么有效 |
|---|---|---|---|
| 1 | **Arena 分配与 padding 优化** | 核查具体调用点的真实对齐要求，消除可证明冗余的 padding；始终满足请求对齐 | 历史路径的对齐钳制不能推广为 `min(align,16)`，过对齐对象必须保留其要求 |
| 2 | **字符串读取改追加** | `ReadString` → `AppendString`；map entry 先 `clear()` 再 append | 新建空串上两者语义等价，但省一次中转；重复 key 覆盖时复用原串容量。可配 `__builtin_prefetch` |
| 3 | **repeated 前瞻 Reserve** | 解析前向前扫描同 tag 的连续条目数，一次性 `Reserve(size+count)` | 消除几何扩容的反复 realloc/搬移；repeated string 多的特征请求收益大 |
| 4 | **map 前瞻预扩 + 免检插入** | lookahead 数 entry 数后按 key 类型一次性扩桶；插入跳过负载因子检查/收缩分支 | 消除每次插入的重哈希判定；大 map 特征字段收益大 |
| 5 | **SIMD varint 编解码** | 批量一次处理 8×u32 / 4×u64：CLZ 算字节数、位分离 7bit 组、查表、成对写 8 字节；含"全 <0x80 单字节"快路径与长值慢路径，尾部标量兜底 | varint 是特征 ID/时间戳类字段的编解码热点，标量逐字节处理浪费 SIMD 宽度。ARM 上用 SVE2 intrinsic，x86 等价 AVX2/AVX-512 |
| 6 | **packed varint 解析快路径** | 一次读 8 字节判边界，SIMD 提取 7bit 组 | 同上，解析方向 |
| 7 | **TLS block 自由链表** | thread_local、LIFO、上限 N 块的缓存：`New` 优先 Pop、`Delete` Push | 消除 arena 解析期内存块 new/delete 抖动；TLS 天然免锁 |

工程要点：
- **平台守卫**：非目标平台保持原生行为（`#if defined(__target_arch__)`），让同一补丁双平台可编（x86 修复 build 的 PR 才能过）。
- **SIMD 段的激活前提**：SVE2 代码段以 `__ARM_FEATURE_SVE2` 为门槛——**构建参数里没有 +sve2 时它根本不会编进产物**。打完补丁务必用反汇编/符号检查确认目标段真的激活了（真实踩坑：优化补丁合入、构建 flags 没跟上，SIMD 代码静静躺着）。
- **正确性测试**：与原版解析/序列化差分验证，覆盖截断/畸形 varint、重复 map key、空值、长度溢出、缓冲区尾部、过对齐分配和超大 repeated；TLS 缓存检查上限、线程退出释放与隔离。SIMD/lookahead 不得越过合法可读范围。
- **收益场景判断**：先确认你的请求 proto 真的重（repeated/map/packed 字段多、payload 大）。echo 类小 proto 优化无感；特征类请求（一条几百个字段）才是主战场。

## 2. 依赖版本管理（升级 → 回退 → 回移植）

实战剧本（protobuf，但模式适用于任何重依赖）：

1. **升级**：构建系统迁移（WORKSPACE→bzlmod）顺带把 protobuf 5.x 升到 33.x——版本由新构建生态决定，而非性能诉求。代价立现：**下游 RPC 框架大面积不兼容**（descriptor 访问器返回类型从 `const string&` 变 `string_view`，~19 个文件要改）、自家代码零星适配。
2. **回退**：兼容成本超预期 → 版本回退到框架官方测试过的版本，但**保留性能优化**——把优化补丁回移植到旧版 API（旧的谓词宏、旧的类型判别、返回值语义差异逐条适配）。
3. **双版本补丁并存**：新版补丁（无平台守卫）与旧版补丁（全量 `#if` 守卫）同时维护，按最终选型取用。

教训：
- 重依赖升级的决策变量是**下游适配成本**，性能补丁反而是可搬资产（设计补丁时就考虑可移植性）。
- 升级期间两条依赖声明路径可能并存（bzlmod 的 override + WORKSPACE 的 archive）——**用构建产物实际验证哪个生效**，别信注释。
- 适配 patch 打在依赖仓库上（`patch_file` 注入），升级时 patch 失败要硬报错。

## 3. 构建体系（L5）

| 项 | 做法 | 说明 |
|---|---|---|
| **指令集目标** | `-march` 对准目标微架构（如 ARM `armv8.5-a+fp+simd+bf16+sve`；x86 按目标机 `avx2`/`avx512`；拿不准用 `-march=native` 仅限同质集群） | 决定 L2/L4 的 SIMD 段是否激活；算子库与框架主代码可以不同目标（库更激进） |
| **优化级别** | `-O3` 显式写在构建脚本里（别依赖默认） | 显式化后可审计 |
| **平台裁剪** | 构建系统 select：目标平台编入定制代码，其余标记 incompatible | 非目标平台零成本 |
| **C++ 标准** | 与框架一致显式固定（如 `-std=c++17`） | 避免 host/target 不一致 |
| **系统库替换** | 能用系统库的（ssl/压缩）用 `TF_SYSTEM_LIBS`/`use_system_libs` 切换 | 镜像内版本可控 |
| **离线构建** | `--distdir`（依赖离线包）+ `--output_user_root`（编译缓存） | 内网/受控环境必需；发布依赖包清单进文档 |
| **ulimit** | 构建/运行容器 `--ulimit nofile=1048576` | 构建工具的 eventfd 会撞默认 1024 |
| **多架构** | Dockerfile 按 `TARGETARCH` 分派 flags（x86 `-march=native` / ARM `-march=armv8.5-a`） | 一份 Dockerfile 双平台 |
| **工具链** | 全程 GCC（openEuler gcc-toolset 固定版本） | 可预测；clang 切换是独立工程 |

**容器化推理服务的规格化**（让基准可复现的前提）：
- 分别固定 cpuset 与 CFS quota/period，记录有效 CPU 集合和时间预算；二者含义不同，具体预算按压测目标设置（见 runtime §2）。
- host network（压测去网络虚拟化干扰）、按需 privileged。
- 镜像里固化：OS + 工具链 + 构建系统版本 + CA 信任（公司证书要同时进系统 truststore、构建工具 JVM truststore、Python certifi——漏一处就构建失败）。

**验证构建生效**：改完 flags 用符号/反汇编抽查（`objdump`/`nm` 看 SIMD 符号是否存在）；这只证明目标代码存在；还须通过实际调用栈或路由计数证明请求执行到了该路径。

## 4. 反模式

- **补丁合了、flags 没开**：SIMD 优化静静躺在 `#if` 后面。构建后验证目标代码段存在。
- **升级决策只看收益清单**：下游适配成本（RPC 框架、自家代码、构建系统）才是主要变量。
- **两条依赖声明并存且不验证**：注释说 A 生效、实际 B 生效。用产物验证。
- **不记录资源约束就压测**：明确独占/共享、quota 是否无限制、cpuset 与亲和性；未限 quota 的独占环境也可作为有效基准。
- **-march=native 上异构集群**：构建机与运行机微架构不同，SIGILL。
- **忘 ulimit**：构建慢/失败查半天，其实是 fd 上限。

## 实战案例摘要

序列化层：7 类 protobuf 热路径优化（v33 补丁 903 行 + 回移植旧版补丁 1021 行双维护，含 SVE2 varint 批量编解码与 TLS block 缓存）。版本管理：bzlmod 迁移带动 5.28.3→33.0 升级 + 863 行 RPC 框架适配补丁 + 回退 5.28.3 并回移植优化。构建层：armv8.3→armv8.5 指令集演进、-O3 显式化、平台 select、distdir 离线构建、双架构 Dockerfile、配额容器规格化。踩过的坑：SVE2 段因构建参数无 +sve2 而未激活、MODULE/WORKSPACE 双路径并存需产物验证——都已写进上文对应小节。
