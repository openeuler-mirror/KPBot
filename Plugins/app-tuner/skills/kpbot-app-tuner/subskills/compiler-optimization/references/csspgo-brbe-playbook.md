# CSSPGO/BRBE 采样式 PGO Playbook（A+K 场景）

> BRBE = Branch Record Buffer Extension（ARM 硬件分支记录扩展，FEAT_BRBE）；CSSPGO = Context-Sensitive Sampling-based PGO。
> 数据来源：鲲鹏 CPU + 昇腾 NPU 单卡 vLLM 实测（`CSSPGO.md`，kpbot-dev-journal/a+k/bale/brbe/），5 轮 `--epochs 3` 均值。
> **定位：插桩 PGO 的低侵入备选路径，不是替代。** 生产默认仍是插桩 PGO2；仅当无法部署插桩版时选本路径。

## 何时选 BRBE/CSSPGO（决策矩阵）

| 维度 | 插桩 PGO（`lto_pgo`） | BRBE/CSSPGO（`css_pgo`） |
|---|---|---|
| torch 侧收益（实测） | gen→PGO2 = **+11.0%** | gen→CSS ≈ **+7.6%**（少 3~4%） |
| Python 侧收益 | 基准 | 噪声级（+0~0.4%），两者等价 |
| 全链总收益（vs v9） | +18.1% | +15.1% |
| 采集方式 | 部署插桩版 + `LLVM_PROFILE_FILE` + 优雅退出刷盘 | `perf record -p <pid>` 直接对运行进程采样 |
| 插桩运行开销 | 有（负载执行变慢，采集态失真） | 采样开销低 |
| 生产侵入 | 须把 instrumented build 装进目标环境跑一轮 | 可直接采生产进程（perf 只读） |
| 多进程采集坑 | vLLM v1 子进程环境重建丢 `LLVM_PROFILE_FILE`，须 sitecustomize.py 注入 + 全进程 SIGINT | 无环境变量依赖；但须选对进程（逐进程 `-p`，Worker 承担算子计算） |
| 采集二进制体积 | 正常（PGO2 libtorch_cpu 218MB） | gen 须完整 `-g`：torch whl 972MB / .so 2.9GB（未 strip，兼作 DWARF 来源） |
| 工具依赖 | llvm-profdata（毕昇自带） | llvm-profgen + BRBE 硬件 + 目标产物完整 DWARF |
| profile 覆盖 | 插桩全量精确计数（torch 实测 80,174 函数） | 采样覆盖低（libtorch_cpu 465/~37k ≈ 1.2%），"温函数"排序不准 → torch 侧收益低的根因 |
| profile 污染 | 多进程 `%m` 同文件互相覆盖 | NPU 自旋热点扭曲（`ReadQueue` 33× 第二名） |
| 流程段数 | 2 段（pgo1 插桩 / pgo2 使用） | 3 段（gen / collect / use） |

**选型规则**：

1. 默认与追求最大收益 → 插桩 PGO。CSSPGO.md 明确生产建议直接用 PGO2（与"Python BRBE + torch PGO 最优组合"性能并列，差 0.05% 噪声级，且无需维护 BRBE Python）。
2. 目标进程不能替换为插桩版（只能采现有生产进程）→ css_pgo。
3. 插桩多进程刷盘不可解（无法注入 `sitecustomize.py`）→ css_pgo。
4. 低侵入快速验证（编译器迁移 +6.4% 之后再低成本叠加 profile）→ css_pgo。
5. Python 侧单独评估 → 任选（等价）。
6. 磁盘敏感 / 无 llvm-profgen / CPU 无 BRBE → 插桩 PGO。

## 实测数据锚点（8 配置同窗口，全对齐 preload）

| # | 配置 | Python | torch/torch_npu | tok/s | vs v9 |
|---|---|---|---|---|---|
| 1 | v9 基线 | PGO | GCC 无 PGO | 154.19 | 基线 |
| 2 | gen（仅 BiSheng+LTO） | PGO | BiSheng 无 profile | 164.06 | +6.40% |
| 3 | **经典 PGO2** | PGO | torch 插桩 PGO | **182.12** | **+18.11%** |
| 4 | CSS-plain | PGO | torch BRBE | 176.49 | +14.46% |
| 5 | CSS-hotonly | PGO | torch BRBE(hot-only) | 177.49 | +15.11% |
| 6 | CSS-newplan | PGO | torch BRBE(new-plan) | 177.59 | +15.18% |
| 7 | 全 BRBE | BRBE | torch BRBE | 177.17 | +14.90% |
| 8 | 最优组合 | BRBE | torch 插桩 PGO | **182.21** | **+18.17%** |

