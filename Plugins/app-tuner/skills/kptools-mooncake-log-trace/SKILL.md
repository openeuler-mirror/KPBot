---
name: kptools-mooncake-log-trace
description: >
  把已编译好的带 MKT 打点 Mooncake .so 部署替换到远端 vLLM 容器并校验生效：产物预检
  （MKT 串 + md5）→ 定位容器内现役 .so → 备份 → docker cp 替换 → md5 回读 → 重启指引 →
  smoke 验证 MKT 成对输出（D 侧 [MKT_TTF]/[MKT_FINE]，P 侧 [MKT_PUT]/[MKT_PFINE]）→ 回滚。当用户提到以下场景时必须使用此技能：
  部署 mooncake .so、替换 mooncake 动态库、mooncake 打点推上去、给 P/D 容器换 mooncake 库、
  mooncake 打点部署后不生效/没有 MKT 输出排查、mooncake log trace。即使用户只说
  "把带打点的 .so 换上去"或"MKT 日志怎么没有输出"，也应使用此技能。只做部署与生效校验；
  源码打点与编译归 kptools-mooncake-instrumentation，日志解析出表归
  kptools-tpsplit-log-analysis。
license: Proprietary
compatibility: 本地 Windows（写出的 .sh 需去 CR）；SSH 访问 P/D 宿主机（使用调用方已授权的目标连接，认证失败即停止）；远端 docker（docker cp 替换，stopped 容器同样有效）；输入产物须为已验证打点 .so（strings 含 MKT 串 ≥2 + md5 清单，来自 kptools-mooncake-instrumentation）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: mooncake_log_instrumentation
  version: "0.2.0"
---

# kptools-mooncake-log-trace：Mooncake 打点 .so 部署（备份/替换/校验/回滚）

把 `kptools-mooncake-instrumentation` 编译产出并验证过的打点 .so 部署替换到远端容器，校验生效。**只做部署，不打点不编译不解析**——源码插桩与编译归 `kptools-mooncake-instrumentation`，日志解析出表归 `kptools-tpsplit-log-analysis`（后续扩展 MKT 解析）。

与姊妹技能的分工：`kptools-tpsplit-log-trace` 部署 vLLM/vLLM-Ascend 的 **Python 层** TTFT 打点（docker cp .py 即生效）；本技能处理 **C++ 层** Mooncake 库（必须先由 instrumentation skill 编译成 .so，再走本 skill 替换）。

## 快速决策

1. 用户要部署打点 .so（默认场景，产物已就绪）→ 走完整流程：产物预检 → 定位 → 部署替换 → 用户重启 + smoke
2. 用户还没有带打点的 .so（要先打点/编译）→ 转 `kptools-mooncake-instrumentation`
3. 产物没过 MKT 串预检（strings 无 MKT_TTF/MKT_FINE）→ **拒收**，退回 instrumentation skill，禁止部署未验证产物
4. 部署后日志无 MKT 输出 → 按第 6 节排查（进程没换库 / glog 级别 / Ray 去重 / 无流量）
5. 多容器部署（如 D 侧多引擎）→ 同一份产物逐容器执行部署，每容器独立备份与校验，**不重新编译**
6. 用户要解析 MKT 日志出表 → 转 `kptools-tpsplit-log-analysis`

## 1. 工作流程总览

```
接收已验证打点产物（.so 集合 + md5 清单 + MKT 串校验确认）
  → 定位容器内现役目标 .so（同名匹配 / /proc/<pid>/maps 权威）
  → 备份（mktemp 私有目录 + MANIFEST）→ docker cp 替换 → md5 回读校验
  → 用户重启引擎 → smoke：发真实请求，D 侧 grep [MKT_TTF]/[MKT_FINE]、P 侧 grep [MKT_PUT]/[MKT_PFINE] 成对输出
  → 异常回滚（按 MANIFEST 恢复）
```

## 2. 产物接收与预检（跨 skill 契约）

来自 `kptools-mooncake-instrumentation` 的交付物：打点 .so 集合（文件名与容器现役一致）+ 逐文件 md5 清单 + strings MKT 串校验确认。

本 skill 部署前独立复核（不信任口头"已验证"）：

1. `strings <每个.so> | grep -c "MKT_TTF\|MKT_FINE"` ≥2，不满足即拒收；
2. 本地 md5 与清单一致；
3. 文件名与容器内现役 .so 对得上（对不上先回探测，见第 3 节）。

## 3. 目标 .so 的确定

