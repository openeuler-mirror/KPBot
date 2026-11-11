---
name: hot-library-version-gap
description: >
  工作场景：perf 热点落在系统库（libcrypto/libz 等）函数里时，先核对进程实际加载的库
  与版本是否落后于已知包含优化的版本，排除"优化已存在、只是没用上"，再决定动手优化
  还是升级验证。只读诊断：解析 /proc/<pid>/maps 或离线 maps 文件确认实际加载对象，
  通过 soname/realpath 链提取版本，对照带出处的版本证据表输出差距结论。
  不适用：换一个不同的库（用 performance-library-selection）；执行升级动作；
  承诺升级收益（需另跑 A/B）。
---

# 热点库版本差距核对

热点在 `libcrypto.so.3!gcm_init_avx` 这类系统库函数时，动手优化源码之前必须先回答
一个问题：**这个优化是不是新版本已经有了，只是机器上装的版本太旧？** 答错这个问题，
后续就是"重新发明轮子"式的白费功夫。

本 skill 是只读诊断流程：热点符号 → 进程实际加载的库 → 版本提取 → 证据表匹配 →
差距结论（或 unknown）。**发现旧版本不等于已经获得性能提升**——升级收益需要另行
A/B 验证，本 skill 不执行任何变更。

## 与 performance-library-selection 的边界

| 问题 | 谁回答 |
|---|---|
| 换一个不同的库（glibc malloc → jemalloc） | `performance-library-selection`（app-tuner） |
| 还是这个库，但当前版本太旧（libcrypto 3.1.4 → 3.3+ 有优化） | **本 skill** |

互补衔接：该 subskill 已包含 `/proc/<pid>/maps` 采集动作，本 skill 可复用其采集结果；
但注意它的推荐逻辑在"检测到已链接某库"时会直接跳到运行时调参，**不检查该库自身版本
是否过时**——这正是本 skill 的主场场景。

## 流程

### 1. 定位热点符号与 DSO

```bash
# 从现有 perf 数据提取热点 DSO 排名（不重复采样）
perf report --stdio -i "$PERF_DATA" --no-children --sort=dso | head -20
```

记录：热点函数名、所在 .so 名。热点不在库函数里则本 skill 不适用。

### 2. 确认进程实际加载的库

```bash
# 在线：读目标进程 maps
cat /proc/<PID>/maps | grep '\.so' | awk '{print $6}' | sort -u

# 或使用本 skill 脚本（支持 --pid 在线 / --maps 离线两种输入）
python3 scripts/check_library_version.py --pid <PID> --lib libcrypto
```

**纪律：不能拿全局命令版本冒充进程加载版本**——`openssl version` 报的是 PATH 里
那个二进制；进程可能通过 `LD_LIBRARY_PATH`、容器、静态链接或自定义前缀加载了完全
不同的另一份库。结论必须来自 `/proc/<PID>/maps`（或等价的 dladdr 证据）。

### 3. 提取实际加载对象的版本

**脚本提取（`check_library_version.py` 自动）**：realpath 链——maps 里的
`libcrypto.so.3` 解析真实文件名（如 `libcrypto.so.3.1.4`），trailing 版本号是
最常见载体；文件名不带可解析版本段（含 rc 等非纯数字后缀）时如实标
`unknown`，不猜。

**agent 补充动作（脚本外，可选）**：soname 只有主版本时（脚本输出
`insufficient-version-detail`），用包管理器反查细版本：
`rpm -qf <realpath>` / `dpkg -S <realpath>`——注意这是**发行版包版本**，
与上游版本可能因 backport 而脱钩；包管理器也失败则保持 unknown。

### 4. 匹配版本证据表

表在 `references/library-versions.json`（含 schema 说明）。每条证据必须满足：

- `min_version`：优化引入的上游版本；
- `source`：可公开访问的上游文档出处（CHANGES/NEWS/ChangeLog/commit）；
- `arch_scope`：适用架构范围。

