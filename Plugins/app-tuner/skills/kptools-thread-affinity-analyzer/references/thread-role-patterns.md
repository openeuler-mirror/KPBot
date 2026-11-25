# 线程角色模式表 — comm → 角色映射

> 真源：`kpbot-app-tuner/subskills/cpu-affinity-optimization/SKILL.md`（PTA 热点分级模型
> 与 vLLM/torchrun 实名映射表）。本表是它在 `analyze.py` 中的落地形式，只做
> **标注**（报告按角色组织结论），不做绑核策略推导；策略与动作归
> `cpu-affinity-optimization`。

## 线程名（comm）是怎么来的

线程名即 `/proc/PID/task/<tid>/stat` 括号内的 comm，由线程自身通过
`pthread_setname_np` 设置，上限 15 字节（所以 `VLLM::EngineCore` 实际显示为
`VLLM::EngineCor`）。采样与角色匹配全部走 procfs，不需要 attach 目标进程。

## PTA 逻辑名与场景实名对照（真源摘录）

| PTA 逻辑名 | vLLM 推理场景实名 | torchrun 训练场景实名 | 职责 / 热点等级 |
|---|---|---|---|
| mainThread | `VLLM::EngineCor`（EngineCore 子进程主线程） | `python3.11` 主线程 | 前向算子下发，**第一热点** |
| npuGuardThread | —（推理无反向传播） | 暂无实名记录 | 反向算子下发，第二热点 |
| aclThread | `acl_thread` | `acl_thread` | ACL 二级流水（task queue）调度，第三热点 |
| releaseThread | `release_thread` | `release_thread` | 资源释放，非热点 |
| hcclCommWatchdogThread | `hccl` / `Hccl*` | `hccl_watchdog_t` / `Hccl_HeartBeat` | HCCL 通信监控/集合通信 |

补充（CANN 驱动/运行时线程，版本相关，不一定全部出现）：`DeviceManager`、
`devmmgr*`、`hcom_*`、`D2H`/`H2D` 拷贝线程。识别时应以实际 `ps -L` /
samples.jsonl 观测为准。

## 本 skill 的匹配规则（references/roles.json）

规则已外置为机器可读配置（`analyze.py` 启动时加载；`--roles` 可覆盖；
缺失/损坏回退内置最小集并 stderr 警告）。**新增框架 = 在 `rules` 里加一条，
不改代码**；参与 A2 设备对齐判定的 role 需同步列入 `npu_host_roles`。
匹配对 comm 小写化后进行；`tid == pid` 的主线程无条件标"进程主线程"并参与
NPU 对齐判定（torchrun 训练的第一热点就是它）。

当前生效规则（与下表一致，以 roles.json 为准）：

| 匹配方式 | 模式 | role_id | 含义 |
|---|---|---|---|
| exact | mainthread | pta_main | PTA 前向算子下发（第一热点） |
| exact | npuguardthread | pta_guard | PTA 反向算子下发（第二热点） |
| contains | acl_thread | acl_thread | ACL 二级流水/控制通路 |
| contains | release_thread | release | 异步资源释放 |
| prefix | hccl | hccl_comm | HCCL 集合通信/看门狗 |
| contains | hcom | hcom | HCCL hcom 通道 |
| prefix | vllm:: | vllm_ec | vLLM EngineCore 主线程 |
| exact | devicemanager | cann_dm | CANN 设备管理 |
| prefix | devmmgr | cann_dm | CANN 设备内存管理 |
| contains | d2h | d2h | Device→Host 拷贝 |
| contains | h2d | h2d | Host→Device 拷贝 |

另支持 `suffix`（后缀匹配）。rules 顺序即优先级（前 > 后）。

## 参与设备 NUMA 对齐判定（A2）的角色集合

`pta_main / pta_guard / acl_thread / release / hccl_comm / hcom / vllm_ec /
cann_dm / d2h / h2d / __main__`——即所有 host 侧算子下发、拷贝、通信、释放
线程。这些线程的主要落点 node 不在任一 NPU 设备所在 node 时报 A2（多 NPU
分布多 node 的场景，只要与其中一台同 node 就算对齐）。

## 纪律（同真源）

- 线程名为数字或纯框架名（如 `python3.11` 线程池 worker）时**禁止未经溯源
  直接套用 PTA 分级绑核取向**——本 skill 对此类线程只按观测数据呈现，不赋
  角色、不做推荐
- 角色表版本相关：以现场 `ps -L` 输出为准，未匹配线程在报告中如实显示
  "未匹配/未命名"
- vLLM AsyncEngine 的 EngineCore 在**子进程**中，线程识别必须先定位子进程
  PID 再采（`pgrep -f 'VLLM::EngineCore'`），主进程的 python 主线程不承担
  算子下发
