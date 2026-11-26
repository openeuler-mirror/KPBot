# Ascend NPU + vLLM 线程绑核实战案例

本文件记录 Ascend910 + vLLM qwen2.5-1.5b 场景的绑核实战经验，作为 `SKILL.md` 中绑核策略的实证支撑。

## 案例 1：单卡 vLLM 线程级 NUMA 绑核

**场景**：Kunpeng 920 (640 核, 4 NUMA node) + Ascend 910, vLLM qwen2.5-1.5b 单卡推理

**初始状态**：
- vLLM 主进程 PID 485242（`python3.11`），775 个线程散布在 0-639 全核
- EngineCore 子进程 PID 485510（`VLLM::EngineCor`），825 个线程
- 三类关键线程混在同一 CPU 池，相互抢占：
  - `VLLM::EngineCor` 主线程：CPU 335 (NUMA 2)，7.6% CPU
  - `acl_thread`：CPU 325 (NUMA 2)，2.4% CPU
  - `release_thread`：CPU 262 (NUMA 1)，1.9% CPU

**关键发现**：
- vLLM AsyncEngine 模式下，EngineCore 运行在**子进程**中，不是主进程线程
- 主进程线程名全是 `python3.11`，无法区分角色
- 子进程线程名是 `VLLM::EngineCor` / `acl_thread` / `release_thread`，可精确识别

**NPU NUMA 拓扑问题**：
- `/sys/bus/pci/devices/*/numa_node` 对所有 Ascend NPU 返回 node 0（不可靠）
- 通过 `npu-smi info -m` 获取 chip→Phy-ID 映射
- chip 3 (ASCEND_RT_VISIBLE_DEVICES=3) 实际在 NUMA node 3
- 通过实测绑核验证：绑 NUMA 3 (480-503) 收益最高

**绑核方案**（NUMA node 3，CPU 480-503）：

| 线程角色 | CPU 范围 | 核数 |
|---|---|---|
| EngineCore (主线程+worker) | 480-487 | 8 |
| acl_thread | 488-495 | 8 |
| release_thread | 496-503 | 8 |

**实施命令**：

```bash
ec_pid=485510  # EngineCore 子进程

engine_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'VLLM::EngineCor' | awk '{print $1}')
acl_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'acl_thread' | awk '{print $1}')
release_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'release_thread' | awk '{print $1}')

for tid in $engine_tids; do taskset -pc 480-487 $tid; done
for tid in $acl_tids; do taskset -pc 488-495 $tid; done
for tid in $release_tids; do taskset -pc 496-503 $tid; done
```

**效果**：

| 阶段 | Total tok/s | Output tok/s | vs S0 |
|---|---|---|---|
| S0 基线 | 126.42 | 63.21 | — |
| C5 绑核后 | 136.15 | 68.08 | **+7.7%** |

（原优化流程中 C5 收益 +8.52%，本次复现 +7.7%，基本一致）

**教训**：
1. **必须先找子进程**：主进程线程名全是 python3.11，EngineCore/acl_thread/release_thread 在子进程中
2. **EngineCore 与 acl_thread 必须分核**：混绑会导致 ACL 通信抢占推理主线程
3. **release_thread 不能省略**：异步释放会阻塞下一轮推理
4. **三类线程必须在同一 NUMA node**：跨 node 会导致 HBM 控制路径加长
5. **Ascend NPU `/sys` NUMA node 不可靠**：需用 npu-smi + 实测绑核验证
6. **EngineCore 有 800+ 线程**：全部绑到 8 核，实际有效（低 CPU worker 不争抢）

---

## 案例 2：线程过提交削减

**场景**：同上环境，vLLM 默认创建 826 个线程

**初始状态**：
- 进程总线程数：826
- 实际使用 engine 线程：82
- OMP_NUM_THREADS 未设置 → 默认 640（等于 CPU 核数）
- 冗余线程：744

**调优动作**：

```bash
export OMP_NUM_THREADS=8
export MKL_NUM_THREADS=8
export OPENBLAS_NUM_THREADS=8
# 重启 vLLM
```

**效果**：
- 线程总数 826 → ~100，减少 744 个冗余线程
- 调度开销下降，EngineCore 线程 CPU 占用更稳定
- 配合线程级 NUMA 绑核，叠加收益

**教训**：
1. 线程过提交是 NPU 推理场景的隐性瓶颈，单看 CPU 利用率发现不了
2. 必须先削减冗余线程，再做细粒度绑核，否则绑核后仍被 idle 线程干扰
3. OMP_NUM_THREADS 应设为实际 engine 线程数，不是 CPU 核数

---

## 案例 3：多卡 TP 场景绑核

**适用条件**：vLLM / TGI / SGLang 以 tensor-parallel > 1 运行在多张 NPU 上。

### 拓扑约束

1. **TP worker 与 NPU 对齐**：每个 worker 进程绑在对应 NPU 所在 NUMA node
2. **HCCL 通信线程与网卡对齐**：HCCL 线程绑在 HCCL 网卡所在 NUMA node
3. **worker 之间 CPU 不重叠**：多 worker 共享 CPU 会引发跨 worker 抢核

### 多 worker 绑核模板

假设 4 卡 TP=4，NPU 分布在 NUMA node 0/1/2/3：

```bash
for rank in 0 1 2 3; do
  case $rank in
    0) worker_cpus="0-15";      worker_node=0 ;;
    1) worker_cpus="64-79";     worker_node=1 ;;
    2) worker_cpus="128-143";   worker_node=2 ;;
    3) worker_cpus="480-495";   worker_node=3 ;;
  esac

  worker_pid=$(pgrep -f "rank.*$rank" | head -1)
  taskset -pc $worker_cpus $worker_pid
done
```

### HCCL 通信线程绑核

```bash
hccl_nic_node=$(cat /sys/class/net/enp133s0/device/numa_node)
for worker_pid in $(pgrep -f 'vllm.*worker'); do
  for tid in $(ps -L -p $worker_pid -o tid,comm --no-headers | grep -iE 'hccl' | awk '{print $1}'); do
    taskset -pc <hccl_nic_node_cpus> $tid
  done
done
```

### 注意事项

- TP worker 进程数 = NPU 数，每个 worker 内部有自己的 EngineCore/acl_thread/hccl_thread，绑核需在每个 worker 进程内单独执行
- HCCL 网卡可能多张（每张 NPU 配一张 RoCE 网卡），需逐张对齐
- 容器场景：多 worker 用 `docker --cpuset-cpus` 隔离
- vLLM 的 RANK 环境变量可用于区分 worker：`VLLM_RANK=N` 或 `ASCEND_RT_VISIBLE_DEVICES=N`
