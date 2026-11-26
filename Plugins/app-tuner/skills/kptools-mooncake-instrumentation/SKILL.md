---
name: kptools-mooncake-instrumentation
description: >
  给 Mooncake KV 传输层（mooncake-store 客户端 C++ 源码）打点并在目标容器内编译成 .so：
  探测容器现役版本 → 按版本拉取源码 → 应用 [MKT_TTF]/[MKT_FINE]（get 链）与
  [MKT_PUT]/[MKT_PFINE]（put 链）传输耗时打点 → 容器内编译 →
  产物校验（strings 验 MKT 串 + md5 清单）。当用户提到以下场景时必须使用此技能：
  mooncake 打点、MKT_TTF、MKT_FINE、MKT_PUT、MKT_PFINE、mooncake 源码插桩/埋点、
  给 mooncake 加日志、编译 mooncake .so、mooncake 版本探测/锚定、KV 传输耗时要看
  C++ 层细分（put 存 KV 或 get 拉 KV）、mooncake instrumentation。即使用户只说
  "给 mooncake 加个打点"或"编个带日志的 mooncake"，也应使用此技能。只做打点+编译，
  产物交付给 kptools-mooncake-log-trace 部署；.so 替换部署与日志解析出表不归本 skill。
license: Proprietary
compatibility: 本地 Windows（写出的 .sh 需去 CR）；SSH 访问目标宿主机（使用调用方已授权的目标连接，认证失败即停止）；目标容器内需 git/cmake/g++ 与 mooncake 构建依赖（由探测步骤确认）；源码从 kvcache-ai/Mooncake（GitHub 失败切 Gitee 镜像）按现役版本拉取
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: mooncake_log_instrumentation
  version: "0.1.0"
---

# kptools-mooncake-instrumentation：Mooncake 源码打点 + 编译（C++ 插桩 → .so）

把 Mooncake 客户端 C++ 源码插入 MKT 传输耗时打点，在目标容器内编译成 .so 并校验产物。**只做打点+编译两件事，不部署**——产物（已验证打点 .so + md5 清单）交给 `kptools-mooncake-log-trace` 执行容器内替换。

打点覆盖两条链：**get 链（D 侧拉 KV）** `[MKT_TTF]` 批次摘要 + `[MKT_FINE]` getmem 内部五相；**put 链（P 侧存 KV）** `[MKT_PUT]` 批次摘要 + `[MKT_PFINE]` BatchPut 内部相（含 master 副本分配等 put 特有阶段）。

与姊妹技能的分工：
- `kptools-mooncake-log-trace`：接收本 skill 产物，做 .so 备份/替换/md5 回读/重启指引/smoke/回滚
- `kptools-tpsplit-log-analysis`：后续解析 MKT 日志出表（MKT 字段契约由本 skill 的打点规格定义）
- `kptools-tpsplit-log-trace`：vLLM/vLLM-Ascend Python 层 TTFT 打点部署（.py 文件级，无需编译）

本 skill 处理的是 Python 层打点看不到的盲区：vLLM 侧只能测到 `backend.put/get` 的 Python 主机侧总耗时，RDMA 传输内部阶段（open/build/alloc/submit/wait）只有 C++ 层打点才可见。

## 快速决策

1. 用户要给 Mooncake 加打点（默认场景）→ 走完整流程：探测 → 源码准备 → 打点 → 编译 → 产物校验 → 交接部署
2. 用户只问"容器里 mooncake 是什么版本/哪个 .so" → 只跑探测（`scripts/probe_mooncake.sh`）
3. 探测出的版本拉不到对应源码，或容器内缺构建工具链 → **停下问用户**，不要拿相近版本硬编（ABI 不匹配会运行时崩）
4. 编译产物已就绪、用户要替换容器里的 .so → 转 `kptools-mooncake-log-trace`
5. 用户要解析 MKT 日志出表 → 转 `kptools-tpsplit-log-analysis`

## 1. 工作流程总览

```
容器内探测现役 mooncake（版本 + .so 路径 + 工具链）
  → 按版本拉取源码（kvcache-ai/Mooncake 匹配 tag/commit）
  → 应用 MKT 打点（按 references/instrumentation.md，函数级挂点）
  → 容器内编译成 .so（references/build.md）
  → 产物校验（strings 验 MKT 串 ≥2 + md5 清单）
  → 交接：产物 + md5 清单交给 kptools-mooncake-log-trace 部署
```

## 2. 探测（第一步，必做）

在目标容器内运行 `scripts/probe_mooncake.sh`（经 `docker exec` 或 SSH）。产出：