- 收益分解：编译器迁移+LTO = +6.4%；torch 插桩 PGO = +11.0%（最大单一增量）；torch BRBE ≈ +7.6%。
- BRBE 三个采集变体（plain/hotonly/newplan）差异 <1%，采集端改进无实质收益，**不要在采集变体上花时间**。
- 前提：跨编译栈对比必须对齐运行时 preload（tcmalloc+stringlib），否则结论被带偏（CSSPGO.md 最重要的方法论教训）。

### 为什么 torch 侧 PGO > BRBE（根因）

| 维度 | Python 解释器 | libtorch_cpu.so | libtorch_npu.so |
|---|---|---|---|
| 热点集中度 | top1 ≈ 36%（`_PyEval_EvalFrameDefault`） | top1 ≈ 7.6%，分散数百函数 | `ReadQueue` 187M（33× 第二名） |
| 采样覆盖 | 775/4799 = 16.1% | 465/~37k ≈ 1.2% | 更低 |
| 热点性质 | 解释器核心原语 | dispatch/autograd/元数据 | 异步队列 + 巨型自旋 |

Python 是"小而沸腾的核"：采样命中主循环即可获得与插桩同等的热度信号 → BRBE ≈ PGO。
torch 是"温热散点 + 虚假自旋热点"：只有插桩全量精确计数才能正确排序温函数并做全局布局 → PGO 明显优（3~4%）。

## 三段流程（gen → collect → use）

统一入口 `scripts/ak_compile_optimize.sh --optimize css_pgo`：

```
gen     伪探针 + 完整 -g 构建（产物含 DWARF，装进目标环境跑真实负载）
        ak_compile_optimize.sh --component <c> --optimize css_pgo --stage gen [--source-dir <dir>]
collect 采样生成 sample profile，两种模式二选一（不需要源码/编译环境）：
        ① 附加模式：对运行中的 gen 版进程采样（只读）——长驻进程用
        ak_compile_optimize.sh --component <c> --optimize css_pgo --stage collect \
          --pid <pid> --binary <gen产物 .so/可执行文件> [--duration 90] [--page-size 65536]
        ② 离线模式：perf 包裹执行短生命周期命令（perf record -- <命令>，命令执行期间采样，
          命令结束即停止；--duration 不适用）——短命令必须用此模式，附加模式来不及命中热点
        ak_compile_optimize.sh --component <c> --optimize css_pgo --stage collect \
          --cmd "<命令>" --binary <gen产物 .so/可执行文件>
use     用 sample profile 重编译（去 -g，保留伪探针）
        ak_compile_optimize.sh --component <c> --optimize css_pgo --stage use [--source-dir <dir>]
```

profile 默认落盘 `<profile-dir>/<component>.spgo.profraw`（`--css-profile` 可覆盖）。组件编译顺序、ABI=1、前置依赖检查与 `lto_pgo` 完全一致（见 `ak-compiler-playbook.md`）。

### 各组件编译 flags

| 组件 | gen | use |
|---|---|---|
| PyTorch | `-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g` | 同左去 `-g`，加 `-fprofile-sample-use=<profraw>` |
| torch_npu | 外部 CXXFLAGS 注入：`-O3 -fGNU-compatibility -Wno-non-pod-varargs -D_GLIBCXX_USE_CXX11_ABI=1 -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g -flto=thin -fuse-ld=lld`，`ci/build.sh --enable_lto`（不带 `--enable_pgo`） | 同左去 `-g`，加 `-fprofile-sample-use` |
| Python | `./configure --with-lto CFLAGS="-O3 -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g" LDFLAGS="…-flto -fuse-ld=lld…"`（不加 `--enable-optimizations`） | CFLAGS/LDFLAGS 均加 `-fprofile-sample-use=<profraw>`，**LDFLAGS 也要带 `-fpseudo-probe-for-profiling`** |

