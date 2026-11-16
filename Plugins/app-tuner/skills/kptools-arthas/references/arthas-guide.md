# Arthas 使用指南（kptools-arthas）

> 知识来源：Arthas 官方文档（arthas.aliyun.com/doc，Apache-2.0 开源项目）。
> 本指南只覆盖与 Java 性能分析相关的命令；命令语义以官方文档为准，标注「待实测」的内容须先在目标环境验证。

## 1. 概述与定位

Arthas 是 Alibaba 开源的 Java 运行时观测工具：attach 运行中的 JVM（免重启），提供方法级调用链耗时分解、入参/返回值观察、线程分析、GC/内存状态等性能分析能力。

与采样型工具的分工（详见 arthas-vs-java-perf.md）：

- 采样火焰图（java-perf / py-spy / tuner hotspot）回答**热点在哪**
- Arthas 回答**为什么慢**：耗时落在哪一段、参数是什么、谁持锁、GC/内存压力如何

输出为纯文本，可直接 grep/解析（java-perf 输出为自包含 HTML，无机器可解析契约）。

## 2. 安装与离线

### 2.1 在线安装（deploy.sh）

```bash
bash scripts/deploy.sh
```

下载 arthas-boot.jar 到 `~/.arthas/arthas-boot.jar`（来源 `https://arthas.aliyun.com/arthas-boot.jar`，可用 `ARTHAS_BOOT_URL` 覆盖）。

### 2.2 首次 attach 的完整包下载

arthas-boot.jar 只是引导器：**首次 attach 时会从 maven 仓库拉取完整 arthas 包并缓存到 `~/.arthas/`**，此后 attach 直接使用本地缓存。

### 2.3 离线环境（预热度缓存）

完全离线的环境须提前预热度缓存：在有网环境对任意可用 JVM（含临时进程）attach 一次，`~/.arthas/` 缓存随用户目录迁移：

```bash
# 有网环境：对一个业务 JVM（或临时 sleep JVM）执行一次批处理
bash scripts/deploy.sh --prewarm <PID>
```

容器场景建议在镜像构建期预热（参考 cases/agent-arthas-trigger/Dockerfile 的做法），运行期即可离线。

## 3. attach 机制与权限

| 机制 | 说明 |
|------|------|
| attach 通道 | JVM attach API（unix domain socket），非 ptrace；同 uid 即可，无需 yama/ptrace_scope |
| 权限 | 须与目标 JVM 同 uid，或 root；跨 uid attach 直接被拒 |
| 容器 | 须进入目标进程所在容器（`docker exec` / `kubectl exec` / `nsenter`），不支持跨容器 attach |
| JDK | JDK 9+ 走 attach API；JDK 8 由 arthas-boot 自动注入 tools.jar；验证范围 JDK 8-21 |
| 端口 | attach 后在目标 JVM 内启动 arthas 服务端：telnet 3658 / http 8563；被占用时 `--telnet-port`/`--http-port` 指定 |
| 生命周期 | 服务端 idle 常驻目标 JVM（agent + spy 字节码保留）；`shutdown` 命令完全卸载并退出 |

## 4. 三种使用方式

| 方式 | 命令 | 适用 |
|------|------|------|
| 交互 telnet | `java -jar arthas-boot.jar`（选择进程后进入交互） | 人工临时分析 |
| **批处理（agent 必用）** | `java -jar arthas-boot.jar <PID> -b -c "<command>"` | 自动化采集；`-b` 执行完自动退出 |
| collect.sh 封装 | `bash scripts/collect.sh -p <PID> -c "cmd1; cmd2"` | 统一证据目录 + 分节标记 + 超时兜底 |

collect.sh 将命令串按 `;` 拆分**逐条 attach**（每条一次 `arthas-boot -b -c` 调用），因此单条命令内不要使用含 `;` 的 OGNL 表达式。

## 5. 命令全景（按性能分析用途）

### 5.1 只读观测（无字节码增强，开销可忽略）

| 命令 | 用途 |
|------|------|
| `dashboard -n 1` | 总览快照：线程状态分布/高 CPU 线程、内存 used/total/max、GC 次数与耗时、JVM 运行时信息 |
| `thread -n 5` | 最占 CPU 的 5 个线程（附线程栈） |
| `thread --state BLOCKED` | 列出 BLOCKED 状态线程 |
| `thread -b` | 找出阻塞其他线程最多的线程（持锁者，锁竞争分析入口） |
| `thread <tid>` | 指定线程的栈 |
| `memory` | 堆/非堆各空间（Eden/Survivor/Old/Metaspace 等）used/total/max |
| `sysprop` / `sysenv` | 系统属性 / 环境变量 |
| `vmopt` | JVM 参数（GC 类型、堆配置等） |
| `mbean` / `perfcounter` | JMX MBean / JVM 性能计数器（含 GC 次数与耗时累计） |
| `sc -d <类>` | 类信息：加载器、继承链（trace/watch 前确认类名与通配符命中） |
| `getstatic <类> <字段>` | 查看静态字段值（缓存上限、线程池大小等性能常量） |
| `ognl '<表达式>'` | 运行时表达式查询，如 `ognl '@System@getProperty("java.version")'` |

