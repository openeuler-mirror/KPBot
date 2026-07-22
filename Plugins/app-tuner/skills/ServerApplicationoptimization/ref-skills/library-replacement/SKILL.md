---
name: library-replacement
description: >
  鲲鹏/ARM（aarch64）服务器上的 Linux 库替换性能调优体检工具。采集目标进程的动态库依赖
  （lsof/proc maps）与 perf 热点，对照本地 optimization_kb.json 知识库识别当前使用的库并
  推荐更优的鲲鹏优化替换库（如 jemalloc/tcmalloc、rocksdb、libmem、sonic-cpp、ISA-L 等），
  输出调优建议报告。支持在线（进程已运行，提供 PID）与离线（提供启动命令）两种模式，仅提供
  方案不执行变更。当用户在鲲鹏/ARM 环境下提到库替换、性能调优体检、perf/CPU 热点分析、
  LD_PRELOAD、allocator/哈希/压缩/加密/JSON/KV 存储库优化、benchmark 调优等场景时主动使用本技能。
triggers:
  - "性能调优"
  - "库替换调优"
  - "CPU 热点分析"
  - "LD_PRELOAD"
  - "NoSQL 调优"
  - "allocator 调优"
  - "哈希函数优化"
  - "压缩库优化"
  - "加密库调优"
  - "JSON 解析优化"
  - "数学库优化"
  - "系统瓶颈体检"
  - "benchmark 调优"
  - "perf 分析"
  - "jemalloc"
  - "tcmalloc"
  - "zlib 替换"
  - "openssl 优化"
  - "sonic-json"
  - "Hyperscan"
  - "xxhash"
  - "RocksDB"
  - "RocksDB 状态后端"
  - "Flink RocksDB"
  - "LSM 调优"
  - "嵌入式 KV"
  - "键值存储优化"
  - "performance tuning"
  - "library replacement"
  - "调优体检"
  - "分析一下当前运行的程序"
  - "perf 火焰图"
---

# library-replacement

非侵入式 Linux 库替换调优体检：采集进程的库依赖与 perf 热点，对照 `optimization_kb.json` 推荐更优替换库。**仅提供方案，不执行变更。**

## 红线（必须遵守）

1. **只读探测**：仅用只读命令（`lsof`/`ps`/`perf`/`readelf`/`/proc` 等），禁止写入、环境变量注入、kill、重启。
2. **无状态脚本依赖**：脚本间不得传递活动进程 PID 等非文本状态；需观测进程时必须在单脚本内闭环完成"启动→采样→收集"，仅以文本/JSON 交互。
3. **仅支持 aarch64**：`uname -m` 非 aarch64 则终止。
4. **数据不得编造**：所有输出必须来自知识库或现场采集。

## 知识库

- 路径：依次查找 `./optimization_kb.json`、`./docs/optimization_kb.json`、`../optimization_kb.json`；缺失则终止。
- 结构：`library_profiles.<category>.<lib>.{best_scenarios, strengths, verification_steps}`。类别与库清单直接读 KB，不在此枚举。

## 脚本契约

三个项目脚本，I/O 如下（签名无法自行推断）：

| 脚本 | 输入 | 输出 |
|------|------|------|
| `scripts/sample_online_pid.sh` | `<PID>`（在线，进程已运行） | 整合 JSON 报告文件绝对路径 |
| `scripts/run_and_profile_offline.sh` | `<command> <args...>`（离线，仅执行用户显式提供的命令） | 整合 JSON 报告文件绝对路径 |
| `scripts/detect_all_libraries.sh` | `<报告文件路径>` | `{detected_libraries:[{category,current_lib,detection_method,evidence}]}` |

整合 JSON 同构字段：`{process_found, ps_info, libraries:[静态依赖路径], perf_available, hotspots:[{overhead,lib,symbol}], perf_error?, needs_authorization?, auth_command?}`。检测脚本对整份报告 grep 匹配，静态/动态任一命中即识别该库。

## 非显然约束（踩坑所得，勿违背）

1. **JNI 库懒加载**：`librocksdbjni.so` 等由 JVM 运行时懒加载，单次早期 `lsof` 会漏掉。离线脚本已在采样窗口内轮询 `lsof` + `/proc/<PID>/maps` 取并集来捕获；勿回退为单次采样。在线模式采样已运行进程则无此问题。
2. **静态 + 动态都要进检测**：`lsof`/`maps` 静态依赖与 perf 动态热点必须都喂给检测脚本；perf 无权限时静态结果兜底识别。
3. **perf 采应用 PID 本身**：用 `perf record -p <PID>`，勿用 `perf record -- <CMD>`（后者使应用成为 perf 的孙进程，`lsof` 采到的是 perf 的库）。
4. **使能方式非统一**：多数库 `LD_PRELOAD` 即可；JAR 内嵌 JNI 库（`librocksdbjni.so`）`LD_PRELOAD` 无效，需替换 JAR 内动态库。具体命令查各库 `verification_steps`。
5. **perf 权限不足**：报告 JSON 会标记 `needs_authorization: true`，与用户交互请求授权（`perf_event_paranoid` 或 `/proc` 权限）后重跑；拒绝则仅静态分析。

## 流程

确认模式（在线给 PID / 离线给命令）→ 采集环境指纹（`uname`/`lscpu`，校验 aarch64）→ 跑对应 orchestrator 得整合 JSON → `detect_all_libraries.sh` 识别 → 对照 KB `library_profiles` 推荐同类别更优库（目标库无需已安装，使能方式见 `verification_steps`）→ 输出报告。

## 输出

Markdown 体检报告，含：硬件指纹、分析模式与目标、采样结果（依赖库 + perf 热点 Top）、识别到的库、替换推荐（当前库→目标库，附 `best_scenarios`/`strengths`）、实施 SOP（引用 `verification_steps`）。所有数据来自现场采集或知识库，不得编造。
