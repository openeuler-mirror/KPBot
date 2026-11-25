# cannlab 踩坑实录 — 症状 → 根因 → 解法

> 全部实测踩过（2026-11 pypto-serving 部署等实战积累），按类别分组。
> 【通用】= 任何任务都可能遇到；【特定栈】= 与具体软件栈相关，遇到同类
> 场景时检索。**发现新坑请更新本文**（每个新坑按同样格式追加）。

## 一、引号与命令传递（Windows → ssh → bash 两层解析）【通用】

### 1. 双引号被吞的连带伤害远不止参数错乱

- **症状**：远端行为诡异——grep 模式里的 `|` 被当管道、`(` 直接语法错误、
  含空格的模式被拆散、`python -c "..."` 的代码体消失
- **根因**：Windows ssh 客户端把远程命令中的双引号剥掉后重组命令行
- **解法**：远程命令值一律单引号；含 `|` `(` `$` 复杂内容的命令**必须**走
  脚本文件（本地写 → scp → bash 执行），不要内联

### 2. PowerShell 对双引号字符串做本地插值

- **症状**：`$(...)`、`$?`（变成 True）、`$HOME`（变成 Windows 路径）在
  本地被求值后才发出去
- **根因**：PowerShell 双引号字符串的插值语义
- **解法**：远程命令需要字面 `$` 时放进脚本文件，或外层用单引号

### 3. exec 偶发静默无输出

- **症状**：连 `echo MARKER` 都没有输出
- **根因**：引号问题或偶发通道失败
- **解法**：输出重定向到 NPU 上的日志文件再 `cat`；用成对 `echo A/B/C`
  标记定位断点

## 二、文件与脚本【通用】

### 4. 画蛇添足的 CRLF 处理事故

- **症状**：跑 `sed -i s/\r$//` 后所有行尾字母 r 被砍掉
  （`StackedDeviceTensor`→`StackedDeviceTenso`），爆出一串诡异 ImportError
- **根因**：sed 脚本未加引号，bash 把 `\r` 转义成字面 `r`，实际执行了
  `s/r$//`
- **解法**：本地工具写出的文件就是纯 LF，scp 到 Linux 直接可用，**根本
  不用转换**；真要转必须写成 `sed -i 's/\r$//'`（内层单引号）

## 三、Python 环境——CPU 与 NPU 是两套世界观

### 5. pyfix312 的适用边界（最容易翻车）【通用】

- **症状**：CPU 上 modelscope 等直接 core dump；NPU 上 pypto/torch/numpy
  导入全部 SIGSEGV（rc=139）
- **根因**：pyfix312 的适用边界弄反必炸——
  - CPU 容器的系统 python（3.12.x）**必须**把环境预置的 pyfix312 目录
    放进 PYTHONPATH：其 lib-dynload 有缺陷，会错误加载另一套构建版
    python 的 `_ctypes` 等 .so（ABI 不符）→ core dump
  - NPU 的构建版 python **绝不能**带 pyfix312：被 shadow 出段错误
- **解法**：同一条配方在两层语义相反，写脚本前先确认目标层（NPU 配方见
  ops-guide §7）

### 6. NPU pip 三连陷阱（构建场景）【通用】

- **症状**：`pip install` 报 "Requirement already satisfied" 却没真装 /
  sdist 装完没有可执行文件 / 镜像源不对
- **根因**：① `~/.local` 残缺包（实测有无二进制的 ninja），`--system-site-
  packages` venv 里永远"已满足"；② 实测环境里 pip 26.x 装 sdist 未产出
  data_files scripts——按实测行为规避（pip 官方 NEWS 未见对应移除
  记录，升级 pip 后可复测）；③ `-i` 镜像参数被环境里的 huaweicloud
  镜像配置盖掉
- **解法**：`--ignore-installed` 真装；`--only-binary :all:` 强制 wheel；
  以日志 "Looking in indexes" 行为准确认实际源

## 四、同步与文件生命周期【通用】

### 7. rsync --delete 的具体受害模式

- **症状**：NPU 上新建的目录（pip --target 依赖、构建产物、克隆的仓库、
  日志、缓存）在下一次**带同步**的 exec 里整目录消失
- **根因**：正向同步是破坏性的，CPU 没有同名路径 → NPU 侧被删
- **解法**：NPU 上每产生一个新目录/重要文件**立刻 pull 回 CPU**，然后才
  允许下一次同步 exec；NPU 上有任务在跑时不要做同步 exec（正在写的日志
  会被 unlink）

### 8. github 在 CPU 和 NPU 上都抖

- **症状**：clone 超时、GnuTLS 错误、连接重置
- **根因**：网络路径不稳定
- **解法**：submodule update / release 下载 / clone 都要 `for i in 1..N`
  重试循环；下载用 `curl -fL --retry 3 -C -` 断点续传