以**运行中引擎进程实际 mmap 的库**为准（`/proc/<pid>/maps`），不是"看起来像"的那份：

- Python 包目录里可能有多份 .so（缓存、多版本共存），替换错文件 = 白干一次；
- mooncake 客户端可能拆多个 .so（client + transfer engine 依赖），产物清单里的全换；
- `scripts/deploy_mooncake_so.sh` 默认按文件名自动匹配容器内目标：**同名多匹配或零匹配即停**，用 `--pairs` 显式指定容器内路径。

## 4. 部署替换

宿主机上运行 `bash scripts/deploy_mooncake_so.sh <容器名> <新.so目录>`（或 `--pairs <映射文件>` 显式指定）。脚本内置纪律：

- CRLF 自检（Windows 写出的 .sh 带 CR 会直接拒跑）；
- 部署前 MKT 串预检（先校验后写入）；
- `mktemp` 私有备份目录 + MANIFEST（备份文件 ↔ 容器路径 ↔ 原 md5）+ 备份回读校验；
- `docker cp` 替换（对 stopped 容器同样有效，`docker exec` 写文件对 stopped 容器无效）；
- 容器内 md5 回读比对，不符即报错停止；
- **不自动重启**：.so 替换对运行中进程无效（旧库已 mmap），必须由用户重启引擎后新库才被加载。

多容器场景：同一产物逐容器执行，每容器独立备份目录与 MANIFEST；不重新编译。

## 5. 重启与 smoke

1. 重启顺序参考 kptools-tpsplit-log-trace 第 3 节第 7 步（P 起 mooncake_master → P 引擎 → D 引擎 → proxy）；
2. 重启后先核进程加载：`grep mooncake /proc/<新pid>/maps`，md5 应等于新 .so；
3. 发 1 条真实请求，引擎日志应出现成对 MKT 输出：D 侧 `grep -E "\[MKT_TTF\]|\[MKT_FINE\]"`，P 侧 `grep -E "\[MKT_PUT\]|\[MKT_PFINE\]"`；
4. 核对一致性（同批 keys 相同、get 链 FINE 五段和 ≈ getmem_ms、put 链 PFINE 各相和 ≈ put_ms，契约见 kptools-mooncake-instrumentation 的打点规格第 6 节），不符说明打点挂错阶段，退回 instrumentation skill。

## 6. 部署后无 MKT 输出的排查

1. **进程没换库**：重启后 `/proc/<新pid>/maps | grep mooncake` 应指向新 .so（md5 比对）；仍加载旧库 = 替换路径不对或有多副本 .so（以 maps 为准，不是 site-packages 里的那份也不奇怪）。
2. **glog 级别**：MKT 打点走 `LOG(INFO)`。mooncake 若被配置成只输出 WARNING+（GLOG_minloglevel / mooncake 日志配置），INFO 会被吞；确认引擎日志级别设置。
3. **Ray 日志去重**：worker 进程输出经 Ray 汇总，需 `RAY_DEDUP_LOGS=0` 才能看到 worker 侧 C++ 日志（同 kptools-tpsplit-log-trace 第 4 节）。
4. **流量没走到**：没发请求或该批次全 miss（无 KV 传输）自然无日志；smoke 必须发真实请求。
5. **产物本身没打点**：手撸 docker cp 跳过预检时，`strings <替换的.so> | grep -c MKT` 复核（编了旧缓存/旧目录）。

## 7. 回滚

```bash
bash scripts/deploy_mooncake_so.sh --rollback <备份目录> <容器名>
```

按 MANIFEST 逐条恢复并 md5 回读校验，再由用户重启引擎；回滚后同样要 smoke（MKT 应消失、引擎正常服务）。

## 8. 使用边界与踩坑

- **产物来源唯一**：只部署 `kptools-mooncake-instrumentation` 交付的已验证产物；任何"现场随便编一个"的 .so 一律退回走 instrumentation skill 的版本锚定与编译流程。
- **先校验后写入**：预检必须在任何 docker cp 之前完成——先覆盖后校验会把被拒绝的内容留在目标目录。
- **多 .so 一起换**：产物清单内的全部替换，md5 全量登记与回读。
- **部署 ≠ 运行**：部署完必须重启 + smoke 才算闭环。
- **Windows 写出的 .sh 带 CR**：远端先 `tr -d '\r' < f > f2`（脚本也有自检兜底）。
- 详细操作与坑表见 [.so 部署替换与回滚](references/deploy.md)。
