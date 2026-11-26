---
name: variant-build-safety
description: >
  工作场景：构建变体库时的安全防线——链接指纹验证、备份/恢复/trap 模板、
  CRLF 纪律。适用于：编译多个变体库做 A/B 对照（base/patched/实验组），
  防止"链接了错误的库"导致假回归或假优化。覆盖：ldd 指向验证、二进制
  体积相称、protoc 生成物 md5 三重链接指纹；文件备份+trap 恢复+标记
  grep 安全模板；CRLF/LF 纪律。不适用：编译本身（构建系统的职责）；
  编译参数取证（用 binary-compile-forensics）。
---

# variant-build-safety — 变体构建安全层

cmake find_package 类工程漏传 `CMAKE_PREFIX_PATH` 会**静默回退系统库**——
构建照常成功、性能数字全废（实测案例：一次 "-886% 回归"的根因）。
构建变体库**必须在解读数字前做链接指纹验证**。

## 核心防线：三重链接指纹

| 检查 | 怎么验 | 拦截什么 |
|---|---|---|
| 1. ldd 指向 | `ldd binary \| grep "libfoo"` → 指向 install-variant 路径；动态链接形态另核对实际加载的共享库路径与构建标识（`readelf -n` 的 build-id） | 链了系统库 |
| 2. 比对产物按链接方式选择 | 静态链接：比可执行文件，体积与代码改动量相称（变小/相同须能解释，完全相同 = 可疑）；动态链接：比变体共享库的字节/构建标识——只重编 .so 时可执行文件可完全不变，要求它变大反而误判有效变体 | 构建实际没生效 |
| 3. 生成器指纹 | 用编译器路径、版本与生成记录（时间戳/日志）核验 protoc 是否重跑；.pb.h 摘要**仅在补丁预期改变头文件时**要求不同——仅改运行库或换用生成相同代码的 protoc 时，重跑后的 .pb.h 可逐字节相同 | protoc 没重跑 |

## 安全模板（基线校验 → 独立快照 → trap 还原 + marker 双向）

```bash
FILES="src/f1.cc src/f2.h"                  # 本次 patch 触及的文件
SNAP="backup-$(date +%Y%m%d%H%M%S)"         # 本轮快照独立目录（不与上轮 .bak 混）
# ① 基线校验先行（此时无 trap 挂着——发现树脏直接退出，不会用旧备份覆盖当前改动）
for f in $FILES; do grep -q "<marker>" "$f" && { echo "FATAL: tree not BASE"; exit 1; }; done
# ② 本轮快照到独立目录；快照完整（逐文件非空）后才注册恢复动作
for f in $FILES; do mkdir -p "$SNAP/$(dirname "$f")"; cp -f "$f" "$SNAP/$f"; done
for f in $FILES; do [ -s "$SNAP/$f" ] || { echo "FATAL: snapshot incomplete"; exit 1; }; done
trap 'for f in $FILES; do cp -f "$SNAP/$f" "$f"; done' EXIT   # per-file cp 还原
# ... 应用 patch、构建 ...
# ③ marker 双向校验：前置（上面 ①，树是 BASE）+ 后置（patch 真正生效）
for f in $FILES; do grep -q "<marker>" "$f" || { echo "MARKER MISSING"; exit 1; }; done
```

> `cp -a original/ original.bak/` + `mv original.bak/ original` 的整目录写法是
> 陷阱：mv 到已存在目录语义是把备份**移入**原目录（还原成 no-op 且污染树）。
> 完整模板与事故案例见 [build-safety-guide.md](references/build-safety-guide.md) §1。

## 和同类 skill 的分界

- `binary-compile-forensics`：产物编译参数取证——本 skill 是**构建过程**
  的安全防线（链接对了没），那个是**产物**的参数验证（编了什么参数）
- `alternating-ab-protocol`：测量协议——本 skill 是测量**之前**的构建
  安全层（防假数据进 A/B）

## 参考资料

- [build-safety-guide.md](references/build-safety-guide.md) — 完整防线
  原理、cmake 回退案例、CRLF 纪律
