# 采集与版本适配

先确定采样发生在宿主机、容器还是 driver，记录 UTC 时间和节点身份（身份可由用户脱敏）。宿主机 CPU 总数不能代替容器可用核数；driver 的资源情况不能代表所有 worker。

## 最小证据

| 证据 | 如何取得 | 能说明什么 / 限制 |
|---|---|---|
| 代码与执行计划 | 读取用户代码；当前版本支持时 `ds.explain()` | 逻辑/物理阶段；不证明实际热点 |
| Dataset stats | 一次真实终结操作完成后读取算子统计：`measured = ds.materialize()`，再保存 `measured.stats()` 原文 | 各 operator 执行统计；文本格式会变化，不建立固定正则解释器。Ray 2.52 中原惰性 `ds.stats()` 在 materialize 后可能仍为空串，不能据此重跑 |
| 环境快照 | `inspect_environment.py` 在实际运行 Python 环境中执行 | 当前节点平台、包版本、affinity、cgroup 和共享内存；不是持续监控 |
| 实际执行资源 | 已连接 driver 中按需记录 `ray.cluster_resources()`、`ray.available_resources()` | Ray 逻辑调度资源；快照不等于物理利用率，禁止为采集隐式 `ray.init()` |
| 系统/worker 时间序列 | 复用已有 nmon、pidstat、容器监控、GPU 厂商工具 | 写明采样间隔、PID/节点、单位和 CPU 百分比口径；单 worker 不代表全体 |
| 调用栈或计数器 | 对已确认的 worker 按需使用 py-spy/perf | 记录采样窗口、工具版本与权限；缺权限时交付现有分析，不改全局安全配置 |

Ray Data 是惰性执行；`count()`、`materialize()`、迭代或 sink 都可能引入执行。需要同一轮算子统计时写 `measured = ds.materialize()`，随后读取 `measured.stats()`；不要对原惰性 `ds` 读 stats，也不要为取 stats 重跑。不要为取 stats 先跑一次 count 再把第二次当冷启动基线。区分构图、初始化、执行、输出落盘和清理的时间范围。

## 环境工具的解释

`inspect_environment.py` 读取包元数据，不加载 torch/Ray 或 GPU runtime。缺包返回 null；不能据此断定另一环境或远端 worker 没有该包。GPU 运行态需另采。

Linux affinity 来自当前进程。cgroup 路径通过 `/proc/self/cgroup` 和 `/proc/self/mountinfo` 定位，读取 v2 的当前组及挂载内祖先 CPU 配额，或 v1 cpu controller 的对应配额。可见范围内最小有限配额结合 affinity 得到 **CPU 上界**；不是保留核数，不保证独占，也无法看到 cgroup namespace 隐藏的祖先限制。cpuset/memory 原始值用于人工复核；无 quota 不代表没有资源竞争。

输出保留探测失败信息和空值。环境快照中的 `ray` 版本来自所用解释器；容器与宿主机分别采集时保留两个文件，不覆盖。缺失 mount/proc 文件时不能改读宿主根目录猜测结果。

## 来源

公开接口和指标解释见 [sources.md](sources.md)。目标版本不支持时保存错误/帮助信息，改用已有日志或相应版本文档；不自动切到私有接口。
