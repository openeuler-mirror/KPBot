# DSH 代理优化接入夹具

此夹具用于验证 DSH 技能加载、独立子代理、候选隔离、编译、正确性及本地基准证据链。它不是鲲鹏硬件验收，也不预设必须获得性能收益。`kernel.c` 是可修改目标，`bench.c` 是固定验收工具，不应由优化代理修改。

目标函数对不重叠的输入和 scratch 数组进行 uint32 模运算，返回 uint64 累加值；scratch 的所有结果均可观察。候选不得删除 scratch 写入或改变数值语义。

在单独测试目录中复制 `kernel.c` 后编译（`FIXTURE` 为本目录的绝对路径）：

```bash
gcc -O2 -std=c11 -Wall -Wextra -Werror kernel.c "$FIXTURE/bench.c" -o bench
./bench --test
./bench --bench
```

正确性覆盖空数组、NULL+零长度、边界长度、非向量宽度倍数、确定性伪随机输入、完整 scratch 内容、输入不变性及两端哨兵。基准参数固定为 2^20 元素、8 次预热和 128 次测量，输出正耗时 JSON。比较须冻结编译参数，采用交错多轮测量并报告噪声，不应以一次最快值认定收益。

也可使用固定的配对执行器，避免由代理临时生成 shell 包装脚本：

```bash
node "$FIXTURE/compare.mjs" /absolute/baseline/kernel.c /absolute/candidate/kernel.c /absolute/new-output-dir
```

执行器固定编译参数，先验证双方正确性，再做两组 ABBA，保存每条实际命令、退出码、原始输出和测量 JSON。输出目录必须尚不存在；命令失败/超时或正确性不通过就停止，不写成功测量报告。它只产生测量，不自动判定假设支持或替代业务验收门控。

模型验收应使用独立的 DSH_HOME、会话和工作目录，只借用已授权模型路由；不要把技能安装进正在开发的会话或写入真实 profile。凭据由运行环境提供，不写入测试文件。报告需区分：技能接入成功、局部正确性/性能和真实目标系统验收。失败或无收益也是有效结果，不得伪造支持性证据。
