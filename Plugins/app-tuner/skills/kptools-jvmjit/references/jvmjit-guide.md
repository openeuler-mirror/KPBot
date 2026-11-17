# JVM JIT 符号化采集指南（libperf-jvmti + perf inject --jit）

本文档所有"实测"结论基于：openEuler 24.03 SP2 aarch64、perf 6.6.0-145、毕昇/OpenJDK 17、
Ubuntu 24.04 容器（glibc 2.39）、docker 18.09（2026-11 验证）。

## 1. 机制：为什么 JVM 在 perf 下没有符号

JVM 将字节码 JIT 编译到**匿名内存页**，perf 采样到的 PC 无法用常规符号表解析——
报告里表现为 `unknown`、裸地址或 `[JIT]` 标记。`libperf-jvmti.so` 是 perf 源码树自带的
JVMTI agent（openEuler 装在 `/usr/lib64/libperf-jvmti.so`）：

1. JVM 启动时加载 agent（`-agentpath:.../libperf-jvmti.so`）
2. agent 订阅 JVMTI `CompiledMethodLoad/Unload` 事件，把**每个 JIT 方法连同机器码字节**写入 jitdump 文件
3. `perf inject --jit` 读取 dump，为每个 JIT 方法生成 ELF（`jitted-<pid>-<N>.so`，位于 **dump 同目录**），并把 MMAP 事件注入 perf.data
4. `perf report` 即可显示 `pkg.Class.method(params)` 级符号；jitted ELF 可直接 `objdump` 反汇编（**指令级证据**）

dump 路径（实测，perf 6.6）：

```
$HOME/.debug/jit/java-jit-<时间戳>/jit-<pid>.dump     # 新版 agent
/tmp/perf-<pid>.dump                                  # 旧版 agent（历史遗留，同样支持）
```

JVM 启动日志会打印真实路径：`java: jvmti: jitdump in <path>`。

## 2. 先决条件清单

| 先决 | 检查方式 | 不满足时 |
|------|---------|----------|
| perf 支持 `--jit` | `perf inject --help 2>&1 \| grep -- --jit` | 升级 perf（≥4.x 系发行版均带） |
| agent 文件存在 | `ls /usr/lib64/libperf-jvmti.so`（openEuler/CentOS）；Debian 系在 `/usr/lib/linux-tools-*/` 下，未实测 | 安装 perf 包或从内核 tools 源构建 |
| JVM **启动期**加载 agent | `tr '\0' ' ' < /proc/<pid>/cmdline \| grep agentpath` | **无法运行期补加**：启动参数追加后重启 |
| 调用栈模式 | 同上 grep `PreserveFramePointer` | 只用 flat 模式（不加 `-g`） |
| 容器采集权限 | 容器内试跑 `perf stat -e cycles -p <pid> -- true` | 重建容器加能力，见 §5.1 |

## 3. 采集配方

```bash
# flat 热点（默认，最稳）
perf record -k 1 -p <PID> -e cycles,instructions -o raw.data -- sleep 8

# 带调用栈（JVM 须 -XX:+PreserveFramePointer）
perf record -k 1 -p <PID> -e cycles -g --call-graph fp -o raw.data -- sleep 10

# 注入符号
perf inject -i raw.data --jit -o jitted.data

# 方法级热点（多事件时 report 按事件分节）
perf report -i jitted.data --stdio --no-children --percent-limit 0.5
```

### 3.1 为什么必须 `-k 1`

perf 6.6 的 `inject --jit` 要求 jitted MMAP 时间戳与采样时间戳同源（CLOCK_MONOTONIC），
缺失时直接失败：

```
error, jitted code must be sampled with perf record -k 1
0x94818 [0xa0]: failed to process type: 10 [Operation not permitted]
```

`-k 1` 即 clockid=CLOCK_MONOTONIC。`-k` 自 perf 4.x 起可用；若旧版拒绝数字形式，
改用 `-k mono`。collect.sh 已内置该参数。

### 3.2 事件选择

| 事件 | 用途 | 备注 |
|------|------|------|
| `cycles` | CPU 热点主证据 | 必选 |
| `instructions` | IPC/指令占比 | 与 cycles 复用无冲突（≤6 计数器） |
| `branches,branch-misses` | 分支密集热点 | 追加时注意复用比 |
| `cache-misses` | 访存型佐证 | 与 cycles 组即可 |

生产采样从 `-d 8` 短窗口起步，确认开销与产出后再延长。

## 4. dump 刷盘语义（实测）

agent **增量缓冲、JVM 退出时全量刷盘**，运行中只有零星早期 flush：

