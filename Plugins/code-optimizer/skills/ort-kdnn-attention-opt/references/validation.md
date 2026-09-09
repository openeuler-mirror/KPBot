# 验证:正确性闭环清单

Phase 4 使用。原则:**性能数字在正确性闭环之前不出现在任何报告里**。分层执行,全部通过才算完。

## 1. 单元层:OpTester 直测融合算子

模式(`onnxruntime/test/optimizer/kdnn_attention_fusion_test.cc` 的成熟写法):

```cpp
OpTester test("KdnnFusedAttention", 1, kKdnnDomain);
test.AddAttribute<int64_t>("num_heads", num_heads);
test.AddAttribute<float>("scale", scale);          // 或省略测默认 scale
test.AddInput<float>("query", {batch, seq_q, hidden}, query_data);
...
test.AddInput<float>("mask", {1, 1, 1, seq_k}, mask_data);   // 或 AddOptionalInputEdge<float>()
test.AddOutput<float>("output", {batch, seq_q, hidden}, expected, false, 1e-4f, 1e-4f);
test.Run();
```

期望值来自手写**参考实现**(标量 double 风格,逐 (batch, query-row, head) 独立计算)。写参考实现的
已知陷阱(真实踩坑):**mask 的 batch 维广播必须显式处理**——共享 mask 时参考实现要按 mask_batch=1
索引,默认按 batch 索引会越界读,症状是"batch 0 对、batch 1 错、恒定偏差",极像 kernel bug。

参考实现必须覆盖的语义(与 kernel-design.md §7 逐条对齐):max-subtraction;全 -inf 行 → 输出全 0;
+inf 并列 → 均分;NaN 传播;无 mask;scale 缺省 = 1/sqrt(d)。

## 2. 必测用例矩阵

| 类别 | 用例 |
|---|---|
| 布局 | rank-3 `[B,Sq,hidden]`;rank-4 `[B,Sq,H,d]`(Sq=1);奇数尾(Sk=17、H=3、d=7) |
| mask | 无;共享 `[1,1,1,Sk]`;per-batch `[B,1,1,Sk]`;全 -inf(期望全 0 **且无非有限值**);含 NaN;非法中间维(`[B,2,Sk]` 期望失败,`test.Run(kExpectFailure, "intermediate mask dimensions must be 1")`) |
| scale | 显式;缺省(省属性);同 shape 不同 scale(见 §4) |
| 广播 | q/k/v/mask batch 的 1↔B 组合至少 3 种 |
| 重复 | 同一 OpTester `SetNumRunCalls(2)`(同 session 双 Run,覆盖 cache 命中路径) |
| 算法切换 | env 切算法/gemv 模式后同 shape 再跑(每档都要对参考) |

## 3. matcher 层:图结构测试

`TransformerTester(build_case, check_graph, TransformerLevel::Default, Level1, opset, atol, rtol)`:
builder 搭 tf2onnx 风格投影链,check 用 `CountOpsInGraph` 断言融合节点数。

- **正例**:标准模式融合成功 + 数值一致;带 Tile 的 tiled-mask 模式。
- **反例(每个拒绝理由一个测试)**:reversed Div;QK 输出共享;per-head mask;单头(num_heads=1);
  Sq>1 的 rank-4。
- matcher 测试也要 `ScopedEnvironmentVariables{{"ORT_KDNN_FUSE_ATTENTION","1"}}`(pass 是 env-gated)。
- **默认关测试**:不设 env 时融合数必须为 0——这是最重要的回归保护(基线永远可达)。

**Day-1 就搭合成用例套件**(消融实验的重要教训:no_scale 图的 scale 语义错误是在实现后期才被
"合成正例 + 数值 A/B"抓出来的,若套件先行会在第一天暴露)。用 python(onnx 包)生成合成模型:
4~21 个正例(标准/部分遮蔽 mask/无 mask/无 scale/Mul 变体/运行时 mask/奇数形状)+ 5~9 个反例,
落盘后用 `--optimized-model-path` + ONNX parser 精确计数融合节点(**禁止用 strings 估算**),
再跑开/关数值 A/B。这套件应先于 kernel 完成存在。

## 4. cache 碰撞回归(防"同 shape 异参数"串台)

一个 shape 连续构造多组 (scale, 算法, gemv) 组合,每组都对参考;`SetNumRunCalls(2)` 让第二跑命中
primitive cache。**cache key 漏字段(如 scale、算法、SVE VL)时只有这个测试能抓到**——普通的
单配置测试永远发现不了。

## 5. 端到端 A/B

两种等价手段:
1. benchmark harness 的对比模式(若已实现):双 session(门控 env 包住 session 构造),同输入,
   逐元素 `|a-e| > atol + rtol*|e|` 判失败,报 max_abs/max_rel/failures/nonfinite。功能 A/B 用
   **atol=rtol=1e-4**,并把其他 KDNN 优化 gate 显式归零(只测被测变量)。
2. 自己写:同一输入喂"优化开"与"优化关"两个 session,diff 输出。注意两 session 用**同一进程同一
   二进制**,靠 env 门控区分——这正要求门控是"session 创建期读"模式。

顺带用 `--optimized-model-path` 落盘优化后图,确认融合节点数量与预期一致(6 个 block → 6 个融合节点)。

## 6. 内存与并发

- **ASan**:改内存/workspace/kernel 后至少一次。KDNN 与测试程序必须同 sanitizer 配置;受限容器
  `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1` 先做越界/UAF,非受限环境补 leak。
- **双运行时**:线程相关改动要 KDNN_CPU_RUNTIME=THREADPOOL 和 OMP 两种构建都过聚焦测试
  (THREADPOOL 走 ThreadpoolIface 调度,OMP 走 KDNN 内部 Parallel)。
- **并发**:同一 primitive 实例 + 各线程独立 workspace 并发 Run(N 线程 × M 轮,断言全部对参考);
  外层多线程拆 batch(TryBatchParallelFor 路径)下重复 Run。
- **数值边界**:全 -inf、+inf ties、NaN 已在 §2;再加极端 scale(0、很大)确认不产生非有限值。

## 7. 运行方式

```bash
cmake --build kdnn/RelWithDebInfo --target onnxruntime_test_all --parallel 48
env LD_LIBRARY_PATH="$PWD/kdnn/RelWithDebInfo:$PWD/onnxruntime/core/kdnn/out/lib" \
  kdnn/RelWithDebInfo/onnxruntime_test_all --gtest_filter='Kdnn*Test.*'
```

单算子正确性(若有独立 bench):`--mode C`(逐 mask 对比 double 参考,报 max_diff/rmse/fail%)和
`--mode S`(首次 JIT 并发构造 + 同实例独立 workspace 并发 Run)——这两个模式不接 CI,改 kernel 后
**必须手动跑**。