通用增强（现场调优参考，非脚本默认）：`-fno-plt -mcpu=native -mno-outline-atomics -Wl,-zcommon-page-size=2097152 -Wl,-zmax-page-size=2097152 -Wl,--no-rosegment -Wl,-z,norelro`。

`-mcpu` 不写死型号，遵循编译器自身判定：脚本 `--mcpu auto`（默认）用 `-mcpu=native -###` 看编译器把本机解析成什么，**只有解析出具体型号才采用**；解析为 `generic` 就不添加。
实测（BiSheng 4.2，Kunpeng part 0xd06）：`native` 解析为 `generic`；512³ matmul 微基准 `-O3` 下 generic 616ms，hip09 718ms、hip10c 1205ms、hip11 820ms、tsv110 1792ms——**generic 最快，强行指定反而变慢**，所以"解析不到就不加"是正确默认。需要固定值时用 `--mcpu hip11` 显式覆盖，并自行 A/B。

## 关键坑（必读）

1. **共享库必须完整 `-g`**：llvm-profgen 靠 DWARF 枚举函数，`-gline-tables-only` 不够。空 profile 的最常见根因就是 gen 缺完整 `-g`。gen 产物 wheel 未 strip（torch whl 972MB / .so 2.9GB），本身兼作 DWARF 来源。
2. **use 阶段必须保留 `-fpseudo-probe-for-profiling`**：伪探针式 profile 需 SampleProfileProbePass 绑定，去掉会告警 `Pseudo-probe-based profile requires SampleProfileProbePass` 且 profile 被静默忽略。Python/LTO 场景 **LDFLAGS 也要带**（链接期同样要绑定）。
3. **`-page-size` 须与目标 OS 页大小一致**（现场 64K 页用 `-page-size=65536`）；部分 llvm-profgen 版本无该选项，此时不传（脚本的 `--page-size` 只在显式给出时传递）。
4. `Invalid instructions` 告警可忽略；`--perfdata` 输入为空时改用 `--perfscript`。
5. **采集必须用 gen 版二进制跑负载**：use 产物（`-fprofile-sample-use`）不再产出采样可用信息，profile 也不匹配；对非 gen 进程采样得到的 profile 无效。
6. **多进程选对进程**：vLLM v1 的 APIServer/EngineCore/Worker 中 Worker 承担算子计算，`-p` 须指向真正干活的进程。
7. **NPU 自旋热点扭曲**：libtorch_npu 的 `ReadQueue` 自旋可达第二名的 33×，纯采样 profile 被扭曲；采集变体改进无实质收益（见「实测数据锚点」）。
8. **Python 侧收益噪声级**：全 BRBE（Python BRBE + torch BRBE）与 CSS-newplan 持平；"Python BRBE + torch 插桩 PGO"与纯 PGO2 并列（+0.05%）。不要为 Python 单独维护 BRBE 版本。
9. torch_npu 的 clang 严格 const 检查、ABI=1、`new_qtensor` ThinLTO 折叠等坑与插桩 PGO 共通，见 `ak-compiler-playbook.md`。
10. 采样频率：**不要指定 `-F`（也不指定 `-m`）**，用 perf 默认采样频率与默认 mmap 页数，避免采样率/缓冲与节点能力不匹配导致 `lost events` 或额外开销；时长用 `-- sleep <秒>` 控制。

## 有效性门槛与验证

### 收益验证工具与目标节点 runbook

前置门控（只读，A/B 前必跑）：

```bash
bash scripts/brbe_env_check.sh [--container <name>]        # ready 才继续
# 检查项: 鲲鹏CPU / 昇腾NPU(/dev/davinci*) / CANN / 毕昇 / llvm-profgen / BRBE硬件(真实分支栈校验) / 磁盘≥10GB / 当前栈编译器身份
```

完整收益验证序列（quick 模式只做 1、precise 模式全做）：

```bash
# 1. 基线压测（当前二进制，5 轮均值）
bash scripts/brbe_ab_benchmark.sh --mode run --label baseline \
  --cmd "<压测命令>" --rounds 5 [--metric-regex 'tok/s']

# 2. css_pgo 三段（见上文「三段流程」）
bash scripts/ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage gen ...
bash scripts/ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage collect --pid <pid> --binary <gen产物>
bash scripts/ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage use ...

# 3. 候选压测 —— 压测命令与启动器必须与基线完全一致（含 LD_PRELOAD 分配器）
bash scripts/brbe_ab_benchmark.sh --mode run --label use \
  --cmd "<与基线完全一致的压测命令>" --rounds 5

# 4. 增益判定
bash scripts/brbe_ab_benchmark.sh --mode compare \
  --baseline /tmp/brbe_ab/baseline.json --candidate /tmp/brbe_ab/use.json [--min-gain 1.0]
```

