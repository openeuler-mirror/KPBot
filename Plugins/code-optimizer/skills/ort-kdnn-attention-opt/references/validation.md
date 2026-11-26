# 验证:正确性闭环清单

Phase 4 使用。探索性能可先记录并标注验证缺口；只有正确性通过后才能给出可交付的收益结论。按改动涉及的层选择验证，未使用 cache 的实现不要求 cache 测试。

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

先对目标未融合图验证特殊值行为；以下零输出/均分仅在参考契约一致时适用。参考实现必须覆盖的语义(与 kernel-design.md §7 逐条对齐):max-subtraction;全 -inf 行 → 输出全 0;
+inf 并列 → 均分;NaN 传播;无 mask;scale 缺省 = 1/sqrt(d)。

## 2. 按实现选择用例矩阵

| 类别 | 用例 |
|---|---|
| 布局 | rank-3 `[B,Sq,hidden]`;rank-4 `[B,Sq,H,d]`(Sq=1);奇数尾(Sk=17、H=3、d=7) |
| mask | 无;共享 `[1,1,1,Sk]`;per-batch `[B,1,1,Sk]`;全 -inf（按参考契约检查）;含 NaN;非法中间维(`[B,2,Sk]` 期望失败,`test.Run(kExpectFailure, "intermediate mask dimensions must be 1")`) |
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
- **默认关测试**:不设 env 时融合数必须为 0——这是案例默认策略的回归保护；新路径可采用 session 选项或独立参考构建，并测试实际启用策略。

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
   逐元素 `|a-e| > atol + rtol*|e|` 判失败,报 max_abs/max_rel/failures/nonfinite。功能 A/B 按 dtype/模型约定容差（float32 示例 **atol=rtol=1e-4**）,并把其他 KDNN 优化 gate 显式归零(只测被测变量)。
2. 自己写:同一输入喂"优化开"与"优化关"两个 session,diff 输出。优先使用同一二进制的两个独立进程或 session 配置区分；env 双 session 仅用于无并发的构造测试——这正要求门控是"session 创建期读"模式。

顺带用 `--optimized-model-path` 落盘优化后图,确认融合节点数量与预期一致(6 个 block → 6 个融合节点)。

## 6. 内存与并发

- **ASan**:改内存/workspace/kernel 后至少一次。KDNN 与测试程序必须同 sanitizer 配置;受限容器
  `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1` 先做越界/UAF,非受限环境补 leak。
- **运行时覆盖**：线程相关改动覆盖实际支持的运行时；同时支持 KDNN_CPU_RUNTIME=THREADPOOL 和 OMP 时两种构建都做聚焦测试
  (THREADPOOL 走 ThreadpoolIface 调度,OMP 走 KDNN 内部 Parallel)。
- **并发**:同一 primitive 实例 + 各线程独立 workspace 并发 Run(N 线程 × M 轮,断言全部对参考);
  外层多线程拆 batch(TryBatchParallelFor 路径)下重复 Run。
- **数值边界**:全 -inf、+inf ties、NaN 已在 §2;再加极端 scale(0、很大)比较非有限值分类是否与参考一致。

## 7. 运行方式

```bash
cmake --build "$ORT_BUILD_DIR" --target onnxruntime_test_all --parallel "$BUILD_JOBS"
env LD_LIBRARY_PATH="$ORT_BUILD_DIR:$KDNN_INSTALL_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$ORT_BUILD_DIR/onnxruntime_test_all" --gtest_filter='Kdnn*Test.*'
```

单算子正确性(若有独立 bench):`--mode C`(逐 mask 对比 double 参考,报 max_diff/rmse/fail%)和
`--mode S`(首次 JIT 并发构造 + 同实例独立 workspace 并发 Run)——这些是案例 bench 的模式，可接 CI 或用等价测试覆盖，不要求所有项目提供相同 CLI。
