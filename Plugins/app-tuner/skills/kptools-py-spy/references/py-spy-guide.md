# kptools-py-spy 使用指南

## 1. py-spy 是什么

py-spy（github.com/benfred/py-spy，Rust）以 0 侵入方式采样 Python 进程调用栈——不注入代码、不改启动命令、目标进程无需重启。直接读取 CPython frame stack，火焰图带 Python 函数名与行号。

**与 perf/devkit hotspot 的区别**：perf/tuner hotspot 只能看到 native 帧（如 `_PyEval_EvalFrameDefault`），无法区分是哪段 Python 代码在烧 CPU。py-spy 是 Python 场景的函数级归因工具。

## 2. 三模式选择

| 模式 | 场景 | 产物 |
|------|------|------|
| `--record` | 常规采集，火焰图/原始数据 | SVG / raw / speedscope / chrometrace |
| `--top` | 人在终端实时观察（TTY）；agent 环境自动降级为 raw 统计排序 | 终端视图 / 帧统计文本 |
| `--dump` | 瞬时线程栈快照（ hung 死/卡住排查，等价 jstack） | 全线程 Python 栈文本 |

## 3. 开关语义矩阵（防混用）

| 语义 | py-spy | tuner hotspot | java-perf | 备注 |
|------|--------|---------------|-----------|------|
| 采样密度 | `-r` Hz（↑ 更密） | `-f` Hz（↑ 更密） | `-i` ms（↓ 更密） | java-perf 方向相反，勿混淆 |
| 火焰图开关 | 默认输出 | 需显式 `-g` | 默认输出 | |
| `-i` 参数 | `--idle`（含 idle 线程） | 子报告间隔（秒） | 采样间隔（毫秒） | 三处语义不同 |
| GIL | `-g` 仅持 GIL 线程 | — | — | |
| 子进程 | `-s`（torchrun 多 rank） | — | — | |

## 4. 场景配方

| 场景 | 命令 |
|------|------|
| 训练进程 Python 侧热点（data loader / 预处理 / Python 开销） | `collect.sh -p PID -d 30 -s` |
| 疑似 GIL 竞争（线程多、CPU 单核高） | `collect.sh -p PID -d 30 -g -t` |
| off-CPU 近似（线程多但 CPU 低：IO/锁等待） | `collect.sh -p PID -d 30 -i -t`（对照 `-g` 输出：差集≈等待中的线程） |
| 服务卡死/ hang | `collect.sh --dump -p PID`（连续多次间隔采样对比栈变化） |
| vLLM 推理服务 Python 侧 | `collect.sh -p PID -d 30 -s`（配合 kptools-ascend-profiling 看 NPU 侧） |

## 5. 火焰图解读要点

- SVG：宽度 = CPU 时间占比；从顶向下找"自己的代码帧"，库帧（torch/三方）向上聚合到调用它的业务帧
- raw 文本：`栈帧 (文件:行号) 采样数`，采样数近似 CPU 时间份额；`--format raw` 便于 grep/对比两轮采集
- `-g` vs 默认：默认含 idle 线程（看全貌），`-g` 只看真正在烧 GIL 的（定位竞争）
- `-i` 的代价：idle 线程栈会稀释火焰图宽度占比，off-CPU 分析时重点看"不在 -g 输出里的大栈"

## 6. 约束与局限

- ptrace：root，或同 uid + `ptrace_scope=0`；容器内需 SYS_PTRACE 能力
- 采样默认短暂暂停目标进程（每次 dump 栈 ~ms 级）；`--nonblocking` 可免暂停但有丢样
- `-n` Native：需 CPython 带 debug symbols；**torch_npu 下 C++ 扩展栈可见性待实测**（实测前输出仅保证 Python 帧正确）
- 不支持非 CPython 解释器（PyPy/Jython 不支持）
- CANN/torch_npu 侧瓶颈（算子执行、HCCL）py-spy 不可见 → 用 kptools-ascend-profiling

## 7. 证据留存

- 显式 `-o` 输出到 `./py-spy-evidence/<时间戳>/` 或调用方指定目录
- 对比分析（优化前后）建议固定 `-d` 与 `-r`，用 `--format raw` 做 diff