### 5.2 方法级性能分析（字节码增强，有观测开销，必须 -n 限次数）

| 命令 | 用途 | 关键参数 |
|------|------|----------|
| `trace <类> <方法> '#cost > 100'` | 调用链耗时分解树（每段占比与毫秒数） | `#cost` 过滤慢调用；`-n` 限制采集次数；`--skipJDKMethod` 跳过 JDK 帧 |
| `watch <类> <方法> '{params,returnObj,throwExp}'` | 观察入参/返回值/异常 | `-x 2` 展开深度；`-n 3` 次数限制；支持 `'#cost > 100'` 条件 |
| `stack <类> <方法>` | 该方法被谁调用（栈） | `-n 3` 次数限制 |
| `monitor -c 10 <类> <方法>` | 统计窗口（默认 10s）：调用次数/RT/失败率 | `-c` 窗口秒数 |
| `tt -t <类> <方法>` | 时间隧道：录制调用现场（可查参数/耗时） | `-n 5` 录制条数；`tt -l` 列表；`tt -i <index>` 查详情 |

> `tt --replay`（重放执行）、`redefine`/`retransform`（热更新）会改变目标进程行为，不在本 Skill 只读性能分析范围。

### 5.3 内存取证（重操作，慎用）

| 命令 | 用途 | 风险 |
|------|------|------|
| `vmtool --action forceGc` | 主动触发 GC（对比回收前后判断泄漏 vs 高分配） | 触发 STW |
| `vmtool --action getInstances --className <类> --limit 10` | 统计/查看存活实例 | 需 `options unsafe true` |
| `heapdump --live /tmp/heap.hprof` | 仅存活对象堆转储 | **Full GC + STW**，文件与堆同量级；仅泄漏取证用 |

### 5.4 采样火焰图（profiler，内嵌 async-profiler）

```bash
profiler start                       # 默认 CPU 事件
profiler start --event alloc         # 内存分配事件
profiler stop --file /tmp/cpu.html   # 停止并输出火焰图（绝对路径）
profiler list / status / resume
```

实测：aarch64 + JDK 17 + arthas 4.3.5 下 CPU 事件火焰图生成正常（HTML 含 Java 方法帧）；ALLOC 事件与鲲鹏平台全景火焰图仍优先 kptools-devkit java-perf（可叠加 tuner hotspot 看 native 视角）。

## 6. 输出解读要点