- 实测 A（运行中 inject）：dump 停在 290816 字节，report 中 ~80% 样本显示为
  `[JIT] tid` 裸地址——它们落在 record 窗口内被 C2 重编译的代码区，dump 里还没有
- 实测 B（`kill -TERM` 后）：dump 增至 296271 字节，重新 inject 后裸地址**全部收敛**
  为 `DemoCompute.main` / `DemoCompute.fib` 符号
- 非优雅退出（`kill -9`/断电）丢缓冲尾部记录

处理策略：

| 环境 | 做法 |
|------|------|
| 测试环境 | `collect.sh --stop-jvm`：record 后优雅退出目标 JVM，一次到位 |
| 生产 | 先 inject 看已有符号（预热期编译的代码已 flush）；裸地址占比高时，等待下一个优雅重启窗口刷盘后重新 inject 同一份 raw.data |

## 5. 容器内采集（核心场景）

**结论（实测）：容器目标的整条采集链必须在容器内执行**——目标 PID 在容器 PID 命名空间内，
dump 写在容器文件系统，宿主机 perf 虽能看到容器进程，但 `inject --jit` 拿不到 dump 路径。

### 5.1 权限

容器内 `perf_event_open` 报 `Operation not permitted` = 缺 perf 能力：

```bash
docker run --cap-add=PERFMON ...        # 内核 5.8+ / 新版运行时
docker run --cap-add SYS_ADMIN ...      # 旧运行时（实测 docker 18.09 不识别 PERFMON 名称）
```

先决检测：容器内 `perf stat -e cycles -- true` 试跑。

### 5.2 工具注入（镜像无 perf 时）

宿主机 perf 二进制可在"同架构 + 容器 glibc ≥ 宿主 glibc"的容器内运行
（实测 openEuler 24.03 perf 于 Ubuntu 24.04 容器正常）：

```bash
bash scripts/toolpack.sh --container <ctr>       # 自动打包注入 + 版本验证
docker exec <ctr> bash -c "LD_LIBRARY_PATH=/tmp/jvmjit-tools/lib /tmp/jvmjit-tools/perf --version"
```

打包逻辑：`ldd perf` 全部绝对路径依赖中**排除 libc 族**（libc/libm/libpthread/libdl/
librt/ld-linux/libgcc——用容器自带的新版），其余随二进制拷入。生产最佳实践是把
agent 与 perf 直接打进镜像（Dockerfile 增加 openEuler 基镜像对应包）。

### 5.3 容器内全链路（验证通过的最小序列）

```bash
docker run -d --cap-add SYS_ADMIN --name web-ctr <image> sleep infinity
docker cp /usr/lib64/libperf-jvmti.so web-ctr:/tmp/jvmjit-tools/   # 或镜像内置
# JVM 须带 -agentpath:/tmp/jvmjit-tools/libperf-jvmti.so 启动
docker exec web-ctr bash -c \
  "LD_LIBRARY_PATH=/tmp/jvmjit-tools/lib /tmp/jvmjit-tools/perf record -k 1 -p <PID> \
   -e cycles,instructions -g --call-graph fp -o /tmp/demo/raw.data -- sleep 8"
docker exec web-ctr bash -c "kill -TERM <PID>; sleep 3"             # 触发刷盘（测试环境）
docker exec web-ctr bash -c \
  "LD_LIBRARY_PATH=/tmp/jvmjit-tools/lib /tmp/jvmjit-tools/perf inject -i /tmp/demo/raw.data --jit -o /tmp/demo/jitted.data"
docker exec web-ctr bash -c \
  "LD_LIBRARY_PATH=/tmp/jvmjit-tools/lib /tmp/jvmjit-tools/perf report -i /tmp/demo/jitted.data \
   --stdio --no-children --percent-limit 0.5"
```

## 6. 指令级下钻

两条互补路径：

### 6.1 perf annotate 全量注解（首选，含逐指令周期占比）

```bash
perf annotate -i jitted.data --stdio --percent-limit 1 > annotate.txt
```

实测（perf 6.6，10MB/58k 样本，openEuler 24.03 aarch64 + JDK 17）：<45s 完成，
产出含**源码行穿插 + 逐指令周期占比**（以下为 DemoCompute.saxpyLoop 被 C2 以
SVE 向量化后的真实采集）：

