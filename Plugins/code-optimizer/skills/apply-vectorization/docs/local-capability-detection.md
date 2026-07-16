# 本地能力探测说明

## 1. 为什么现在拆成三段

旧链路里很多脚本都自己猜：

- 机器支不支持 `neon/sve/sme`
- 当前编译器能不能吃下对应 `-march`
- benchmark 到底是“不能跑”还是“脚本坏了”

现在统一拆成三段：

1. `detect_isa_features.sh`
2. `detect_compiler_support.py`
3. `preflight_benchmark_env.py`

后面的 benchmark 和 smoke compile 都直接消费这三者的输出。

当前实现会优先读取 macOS 内核的 `hw.optional.arm64` 标志，而不是只看 `uname -m` 或 `platform.machine()`；如果进程本身落在 x86_64 slice 下，编译器探测会自动切到 `arch -arm64`，避免把 Rosetta 环境误判成不可用。

## 2. `detect_isa_features.sh`

职责：

- 探测 `neon/sve/sme` 和常见扩展能力位
- 支持 `--list` / `--json`
- 支持 `--require`

示例：

```bash
bash scripts/detect_isa_features.sh --list
bash scripts/detect_isa_features.sh --json
bash scripts/detect_isa_features.sh --require neon
```

典型 JSON 字段：

- `os`
- `arch`
- `source`
- `native_arm64`
- `available`
- `capabilities.neon`
- `capabilities.sve`
- `capabilities.sme`

这里的 `native_arm64` 现在表示“机器是否支持原生 ARM64”，不是“当前 Python 进程是否跑在 arm64 slice 下”。

## 3. `detect_compiler_support.py`

职责：

- 检查 `cc/clang/gcc/aarch64-linux-gnu-gcc`
- 用真实头文件和真实编译参数探测 `neon/sve/sme`
- 给出推荐编译器和不可用原因

示例：

```bash
python3 scripts/detect_compiler_support.py --json
python3 scripts/detect_compiler_support.py --require neon
```

重点输出：

- `recommended_compiler`
- `architectures.<arch>.available`
- `architectures.<arch>.recommended_compiler`
- `architectures.<arch>.reason`

## 4. `preflight_benchmark_env.py`

职责：

- 汇总 ISA 与编译器探测结果
- 直接给 benchmark / smoke compile 提供统一结论

示例：

```bash
python3 scripts/preflight_benchmark_env.py --arch neon --json
python3 scripts/preflight_benchmark_env.py --arch neon --require-run
```

它会输出：

- `status = ready | skip | unsupported`
- `skip_reason`
- `compiler.path`
- `common_flags`
- `arch_flags`
- `baseline_disable_flags`

如果当前 Python 进程是 x86_64，但机器支持 ARM64，`preflight_benchmark_env.py` 仍会把环境判成可运行。

## 5. 退出语义

### `unsupported`

含义：

- 本机根本不报告该 ISA 能力

例如：

- 当前机器没有 `sve`
- 当前机器没有 `sme`

### `skip`

含义：

- 理论目标存在，但当前环境缺少本地可运行工具链
- 或 benchmark 不是在原生 `ARM64` 主机上执行
 - 需要注意的是，这里的“原生 ARM64”以硬件能力和内核标志为准，不再以 Python 进程架构为准

### 真实失败

含义：

- 脚本本身坏了
- response JSON 无法物化
- 编译报错不是预期的“环境不支持”
- checksum 校验失败

## 6. 对 benchmark 的影响

- `scripts/benchmark.sh` 会先跑 preflight
- `scripts/benchmark_real_source.sh` 会先跑 preflight
- `scripts/test_compile.sh` 也会先跑 preflight

所以当前目录里不会再出现“脚本先编译半天，最后才发现本机不支持”的行为。