- **批处理输出格式（实测，arthas 4.3.5）**：非 TTY 下 arthas 自动对命令输出追加 `| plaintext`，业务内容无 ANSI 色码、可直接 grep/解析；仅 banner/INFO 日志段可能含色码
- **thread -n 5**：先输出线程计数摘要（Total/Runnable/Blocked/Waiting…），再输出高 CPU 线程表；表列含 ID、名称、优先级、状态、`%CPU`、累计时间；命中的高 CPU 线程附完整栈帧——栈顶业务帧即 CPU 消耗点
- **trace**：树形输出，每段 `` `---[占比% 耗时ms] 类:方法() ``（arthas 4.3.5 实测用冒号分隔）；首行为该次调用元信息（时间戳/线程名）；配合 `#cost >` 过滤后只保留慢调用。实测样例（`trace DemoApp handle '#cost > 10' -n 2`）：
  ```
  `---ts=...;thread_name=api-worker;...
      `---[160.39ms] DemoApp:handle()
          +---[25.04% 40.16ms ] DemoApp:parse()
          `---[74.88% 120.10ms] DemoApp:io()
  ```
  `[74.88% 120.10ms] DemoApp:io()` 即最耗时子调用证据；达 `-n` 次数后自动退出并提示
- **watch**：按 `-x` 深度展开 params/returnObj/throwExp；关注异常对象与超预期的大对象/集合
- **monitor**：每窗口一行：时间戳、类、方法、调用总数、成功/失败、平均 RT、失败率——用于量化接口劣化与抖动
- **dashboard**：GC 行的 collectionCount/collectionTime 增速快 = GC 压力；Memory 行 old 区 used 持续上涨且 forceGc 后不回落 = 泄漏疑似

## 7. 场景判定与出界路由

| 场景 | 判定信号 | 入口命令 | 出界路由 |
|------|---------|---------|---------|
| CPU 高 | OS/dashboard 层 CPU 高且为 Java 进程 | `thread -n 5` | native（JNI/GC/JIT 线程）→ kptools-devkit tuner hotspot；代码热点 → code-optimizer；需全景 → java-perf |
| 接口慢（RT 高） | 单方法 RT 高于预期或同类基线 | `trace '#cost > N' -n 5` | 源码热点 → code-optimizer；伴随系统资源瓶颈 → kptools 系采集器 + kpbot-app-tuner |
| 线程阻塞/锁竞争 | CPU 不高但吞吐低、大量 BLOCKED | `thread --state BLOCKED` → `thread -b` | 锁设计（临界区过大/持锁做 IO）→ code-optimizer；JVM 参数 → kpbot-app-tuner |
| GC 频繁/内存压力 | RT 毛刺伴 GC、老年代锯齿变密或上涨 | `dashboard` + `memory` | GC/JVM 参数 → kpbot-app-tuner；分配热点/缓存设计 → code-optimizer；系统内存/带宽 → kptools-nmon / devkit memory |
| JVM 运行时参数 | 调优前确认实际生效值（配置文件≠运行值） | `vmopt` / `sysprop` | 参数调整 → kpbot-app-tuner 对应 subskill |

### 7.1 接口慢三步法（耗时分解 → 数据观察 → 量化统计）

```bash
bash scripts/collect.sh -p <PID> -c "trace com.foo.ApiImpl handle '#cost > 100' -n 5"
bash scripts/collect.sh -p <PID> -c "trace com.foo.ApiImpl slowSeg '#cost > 50' -n 5; watch com.foo.ApiImpl handle '{params,returnObj}' -x 2 -n 3"
bash scripts/collect.sh -p <PID> -c "monitor -c 10 com.foo.ApiImpl handle" --shutdown
```

trace 树占比最高的段为瓶颈方向；段均无热点但整体慢 → 转线程阻塞/锁等待；特定入参（大集合/极端值）才慢 → 收集证据交业务。

### 7.2 GC/内存：先判别"泄漏 vs 高分配"再深挖

```bash
bash scripts/collect.sh -p <PID> -c "dashboard -n 3 -i 2000; memory; perfcounter"
bash scripts/collect.sh -p <PID> -c "vmtool --action forceGc; memory"   # 前后对比老年代
# 回落明显（高分配）→ 分配火焰图定位分配点；不回落且 getInstances 持续增长（泄漏疑似）→ 确认后 heapdump --live 取证
bash scripts/collect.sh -p <PID> -c "profiler start --event alloc"
# ... 负载运行 20-30s ...
bash scripts/collect.sh -p <PID> -c "profiler stop --file /tmp/alloc.html" --shutdown
```

## 8. 生产安全纪律

1. 采集前置：确认目标进程是 Java 进程（语言路由），确认同 uid
2. 增强类命令（trace/watch/stack/monitor/tt）：必须带 `-n` 限制次数、类与方法精确匹配、一次只分析一个方法
3. 采集后：执行 `shutdown` 释放 arthas 服务端（collect.sh 的 `--shutdown` 选项）
4. `options unsafe true` 只在必要时临时开启，用完 `options unsafe false` 恢复
5. `heapdump --live` / `vmtool --action forceGc` 属重操作，须与业务负责人确认窗口

## 9. 常见错误对照

| 报错 | 原因与处置 |
|------|-----------|
| `Can not find java process` | PID 错误或进程已退出；`jps -l` 核对 |
| `Operation not permitted` | attach 用户与目标 JVM uid 不同；同 uid 或 root |
| trace/watch 无输出 | 负载未触发该方法（先 `monitor -c 10` 确认有调用量）；类名/方法名拼写错误（`sc -d` 核对） |
| telnet 3658 / http 8563 占用 | 其他进程的 arthas 服务端驻留：collect.sh 自动探测并换随机端口（stderr NOTICE）；直接 attach 时 `--telnet-port <n> --http-port -1` 规避，或对驻留会话执行 `shutdown` 释放 |
| attach 长时间无响应 | 首次运行在下载完整包（无网环境会卡住直至超时）→ 用 §2.3 预热；或端口驻留冲突 |
| `Can not retransform class`（执行 redefine 类命令时） | 属热更新能力，本 Skill 不使用；确认命令未误用 |
