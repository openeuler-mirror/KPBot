---
name: kptools-jvmjit
description: >
  使用 perf + JVMTI agent（libperf-jvmti.so）对 JVM 进程做 JIT 符号化采样分析：
  方法级 CPU/指令热点（perf inject --jit 把 JIT 代码注入符号表）、JIT 代码反汇编级
  下钻（jitted-*.so 可直接 objdump）、容器内 Java 进程的指令级采集。当用户遇到
  Java 进程 perf 采样全是 unknown / [JIT] 裸地址、需要 JIT 反汇编或指令级热点、
  提到 libperf-jvmti、perf inject --jit、-agentpath、容器里 Java 进程 perf 采集时
  触发。运行中 Java 的免重启方法级交互分析 → 外部工具 arthas（非本仓库 skill）；
  采样火焰图全景 → kptools-devkit java-perf。
license: Apache-2.0
compatibility: Linux aarch64/x86_64；openEuler 22.03/24.03 + perf 6.6 + JDK 17 实测验证；JVM 须以 -agentpath 启动期加载 agent（无法运行期附加）
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: java_performance_analysis
  version: "1.0.0"
---

# kptools-jvmjit — JVM JIT 符号化 perf 采样分析

## 职责边界

本 Skill 解决 **JVM 进程在 perf 采样下符号缺失 / 无指令级视角** 的问题，产出符号化热点与 JIT 代码反汇编证据。

- 方法级耗时分解、入参返回值观察等免重启交互分析 → 外部工具 arthas
  （https://github.com/alibaba/arthas，非本仓库 skill，勿当作可加载的技能调用）
- Java 采样火焰图全景（CPU/ALLOC/LOCK）→ kptools-devkit java-perf（aarch64 优先）
- 分析出的 JIT 代码热点 → 源码/字节码移交 code-optimizer（向量化、内联、精度）
- Python → kptools-py-spy；系统级 CPU/内存/IO 趋势 → kptools-nmon
- GraalVM native image 是普通原生二进制 → 直接用标准 perf 流程，本 Skill 出界

## 工作流程

1. 先决检查：`bash scripts/check.sh -p <PID>`（perf/agent/JVM 启动参数/容器能力）
2. **关键先决**：目标 JVM 必须以 `-agentpath:.../libperf-jvmti.so` 启动（无法对运行中 JVM 补加）；缺失时给出重启指引并退出
3. 采集：`bash scripts/collect.sh -p <PID> [-d 秒]`；容器目标用 `--container <名称>`
4. 解读 `report.txt`：方法级占比 → 对热点 jitted-*.so 做 objdump 下钻（指令级）
5. 若 report 出现 `[JIT] tid` 裸地址条目：dump 滞后，参考 jvmjit-guide §4 刷盘语义处理（测试环境 `--stop-jvm`，生产等优雅重启窗口后重新 inject）

## 性能场景速查

| 场景 | 第一步 | 深挖 |
|------|--------|------|
| perf 采样全是 unknown 符号 | `collect.sh -p <PID>` | report.txt 方法级占比 → objdump 热点 jitted-*.so |
| 容器内 Java 进程采集 | `collect.sh -p <PID> --container <ctr>` | 工具注入 `toolpack.sh --container <ctr>`；权限要求见下表 |
| 热点是否被 C2 向量化 | objdump jitted-*.so 查热点循环 | 出现 SVE（`ptrue`/`ld1d`/`fmul z`）或 NEON（`fmla v`/`ldr q`）=已向量化；d 寄存器标量 FP 链 → 移交 code-optimizer |
| 优化前后 JIT 代码对比 | 两轮采集对比 report + 反汇编 | 结构性证据（如 NEON 占比 0→38%）比单一耗时指标更可解释 |
| JIT 代码内指令热点 | report 定位符号 → annotate.txt 逐指令周期占比 | 快速单函数下钻用 objdump 对应 jitted-*.so（guide §6） |

## 批处理采集（agent 必用，非交互）

```bash
# 宿主机 JVM：flat 热点（默认事件 cycles,instructions）
bash scripts/collect.sh -p <PID> -d 8 -o ./jvmjit-evidence/

# 带调用栈（JVM 需已加 -XX:+PreserveFramePointer）
bash scripts/collect.sh -p <PID> -d 10 --call-graph

# 容器内 JVM（核心场景）：dump 与 PID 都在容器命名空间内，采集必须在容器内执行
bash scripts/collect.sh -p <PID> --container web-ctr -d 8

# 镜像内无 perf 时先做工具注入（宿主机 perf + 非 glibc 依赖打包 docker cp）
bash scripts/toolpack.sh --container web-ctr

# 测试环境允许重启时：采集后优雅退出 JVM 触发 dump 全量刷盘（生产禁用）
bash scripts/collect.sh -p <PID> -d 8 --stop-jvm
```

## 输出