判定规则（对应候选动作 `rejection_criteria` / `rollback`）：

| verdict | 条件 | 处置 |
|---|---|---|
| accept | gain > noise 且 ≥ min-gain（默认 1%） | 保留候选，进入二进制等价复核 |
| reject(gain<=noise) | \|gain\| ≤ noise | 差异不显著，拒绝归因 |
| reject(below_min_gain) | 有增益但 < min-gain | 不值得采纳 |
| regression | gain < -noise | 性能退化，执行 rollback 回基线 |

对照建议：同流程跑 `lto_pgo`（PGO2）做三向对比（baseline / css_pgo use / lto_pgo profile_use），验证本节点 BRBE 与插桩的差距是否符合 3~4% 量级。noise = √(cv_base² + cv_cand²)；A/B 未对齐 preload 的对比一律作废（CSSPGO.md 方法论教训）。

### profile 与产物门槛

- profile 必须有函数归属（脚本按文本 profile 顶层 `name:total:entry` 行数判定；0 个归属函数 → collect 失败 / use 告警。小二进制/小组件的有效 profile 可远小于 10KB，不按文件大小判定）。
- `--binary` 必须是进程实际 mmap 的文件名：传软链（如 `python3 -> python3.10`）时，`llvm-profgen` 会报 `No relevant mmap event is matched for python3` / `No samples in perf script` 并产出 0 字节 profile。脚本已自动 `readlink -f` + mmap 名交叉校验（`binary_mmap_mismatch`）。
- use 阶段若复用 gen 的源码树，必须 `make clean`，否则 `make` 跳过重编、profile 被静默忽略（脚本已内置，但仍推荐 gen/use 分树）。
- 函数数参考：Python 有效 profile 实测 775 采样函数（241KB）；torch 侧 465 采样函数即可生效，但覆盖仅 ~1.2% 是收益天花板（插桩 80,174 函数）。
- 采集负载必须与正式压测同形态（model/prompt 分布/batch size），源码变化超 ~10% 重采。
- 编译验证：`readelf -p .comment <so> | grep -i bisheng`；构建日志确认 `-fpseudo-probe-for-profiling`（gen/use）与 `-fprofile-sample-use`（use）；功能 smoke test（import + matmul/device_count）；性能 A/B 用同一启动器对齐 preload。
- 收益承诺：不得引用知识库案例数字，必须由当前 workload A/B 验证。

## 与 skill 主流程的集成

- 候选动作走 Candidate Action Contract，`action_type=css_pgo`，rollback 记录恢复基线二进制/配置/运行库（与 `pgo` 一致）。
- Agent 不得自动执行编译/采集，须向用户展示 `lto | lto_pgo | css_pgo` 清单与决策矩阵后由用户选择。
- 采集（collect）为只读操作，可对生产进程执行；gen/use 属于重编译动作，必须回主流程串行验证。
- 输出字段沿用 skill 输出契约（`profile_guided_candidates` 同时容纳插桩与采样两类候选，标注 `profile_source: instrumented|brbe_sampling`）。

## 手工命令参考（脚本不可用时）

```bash
# gen（PyTorch）
export CC=clang CXX=clang++
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g"
export CMAKE_CXX_FLAGS="$CMAKE_C_FLAGS"
python3 setup.py bdist_wheel && pip3 install dist/*.whl --force-reinstall --no-deps

# collect（对 gen 版进程）
perf record -g --call-graph fp -e cycles:u -j any,u -o perf.data -p <pid> -- sleep 90
perf script -F ip,brstack --show-mmap-event -i perf.data > perf.script
llvm-profgen --binary=<gen产物 .so> --perfscript=perf.script --output=<c>.spgo.profraw --format=text

# use（PyTorch）
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -fprofile-sample-use=<c>.spgo.profraw"
export CMAKE_CXX_FLAGS="$CMAKE_C_FLAGS"
python3 setup.py bdist_wheel && pip3 install dist/*.whl --force-reinstall --no-deps
```
