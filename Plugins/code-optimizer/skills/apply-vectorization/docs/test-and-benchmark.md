# 测试与 benchmark

## 1. 当前口径

当前目录已经不再提交请求、响应和生成源码的快照。

现在的口径是：

- 用户或上游流程提供待优化源码；仓库内样例只服务内部回归
- request、response、generated 都运行时写到对应源码目录下的 `generate/`
- benchmark 脚本只消费当前 generate 目录中的候选结果

## 2. 静态检查

静态检查要覆盖：

- `SKILL.md`
- `README.md`
- `scripts/README.md`
- `docs/usage-guide.md`
- `docs/skill-interaction.md`
- `evals/evals.json`
- 共享头文件

重点确认：

- 不再出现旧 demo 目录或旧快照文件引用
- 面向项目能力说明的 README、SKILL 和 docs 不暴露内部回归路径
- 内部回归样例的 driver 通过共享头文件复用公共能力
- reduction 样例覆盖 sum、dot、prefix scan 拒绝和严格顺序浮点拒绝
- SME ZA 样例覆盖普通 streaming 路径和明确 outer-product 语义下的 ZA/tile 门控
- codegen_style 样例覆盖 C/C++ 标量默认 intrinsics、已有 `asm/__asm__` 默认 inline_asm、`.S/.s/.asm` 默认 assembly artifacts，以及 style 冲突拒绝
- replacement_kind 样例覆盖完整函数、函数体片段、循环行段和 assembly artifact 输出
- gather/scatter 样例覆盖 SVE 已证明索引安全的成功路径，以及未知索引/未知别名的拒绝路径
- SVE 通算样例覆盖压缩固定块 byte/bit 成功、LZ-style 变量长度/重叠 copy 拒绝、加密独立 block 成功、缺少 feature gate 或跨 block 依赖拒绝
- `references/arm_instruction_assets/` 必须存在并可查询 PMULL、AES、SM4、EOR、TBL 等代表指令

## 3. request 与 response 检查

### 3.1 request

`generate_vectorization_request.py` 必须同时支持：

- `--case` 模式
- 显式 `--source-file --target-function --start-line --end-line` 模式

并且继续校验：

- 字段完整性
- `target_arch`
- 路径存在性
- 循环行号合法性
- 可选 `semantic_contract` 的布尔字段和索引属性结构

### 3.2 response

`materialize_vectorization_result.py` 必须：

- 先验证 request
- 再验证 `vectorization_result`
- 拒绝 `success=false`
- 拒绝空 `vectorized_code`
- 根据 `codegen_style=auto` 推导实际形态
- 根据 `replacement_kind` 选择完整函数、函数体、循环行段或 translation unit 的物化粒度
- 在 summary 中保留 `application_mode`
- 支持 `artifacts`，并把 standalone `.S/.s/.asm` 物化到主输出同目录

## 4. benchmark 脚本

### 4.1 `benchmark_real_source.sh`

当前主入口：

```bash
bash scripts/benchmark_real_source.sh \
  --arch neon \
  --source-file <source-file> \
  --driver-file <driver-file> \
  --target-function <function-name> \
  --request-json <source-dir>/generate/<name>_neon_request.json \
  --response-json <source-dir>/generate/<name>_neon_response.json \
  --output-dir <source-dir>/generate
```

它会：

1. 做 preflight
2. 运行时生成 request JSON
3. 校验并物化 response JSON
4. 编译标量源码、候选源码、driver
5. 如果存在 assembly artifacts，把 `.S/.s/.asm` 编译为对象并参与链接
6. 跑统一 before/after benchmark

`--case <case-name>` 保留为内部回归入口；外部项目优先使用显式源码模式。

### 4.2 `benchmark_before_after.sh`

它会汇总 `generate/` 中已经存在的候选响应：

```bash
bash scripts/benchmark_before_after.sh --arch neon
```

外部源码汇总模式：

```bash
bash scripts/benchmark_before_after.sh \
  --arch neon \
  --generate-dir <source-dir>/generate \
  --source-file <source-file> \
  --driver-file <driver-file> \
  --target-function <function-name>
```

### 4.3 `benchmark_model_response.sh`

这个入口仍然保留，但现在只做：

- 消费候选 `response JSON`
- 调用 `benchmark_real_source.sh`
- 输出正确性和性能结果

### 4.4 `test_compile.sh --compile-only`

SVE/SME 在当前主机不可运行时，可用 compile-only 做语法和目标 flags 检查：

```bash
bash scripts/test_compile.sh \
  --arch sve \
  --compile-only \
  --source <candidate-sve-source>
```

compile-only 不读取运行时 preflight 的 ISA 可运行结论，因此只表示编译器接受头文件、intrinsics 和架构 flags；它不能替代真实 SVE 机器上的 correctness/perf benchmark。

不再做参考性能门槛比较。

## 5. smoke compile

`scripts/test_compile.sh [arch]` 现在分两层：

- 先对 scalar/driver 做 host-side smoke compile
- 如果 `generate/` 下已经有某个架构的运行时产物，再结合 preflight 对这些产物做该架构下的 smoke compile
- 同时编译 `*_generated.c` 和 `.S/.s/.asm` artifacts

示例：

```bash
bash scripts/test_compile.sh neon
```

## 6. 推荐的自动化检查

- 生成一个合法的 request JSON
- 生成一个带 `semantic_contract` 的合法 request JSON
- 生成一个合法的第二个 request JSON
- 用临时 response JSON 走通 `materialize_vectorization_result.py`
- 用 `replacement_kind=function_body|loop_body` 的 response JSON 走通物化
- 用带 `artifacts` 的 response JSON 走通 C wrapper + `.S` 物化
- 用 `query_arm_intrinsics.py validate-snippet --style inline_asm|assembly` 验证 style-aware 静态规则
- 在本机支持 `neon` 时，至少跑通一个 L1 case 和一个 L3 case 的 benchmark
- 在本机支持 `neon` 时，用显式外部源码模式跑通 `benchmark_real_source.sh` 和 `benchmark_before_after.sh --generate-dir`
- 对错误 response JSON 验证脚本会明确失败，不绕过 request 校验
