# perf 命令与采样记录

先确认事件权限、调试信息、调用栈和资源约束。以下是命令形状，事件与调用栈方案按目标系统可用能力选择。
```bash
numactl -C <cpu-list> -m <memory-node> -- \
  perf stat -e cycles,instructions,branches,branch-misses,cache-misses \
  <verified-benchmark-command>
numactl -C <cpu-list> -m <memory-node> -- \
  perf record -o <profile.data> -F 99 -g --call-graph fp -- \
  <verified-single-workload-command>
perf report -i <profile.data> --no-children --stdio
objdump -dC <actual-library-or-binary>
```
所有采集和查询进程遵守资源边界，检查内部 worker 绑定。frame pointer 不可用时选择可解释的替代方式，记录其开销和栈质量。
IPC、branch/cache miss 等计数用于提出假设，不能单独证明局部停顿原因。记录事件、频率、持续时间、样本/period 分母、丢失样本、self/inclusive 口径和未知符号范围。
将加载、构建、查询与结果统计区分；采样计时与正式性能对照分开保存。