## 五、NPU Pod 系统层

### 9. /dev/shm 默认只有 64MB【通用】

- **症状**：大模型权重 staging 报
  `unable to allocate shared memory(shm): No space left on device`（29GB 级）
- **根因**：Docker 默认 shm 大小
- **解法**：`sudo -n mount -o remount,size=40G /dev/shm`（sudo 对 mount
  免密）；**Pod 重启后失效**，写进环境脚本每次设置

### 10. /usr/lib64/aicpu_kernels/ 不存在【通用】

- **症状**：依赖 AICPU 预装目录的框架（会往
  `/usr/lib64/aicpu_kernels/0/aicpu_kernels_device/<pid>_<devid>/` 写运行时
  SO）初始化失败
- **根因**：真机由驱动包创建，Pod 里缺失
- **解法**：`sudo -n mkdir -p` 建出后，把**写权限收敛给实际运行账号**
  （`sudo -n chown <运行账号> <目录>` 或按运行组建组 `chmod 2770`），
  并以运行身份验证"能创建、能加载"。**不要 chmod 777**——同 Pod 的
  其他账号将可改动目录项，运行时加载路径即被污染（同样写进环境脚本）

### 11. warmup 报 21008/507018/ERR99999 的两种根因要分清【特定栈】

- **症状**：首次 dispatch/大拓扑 warmup 失败
- **根因**（分清再动手）：
  - **硬件地板（无解）**：die 是 910B3 只有 **20 个 sync-start 块**，
    要求 >20 块同步启动的 kernel（如 24 块 SPMD 的模型）会**瞬间** fatal。
    确诊：设 `ASCEND_PROCESS_LOG_PATH` 后在 device-N/*.log 里找
    `require_sync_start block_num=N > limit=20`
  - **看门狗误杀**：大拓扑首 dispatch 建图预热慢，被 ~30s 看门狗杀
    （数十秒后才报）→ 设
    `SIMPLER_OP_EXECUTE_TIMEOUT_US=400000000 SIMPLER_STREAM_SYNC_TIMEOUT_MS=440000 SIMPLER_SCHEDULER_TIMEOUT_MS=320000`
- **解法**：先看设备日志确诊属于哪种；硬件地板要换代码并行度，看门狗才
  是调参数

### 12. CANN 内置 pypto 是老版【特定栈】

- **症状**：需要 `pypto.runtime` 子包等新版能力时 import 失败
- **根因**：CANN 内置的是扁平结构老版
- **解法**：源码构建装到独立目录（pip --target 或 venv），把该目录放
  PYTHONPATH **最前面**遮蔽 CANN 的；不要动 CANN 安装本身

### 13. Pod 系统缺 libsqlite3.so【通用】

- **症状**：任何 `import sqlite3` 的工具（实测 msprof 采集钩子）静默失败
- **根因**：系统目录没有 libsqlite3.so，环境预置目录（本环境的 pylib）
  中是唯一副本
- **解法**：把该预置目录加入 `LD_LIBRARY_PATH`（构建版 python 的
  sqlite3 模块靠它；预置目录路径见环境文档，不在本文硬编码）

### 14. msprof 与 PyPTO/simpler 派发路径不兼容【特定栈】

- **症状**：msprof 注入后应用在设备 dispatch 阶段原生崩溃（无 Python 栈、
  采集数据为空）；task-based/sample-based/仅 ACL 级 flag 全都一样
- **根因**：profiler 与该派发路径不兼容
- **解法**：要 timeline 用派发框架自带的 span 转 Chrome Trace
  （如 `[STRACE]` span → `strace_timing.parse_spans` 直接复用）

### 15. PowerShell 管道/传参的 GBK 编码坑（中文变 mojibake）【通用】

- **症状**：`git commit -m '中文...'` 或 `git log | Out-File` 后，中文
  变成 mojibake——如"分支"变成 `鍒嗘敮`、"规则"变成 `瑙勫垯`
  （UTF-8 字节被按 GBK 解读再重新编码）
- **根因**：Windows PowerShell 5.1 向原生命令传参按系统 ANSI（GBK）编码；
  管道读取原生命令输出同样按 GBK 解码——两次转换把 UTF-8 中文固化成
  合法但错误的字符，且**不可从外观一眼分辨**
- **解法**：含中文的内容**不经 PowerShell 管道**——写入 UTF-8 文件后用
  `git commit -F <file>`；读取用工具直接按字节处理。mojibake 可逆：
  按 GBK 编码回字节再按 UTF-8 解码即得原文（如 `鍒嗘敮` →
  bytes `e5 88 86 e6 94 af` → "分支"；双重 mojibake 做两次）