```
 Percent |  Source code & Disassembly of jitted-2157837-378.so for cycles
         : 24  static void saxpyLoop(double alpha) {
         : 26      for (...) c[i] = alpha * a[i] + b[i];   ← 源码行穿插
    0.00 :    94:     ptrue   p7.b                         ← SVE 谓词初始化（向量化签名）
    0.35 :   2ac:     ld1d    {z19.d}, p7/z, [x14]         ← SVE 聚合 load：a[i] 段
    0.00 :   2b4:     fmul    z19.d, z19.d, z18.d          ← alpha * a[i]（z 寄存器向量乘）
   18.49 :   2e0:     ld1d    {z20.d}, p7/z, [x18]         ← b[i] 段
   18.69 :   2ec:     st1d    {z19.d}, p7, [x18]           ← SVE 聚合 store：写回 c[i]
   24.13 :   320:     ld1d    {z20.d}, p7/z, [x13]         ← 最热指令：访存占比主导
```

- `--percent-limit 1` 跳过冷符号，是全量导出的性能关键（collect.sh 内置）
- **`-s` 过滤的坑**：`-s` 须用 **ELF 符号名**（`LDemoCompute;fib(J)J`，`;`/`(`/`)`/`[` 等 JVM 内部签名格式），写 `DemoCompute.fib(long)`（report 展示名）匹配不到 → **静默回退全符号反汇编**，表现为"卡死"（实为慢）
- 大数据集全量注解可能数分钟：collect.sh 用 `--annotate-timeout`（默认 10s，保守值避免拖长采集流程）硬界，超时保留部分输出不判失败；需要完整注解时显式提高（如 `--annotate-timeout 120`）

### 6.2 objdump 单函数快速路径

`jitted-<pid>-<N>.so` 由 inject 生成在 **dump 同目录**（非 cwd），直接反汇编（零等待、无逐指令占比）：

```bash
objdump -d "$DUMP_DIR/jitted-61-397.so" | head -40
```

判读要点（aarch64，指令示例均为 openEuler 24.03 aarch64 + JDK 17 实测）：

- 符号名形如 `LDemoCompute;fib(J)J`（JVM 内部签名：L类名;、J/B/I 对应类型）
- **C2 已向量化签名**：
  - SVE：谓词初始化 `ptrue p7.b` + z 寄存器算术 `fmul z19.d, z19.d, z18.d` /
    `fadd z19.d, ...` + 聚合访存 `ld1d {z19.d}, p7/z, [x18]` / `st1d {z19.d}, p7, [x18]`
  - NEON：`fmla v0.2d, ...` + `ldr q0, [x]` / `str q0, [x]`（v/q 寄存器）
- **未向量化特征**：`fmul d18, d18, d16`、`fadd d18, d18, d19` 等 d 寄存器标量 FP
  串行链 = 向量化机会 → 移交 code-optimizer（apply-vectorization / source-transform-autovec）
- **占比归因（实测案例）**：saxpyLoop 已被 SVE 向量化，但 24.13% / 18.69% 周期集中在
  `ld1d`/`st1d`，`fmul`/`fadd` 为 0% → **已向量化但 memory-bound**，优化方向是访存
  （prefetch-optimization / memory-access-optimization），继续压算术无收益
- `bl <stub>` 密集、解释器帧 = 热点未走 JIT，先延长预热再采
- 用 report/annotate 定位符号 → 记下地址段 → objdump 对应 ELF 找该段指令流

## 7. 场景判定与出界路由

| 用户信号 | 判定 | 动作 |
|----------|------|------|
| "perf 采 Java 全是 unknown/[JIT]" | 符号缺失 | 本 Skill：检查 agent → 采集 → inject |
| "容器里 Java 进程怎么 perf 采集" | 容器场景 | 本 Skill 容器模式（§5） |
| "看热点是否被 C2 向量化" | 指令级 | 本 Skill + objdump 判读（§6） |
| "优化前后 JIT 代码对比" | 结构性验证 | 两轮采集 + 反汇编对比 |
| "运行中的 Java 方法耗时/入参返回值" | 免重启交互 | 出界 → 外部工具 arthas（github.com/alibaba/arthas） |
| "给 Java 采张火焰图" | 全景 | 出界 → kptools-devkit java-perf |
| "Python/Go 进程热点" | 语言路由 | 出界 → kptools-py-spy / go-pprof-analysis |
| "JVM 参数/GC 调优" | 调优动作 | 出界 → kpbot-app-tuner（本 Skill 只分析） |
| GraalVM native image | 原生二进制 | 出界 → 标准 perf 流程 |

不承诺：JDK 8 以下 agent 行为（jvmticmlr 契约存在但未实测）；Windows on Arm；
对运行中 JVM 补加 agent；`perf annotate` jitted 符号视图（用 objdump 替代）。
