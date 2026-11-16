---
name: kptools-arthas
description: >
  使用 Arthas 对运行中的 Java 进程做免重启只读性能分析：方法级调用链耗时分解（trace）、
  入参/返回值/异常观察（watch）、调用来源栈（stack）、线程 CPU 与阻塞/锁竞争分析（thread）、
  持锁者识别（thread -b）、QPS/RT/失败率统计（monitor）、时间隧道录制查询（tt）、
  内存与 GC 状态（memory/dashboard）、JVM 运行时性能参数（sysprop/vmopt）、ognl 运行时查询。
  当用户需要分析 Java 进程接口慢、CPU 高、线程阻塞、锁竞争、吞吐上不去、GC 频繁、
  内存压力、疑似内存泄漏，或提到 arthas、trace/watch/tt/monitor 做在线 Java 方法级
  性能分析时触发。Java 采样火焰图 → kptools-devkit java-perf。
license: Apache-2.0
compatibility: Linux x86_64/aarch64, JDK 8-21（JDK 8 自动适配 tools.jar），attach 需与目标 JVM 同 uid
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: java_performance_analysis
  version: "1.1.0"
---

# kptools-arthas — Java 进程性能分析（Arthas）

## 职责边界

本 Skill 只做**只读性能分析**：attach 运行中的 JVM 采集性能证据、量化瓶颈，不修改目标进程状态。

- Java 采样火焰图（CPU/ALLOC/LOCK 热点全景）→ kptools-devkit java-perf（aarch64 优先）；本 Skill 的 `profiler` 命令可作替代
- 分析出的 GC/JVM 参数问题 → 证据移交 kpbot-app-tuner 对应 subskill 实施调优
- 分析出的业务代码热点 → 源码移交 code-optimizer
- `redefine`/`retransform` 热更新、`logger --level` 等运行时改写**不在本 Skill 范围**（生产风险）
- Python → kptools-py-spy；Node.js → kptools-node-profiler；C/C++ → kptools-devkit tuner

## 工作流程

1. 检查环境：`bash scripts/discover.sh`（java、目标 PID、arthas-boot.jar 部署状态）
2. 未部署时安装：`bash scripts/deploy.sh`（在线下载 arthas-boot.jar；离线环境见 arthas-guide §2）
3. **语言路由（必做）**：目标进程须为 JVM——`tr '\0' ' ' < /proc/PID/cmdline | grep -qiE 'java'`；非 Java 进程按职责边界路由
4. 按性能场景选择分析入口（下表），用批处理模式一次性采集证据（无需交互）
5. 解读输出量化瓶颈，按职责边界移交后续优化；采集完建议 `shutdown` 释放会话

## 性能场景速查

| 场景 | 第一步 | 深挖 |
|------|--------|------|
| CPU 高 | `thread -n 5` | `thread <tid>` 看栈 → `stack 类 方法` 找业务入口 → `trace 类 方法 '#cost > 50'` 看耗时分布 |
| 接口慢 | `trace com.foo.ApiImpl handle '#cost > 100' -n 5` | 最慢子调用逐层下钻；`watch 类 方法 '{params,returnObj}' -x 2 -n 3` 看数据；`monitor -c 10 类 方法` 看统计 |
| 线程阻塞/锁竞争 | `thread --state BLOCKED` | `thread -b` 找持锁者 → `stack` 看持锁线程在执行什么 |
| GC 频繁/内存压力 | `dashboard -n 1`（GC 次数/耗时）+ `memory` | `vmtool --action forceGc` 前后对比老年代 → `profiler start --event alloc` 分配火焰图；疑泄漏才 `heapdump --live`（STW，慎用） |
| JVM 运行时参数 | `vmopt` / `sysprop` | `getstatic 类 字段` / `ognl` 查询运行值（核对 GC 器、堆配置等生效值） |

场景判定信号、典型采集链与出界路由见 [arthas-guide](references/arthas-guide.md) §7。

## 批处理采集（agent 必用，非交互）