- `raw.data`（原始 perf 数据）、`jitted.data`（inject 后）、`report.txt`（方法级热点表）
- `annotate.txt`：**指令级注解**（源码行 + 汇编 + 逐指令周期占比，`perf annotate --stdio` 导出；全符号反汇编较慢，限时执行见参数表）
- `manifest.json`：采集参数、dump 路径、jitted .so 清单、`[JIT]` 残留占比、annotate 行数
- stdout: 证据目录路径；stderr: dump 滞后告警、能力缺失告警
- 退出码: 0 成功；2 先决缺失（perf/agent 未加载/PID 非 JVM）；3 参数错误；1 采集失败

## collect.sh 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标 JVM（容器模式为容器内 PID） | 必填 |
| `-d <sec>` | 采样时长（秒，必须为正整数） | 8 |
| `-e <events>` | perf 事件 | `cycles,instructions` |
| `-o <dir>` | 证据目录 | `./jvmjit-evidence/<时间戳>/` |
| `--container <ctr>` | 容器名称，切换为容器内执行链 | 宿主机模式 |
| `--tools <dir>` | 容器内工具目录 | `/tmp/jvmjit-tools` |
| `--call-graph` | 加 `-g --call-graph fp` 调用栈 | flat 模式 |
| `--dump <path>` | 显式指定 jitdump 路径 | 自动发现（$HOME/.debug/jit/java-jit-*/jit-<PID>.dump） |
| `--stop-jvm` | 采集后 `kill -TERM` 目标触发刷盘 | 关闭（生产禁用） |
| `--annotate-timeout <sec>` | 指令级注解导出（annotate.txt）限时（秒，必须为正整数） | 10（保守值；实测 10MB/58k 样本 <45s 完成，大数据集/需完整注解时显式提高） |

## 约束

- **agent 只能启动期加载**：`-agentpath` 无法对运行中 JVM 附加；生产接入须在启动脚本/JVM 参数预置，这决定了本 Skill 需要"部署期规划"，与 arthas 的免重启 attach 互补
- **perf record 必须带 `-k 1`**：perf 6.6 的 `inject --jit` 强制要求（CLOCK_MONOTONIC 对齐），缺失时报 `jitted code must be sampled with perf record -k 1`（本机实测）
- **容器采集三要素**（详见 guide §5）：① 采集命令必须在目标容器内执行（PID 命名空间 + dump 文件系统）；② 容器需 perf 权限：新版运行时 `--cap-add=PERFMON`，旧版（实测 docker 18.09 不识别该名称）用 `--cap-add SYS_ADMIN`；③ 镜像无 perf 时用 toolpack.sh 注入宿主机工具（同架构 + 容器 glibc ≥ 宿主 glibc 即可运行）
- **dump 刷盘语义**：agent 增量缓冲、JVM 退出时全量刷盘；record 窗口内新编译的代码在 dump 中滞后，inject 后表现为 `[JIT] tid` 裸地址条目（实测占比可高达 ~80%），等待刷盘/优雅退出后重新 inject 即收敛
- `--call-graph` 依赖 JVM `-XX:+PreserveFramePointer`（JVMTI agent 只解决符号，不解决 Java 帧回溯）
- jitted ELF 由 inject 生成在 **dump 同目录**（非 cwd），objdump 按该路径取用
- 采样有观测开销：事件数 ≤3、时长 ≤30s 起步，生产先小窗口确认再延长

## 错误处理

- check.sh 报 `容器内无 perf（镜像未内置且未注入）` → 先 `toolpack.sh --container <ctr>` 注入宿主工具（**无需重建容器**），再重跑 check.sh
- `perf_event_open ... Operation not permitted`（容器内，perf 已存在）→ 容器缺 perf 权限：重启容器加 `--cap-add=PERFMON`（旧运行时 `SYS_ADMIN`），见 guide §5.1；注意区分"无 perf（注入即可）"与"无权限（才需能力）"，check.sh 已分开判定
- check.sh 报 `agent not loaded` → JVM 未带 `-agentpath` 启动：在启动参数追加 `-agentpath:/usr/lib64/libperf-jvmti.so` 后重启；容器场景把 agent cp 进容器再重启
- report 出现 `[JIT] tid` 裸地址 → dump 滞后（非故障）：等 dump 增长或优雅退出后重新 inject；测试环境直接 `--stop-jvm`
- `jitted code must be sampled with perf record -k 1` → record 未带 `-k 1`，collect.sh 已内置；手工执行时补上
- 找不到 dump → agent 未加载或 HOME 变化：`grep -oP 'jitdump in \K\S+' <java启动日志>` 拿真实路径，或 `--dump` 显式指定
- inject 成功但 report 无 Java 符号 → 采样窗口内负载过冷（JIT 未编译热点）：延长预热或 `--stop-jvm` 刷盘后重看

## 参考

- [JVM JIT 采集指南](references/jvmjit-guide.md) — agent 机制与 dump 刷盘语义（实测数据）、-k 1 强制原因、容器工程（权限/工具注入）、采集配方、输出解读、指令级下钻、场景判定与出界路由
- [与其他 Java 分析工具分工](references/jvmjit-vs-other-tools.md) — jvmjit / arthas / java-perf / py-spy 路由表
