# 变体构建安全参考（远程树保护 / 链接指纹 / 环境坑 / 换库参数表）

库优化的构建环节有两类高频事故：**把目标源码树改坏没还原**（后续所有实验
基线污染）和**以为在测变体、实际链接的还是旧库**（数字全废）。本文档是
两者的防护清单。

## 1. 变体构建安全模板

远程树上的每次 patch 应用 + 构建都照抄此模式（bash）：

```bash
#!/bin/bash
ROOT=<远程工作根>; TREE=<目标库源码树>; cd "$TREE"
FILES="src/file1.cc src/file2.h ..."          # 本次 patch 触及的文件
trap 'for f in $FILES; do [ -f "$f.bak" ] && cp -f "$f.bak" "$f"; done' EXIT
# 前置校验：树必须处于 BASE 状态（marker = BASE 里没有、patch 会引入的特征串）
for f in $FILES; do grep -q "<marker>" "$f" && { echo "FATAL: tree not BASE"; exit 1; }; done
for f in $FILES; do cp -f "$f" "$f.bak"; done
# CRLF 树先洗行尾（Windows 同步产物），LF 树此步无害
for f in $FILES; do tr -d "\r" < "$f" > "$f.tmp" && mv "$f.tmp" "$f"; done
patch -p1 < "$ROOT/patches/<patch>.patch" > /dev/null || { echo "APPLY FAILED"; exit 1; }
# 后置校验：marker 必须出现（patch 真正生效）
for f in $FILES; do grep -q "<marker>" "$f" || { echo "MARKER MISSING"; exit 1; }; done
bash "$ROOT/build_lib.sh" <变体名>    # 快照语义：构建当前树状态，树本身由 trap 还原
```

要点：

- **marker 双向校验**：前置（树是 BASE）+ 后置（patch 生效），只做一侧拦不住
  "树已脏"和"patch 没打上"两类事故
- **trap 还原**：无论构建成败，树回到 BASE；构建产物的变体性由"构建当前
  树状态"的快照语义保证
- **patch 文件用 `git diff` 生成**：手工写 hunk 行号必错
- **脚本上传必须 LF**：Windows 侧用字节精确写文件（PowerShell 的
  `WriteAllLines` 强制 CRLF 会毒化 bash 脚本；用 `WriteAllText` 或专用工具）
- 变体目录独立（`install-<变体名>/`），不覆盖共享 install

## 2. 链接指纹验证（性能归因的前置门禁）

**事故模型**：cmake `find_package` 类工程（依赖靠调用者传
`CMAKE_PREFIX_PATH`）在漏传前缀时会**静默回退系统库**——构建照常成功、
测试照常跑通、数字全部无效。实测案例：曾据此得出"优化劣化 886%"的结论，
根因是 bench 链了系统库而非变体库。

**验证三件套（每个变体 bench 构建后必做）**：

1. `ldd <bench 二进制> | grep <库名>`：链接的库路径指向变体 install 目录
2. 二进制体积对比：变体间体积应与代码改动量相称（体积完全相同 = 可疑）
3. 生成物指纹：protoc/代码生成器类工具，对比生成文件 md5（生成器版本
   错则生成物必错）

**两类工程的行为差异**：

| 工程类型 | 链接对象由谁决定 | 风险点 |
|----------|------------------|--------|
| find_package 靠调用者传前缀 | `CMAKE_PREFIX_PATH` / `-D<_pkg>_DIR` | 漏传 → 静默回退系统库 |
| CMakeLists 自读环境变量（如 `$ENV{LIB_HOME}`） | 环境变量 | 变量名拼错 → 同样静默回退 |

**纪律**：链接指纹验证前置于一切性能结论；"构建成功"不构成任何证据。

## 3. 换目标库参数核对表（10 项）

从一个库换到另一个库时逐项重新确认（前 3 项最常翻车）：

| # | 项 | 需要重新确认什么 |
|---|---|---|
| 1 | 库帧在火焰图里的定位 | 命名空间模式（前缀帧/模板实例化帧/lambda 帧）；模板名含逗号空格的解析（见火焰图解析参考 §5-6） |
| 2 | 源码树 | 生产用的是 fork 还是官方版本？fork 自带哪些既有优化（先 diff 再判断） |
| 3 | 上游参照树 | 官方高版本源码（"上游已做什么"用 diff 回答） |
| 4 | 构建链 | 库的依赖与变体构建方式；header-heavy 库无独立 .a 可换（见 #9） |
| 5 | 生产传导模型 | 预编译库（.a 重链即传导）vs header-only 模板（内联固化在业务二进制，换库必须业务重编）——两类模型的验证对象完全不同 |
| 6 | 场景画像 | 先跑 Step 1 分析库在新图里的类别构成，不沿用上一个库的结论 |
| 7 | 语义判据 | 是否存在等价 DebugString 的全量比对；无则按对象类型设计行为断言（注意顺序无关性：map 类容器条目顺序未指定，按序比对连未改库的基线都过不了） |
| 8 | 微基形态 | IO 线程负载与 CPU 负载可能需要不同微基形态 |
| 9 | 内联边界 | header-only 库的自研优化改 header → 微基天然重编可测；生产传导评估要写清"需业务重编"前提 |
| 10 | 变体命名/目录 | 沿用 `<库前缀>base/opt` 惯例；patch 目标文件多为 header |

## 4. 环境坑速查

| 坑 | 正确姿势 |
|----|----------|
| SSH 后台命令挂住 | `nohup ... < /dev/null &`（stdin 不关会话不返回） |
| 远程 CRLF 树 | patch 前 `tr -d "\r"`；diff 断言加 `--strip-trailing-cr` |
| 脚本上传变 CRLF | Windows 侧字节精确写文件，禁按行写 |
| 静态库（.a）分析 | 先 `ar x` 提取成员再 objdump；对 .a 直接 grep 指令 = 0 假阴性 |
| folded 帧解析 | 按尾部 `' (\d+)$'` 分割（帧名含空格/模板参数） |
| 多轮 bench 轮间状态 | 轮间清理状态（缓存/临时文件），共享机器慢漂移用交替执行对冲 |