**表未命中 ≠ 无优化**。表的正确语义是"已核实的快照"，不是全集。未命中时进入
上游检索方法论（下一节），仍找不到才输出 `unknown`。

**匹配语义**：`match` token 是库名前缀——脚本判定加载对象的 basename 是否
以某 token **开头**（`libcrypto.so.3` 匹配 token `libcrypto.so`；`libzip.so.2`
不匹配 token `libz.so`）。自造表时 token 写完整库名前缀，不要写裸子串。

### 5. 输出结论

```markdown
### 版本差距核对：<库名>

- 实际加载：<realpath>（maps 确认，非全局命令）
- 当前版本：<提取结果 or unknown + 失败原因>
- 已知优化：<表条目 min_version + improvement + source 链接>
- 结论：<存在差距：先升级验证，再评估是否值得自行优化 | 无差距/unknown>
- 提醒：本结论不包含升级收益；升级前后需另跑 A/B
```

## 表未命中时的上游检索方法论

证据表永远是快照，热点库未命中时按以下顺序找证据，**找到必须带链接回来**：

1. 上游仓库符号考古：`git log -S "<热点符号>" --oneline`——哪个 commit 引入/重写了它；
2. 上游 CHANGES / NEWS / ChangeLog 中搜该符号或功能名；
3. 上游 release note / 公开 benchmark 公告；
4. 发行版包 changelog（确认是否 backport）。

检索失败、证据不可公开验证、或版本语义不明（backport 情况）→ 输出 `unknown`，
**不编造"应该有优化"**。

## ARM backport 陷阱

- 上游版本号 ≥ min_version ≠ 已含优化（发行版可能没 backport）；
- 上游版本号 < min_version ≠ 没有优化（openEuler 等发行版常把补丁 backport 进旧包，
  版本号不变）；
- 包版本与上游版本语义脱钩时一律标 `unknown`，除非拿到发行版包 changelog 的直接证据。

## 脚本用法（scripts/check_library_version.py）

```sh
# 在线：检查目标进程加载的 libcrypto 及版本（inode 核验默认开启——同源文件系统视图）
python3 scripts/check_library_version.py --pid <PID> --lib libcrypto

# 离线：用导出的 maps 文件 + 一个用于解析 .so realpath 的根目录
# （inode 比对始终执行并输出三态：disk(verified) / disk(unverified)
#   +version_confidence=low +重判提示 / 树中缺文件时 mapped-name+stale。
#   跨机拷贝的树 inode 必然失配——此时只降置信度、不降级判定）
python3 scripts/check_library_version.py --maps proc_maps.txt --root /path/to/fsroot

# 同机分析（同一 inode 空间）：显式开启失配降级——原地升级会被判 stale
python3 scripts/check_library_version.py --maps proc_maps.txt --root / --verify-inode

# 自定义证据表 / 落盘输出
python3 scripts/check_library_version.py --maps m.txt --root r/ \
    --knowledge references/library-versions.json --output verdict.json
```

- 只读：不安装、不升级、不注入任何环境；
- 输出 JSON：每库 {soname, realpath, version, version_method, knowledge_match}，
  `knowledge_match` 含 gap 结论与 source 或 `unknown`；
- 大小写敏感的库名过滤用 `--lib`（可多次）。

## Anti-patterns

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | 拿全局命令版本冒充进程加载版本 | 一律 /proc/PID/maps 或 dladdr |
| 2 | 编造"新版本应该有优化" | 证据表或上游检索，找不到就 unknown |
| 3 | 发现旧版本就宣称性能提升 | 只报差距；收益需升级后 A/B |
| 4 | 版本号满足就认定含优化 | backport 陷阱：包 changelog 直接证据优先 |
| 5 | 把本 skill 当升级工具执行 | 只读诊断，变更走候选动作由用户/主框架落地 |
| 6 | 热点在自有代码里也跑这套流程 | 本 skill 仅适用热点在库函数 |

## References

- [library-versions.json](references/library-versions.json) — 版本证据表（初始快照，
  含证据政策与 schema 说明）；扩充新条目必须附可公开访问的出处