```bash
# 命令串按 ';' 拆分逐条 attach，输出带 === CMD === 分节标记写入证据文件
bash scripts/collect.sh -p <PID> -c "thread -n 5; thread --state BLOCKED" -o ./arthas-evidence/

# 预设场景
bash scripts/collect.sh -p <PID> --preset cpu        # dashboard + top5 CPU 线程
bash scripts/collect.sh -p <PID> --preset blocked     # BLOCKED 线程 + 持锁者
bash scripts/collect.sh -p <PID> --preset overview    # 总览 + 内存 + JVM 参数

# 采完 best-effort 释放 arthas 服务端（追加 shutdown，失败不计）
bash scripts/collect.sh -p <PID> --preset cpu --shutdown
```

## 输出

- stdout: 证据文件路径
- stderr: 语言路由告警、uid 不匹配告警、单条命令失败清单
- 退出码: 0 成功（部分命令失败仍为 0，失败清单见 stderr）；2 依赖缺失（java / arthas-boot.jar）；3 参数错误；1 全部命令失败

## collect.sh 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标 Java 进程 | 必填 |
| `-c "<cmds>"` | 命令串，`;` 分隔多条 | 与 --preset 二选一 |
| `--preset <name>` | cpu / blocked / overview | - |
| `-o <dir>` | 证据目录 | `./arthas-evidence/<时间戳>/` |
| `--jar <path>` | arthas-boot.jar 路径 | 搜索 `$ARTHAS_BOOT_JAR` → `~/.arthas/` → `/opt/arthas/` |
| `--timeout <sec>` | 单条命令 attach 超时 | 120 |
| `--telnet-port <n>` | arthas telnet 端口 | 3658；被其他进程的 arthas 会话占用时自动换随机端口（stderr NOTICE） |
| `--shutdown` | 结束后 best-effort 执行 `shutdown` 释放 | 关闭 |

## 约束

- attach 权限：须与目标 JVM 同 uid（或 root）；容器内须进入目标进程同一容器（`docker exec`/`kubectl exec`/`nsenter`），不支持跨容器 attach
- trace/watch/monitor/tt 基于字节码增强，有观测开销：生产必须 `-n` 限次数、精确匹配类/方法、短窗口；采完 `shutdown`
- `-c` 按 `;` 拆分逐条 attach，单条命令内不要使用含 `;` 的 OGNL 表达式（拆成多次采集）
- `heapdump --live` 触发 Full GC + STW，仅内存分析取证时使用；dump 文件与堆同量级
- `vmtool`/部分 ognl 查询需 `options unsafe true`，临时开启、用完恢复
- JDK 8 的 tools.jar 由 arthas-boot 自动适配；JDK 8-21 为验证范围，更老 JDK 不承诺
- arthas `profiler`（内嵌 async-profiler）aarch64 实测可用（CPU 事件，arthas 4.3.5 + JDK 17 验证）；ALLOC 事件与鲲鹏全景对比仍优先 kptools-devkit java-perf

## 错误处理

- `Can not find java process` → 确认 PID（`jps -l` 或 `ps -ef | grep java`）
- attach 报 `Operation not permitted` → 与目标 JVM 同 uid 或 root 执行
- trace/watch 无输出 → 负载未触发该方法，或类名/方法名写错：`sc -d 'com.foo.*'` 确认类名（通配符须引号包裹）
- telnet 3658 / http 8563 端口被占 → 其他进程的 arthas 会话驻留：collect.sh 自动探测并换随机端口；直接 attach 时用 `--telnet-port <n> --http-port -1` 规避，或对驻留会话执行 `shutdown` 释放
- attach 卡住不退出 → collect.sh 已有 `--timeout`（默认 120s）兜底；频繁超时先检查端口驻留

## 参考

- [Arthas 使用指南](references/arthas-guide.md) — 安装/离线、attach 机制、命令全景、输出解读、场景判定与出界路由
- [与 java-perf/py-spy 分工](references/arthas-vs-java-perf.md) — 何时用哪个工具

## 安全约束

当你在容器化 A/B 用例（agent tier）中执行时，必须遵守以下硬性边界：

- 禁止读取凭证文件以及 /workspace 之外的任何文件（目标进程的确认与语言路由由 collect.sh 内部完成，无需自行读取 /proc）。
- 禁止任何网络访问（模型 API 已由运行环境代为调用）。
- 分析证据与产物只允许写入 /workspace 下的目录（如 ./arthas-evidence/）。