- mooncake 相关 Python 包与版本（pip 元数据）、`import mooncake.*` 实际加载路径
- 全部 mooncake 相关 .so 及软链指向
- 运行中引擎进程实际 mmap 的 mooncake 库（`/proc/<pid>/maps`，最权威）
- .so 内嵌版本串、git 残留信息
- 构建工具链（git/cmake/g++）与 RDMA 依赖是否齐备
- 容器内是否残留 mooncake 源码/构建目录（有则优先复用，版本必然匹配）

**版本锚定规则**：以运行中进程 mmap 的 .so 为准（文件名、md5、路径）；pip 元数据仅作参考。探测拿不到确定版本（无版本串、无源码残留）即停下问用户，禁止按猜测版本编译。两处版本线索矛盾（如 pip 一个版本、.so 串另一个版本）说明可能内部 fork 或 .so 被覆盖过——停下向用户要源码来源，不硬猜。

## 3. 源码准备

1. 按探测版本拉取 `kvcache-ai/Mooncake`（GitHub 优先，连接重置时切 Gitee 镜像 `gitee.com/kvcache-ai/Mooncake`），checkout 到对应 tag/commit。
2. 容器内残留源码且 git 状态干净 → 直接复用（跳过网络拉取）。
3. 版本对不上（tag 缺失、内部 fork）→ 列出差异问用户要源码来源，不硬猜。

## 4. 打点（skill 核心资产）

按 [打点规格](references/instrumentation.md) 修改四处源码（`mooncake-store/src/`）：
- `real_client.cpp`：`RealClient::BatchGet` 收尾挂 **MKT_TTF**（get 链批次摘要：query/select/getmem/offload/checksum/diskio 六相）；批量 put 入口（`batch_put_from` / `batch_put_from_multi_buffers`，按实际入口，可双挂）挂 **MKT_PUT**（put 链批次摘要）
- `client_service.cpp`：内存路径批量传输函数挂 **MKT_FINE**（getmem 内部五相 open/build/alloc/submit/wait）；`Client::BatchPut`（put 单一收束点）挂 **MKT_PFINE**（含 StartBatchPut 的 master 副本分配等 put 特有相）

规格包含字段契约（字段名、单位、顺序必须与既有日志逐字段一致，下游 kptools-tpsplit-log-analysis 按此 grep）和函数级挂点定位方法（按调用链定位而非行号，行号随版本漂移）。put 链当前为设计格式串（v0.3.14 源码已核对调用链），首次部署后采集实测样例回填校准。

## 5. 编译与产物校验

- **编译**：在目标容器内按 [编译指南](references/build.md) 用 CMake 构建。**禁止在容器外/异版本环境编译后直塞**——ABI 与依赖必须与容器运行环境一致，否则替换后 Python 侧 import 或运行时崩溃。
- **产物校验**：`strings <目标.so> | grep -c "MKT_TTF\|MKT_FINE"` 应 ≥2；为 0 = 打点没编进目标库，**禁止交付**。全量记录产物 md5 清单。
- **禁止携带未编译验证的改动**：清单内每个文件的打点内容必须是容器内实际编译通过并过 strings 校验的版本；新改动必须先过"编译 + 校验"再进交付清单。

## 6. 交接（产物契约）

交付给 `kptools-mooncake-log-trace` 的产物契约：

1. 打点 .so 集合（文件名与容器现役 .so 一致）
2. md5 清单（逐文件）
3. strings MKT 串校验通过的确认（≥2）

交接时说明：.so 替换对运行中进程无效（已 mmap 旧库），部署方会提示用户重启；smoke 需发真实请求并 grep `[MKT_TTF]`/`[MKT_FINE]` 成对输出；MKT 走 `LOG(INFO)`，worker 侧输出需 `RAY_DEDUP_LOGS=0` 且确认 glog 级别（详见 kptools-mooncake-log-trace 的部署文档）。

## 7. 使用边界与踩坑

- **日志格式是硬契约**：字段名、顺序、单位逐字段对齐 [打点规格](references/instrumentation.md) 中的样例，下游 kptools-tpsplit-log-analysis 的 MKT 解析按此实现，改字段必须双方同步。
- **版本错配 = 运行时崩**：源码版本与现役 .so 不一致时，编译产物可能 import 失败或运行中崩溃；版本锚定拿不准即停。
- **打点计时纪律**：单调时钟（steady_clock）、各相只计自身区间、异常提前返回也要打出已耗相位（或标 partial）、`LOG(INFO)` + 固定前缀，详见打点规格第 7 节。
- **Windows 写出的 .sh 带 CR**：远端先 `tr -d '\r' < f > f2` 再执行。
- 多 .so 产物：mooncake 可能拆多个动态库（client + transfer engine 依赖），打点涉及的全量交付，md5 全登记。
