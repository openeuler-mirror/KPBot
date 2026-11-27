# 编译与第三方优化

构建入口与依赖见 `oceanbase-build-deployment/references/build-and-deps.md`。
本文只讲“为什么/收益/风险/怎么验证”。

## 1. 编译器：开源 LLVM 11 → LLVM for openEuler

### 1.1 实测收益

| 平台 | 指标 | 收益 |
| --- | --- | --- |
| 920B | 五场景平均 | +21.81% |
| 950 | 五场景平均 | +11.25% |

收益来源（LLVM for openEuler 特性）：

| 特性 | 作用 | 920B 贡献 |
| --- | --- | --- |
| 鲲鹏架构优化 | 指令集/流水线适配 | 组合约 +12.2% |
| 函数内联阈值 | 更多内联 | 同上 |
| ICP 增强 | 间接调用转直接调用，增加内联机会 | 同上 |
| 自适应内存拷贝 | 识别源/目标特征，特化 memset/memmove | 同上 |
| 智能哈希预取 | 多层间接访存插入预取 | 同上 |
| 动态库快速访问（GOT） | 消除 PLT 跳转 | 同上 |
| 访存使能向量寄存器 | ldp/stp 使用向量寄存器 | 同上 |
| BOLT AArch64 | 基本块重排 | 约 +3.0%（内部构建实测；开源 CE 树须按 §3 额外开关） |
| CSPGO | 上下文敏感 PGO | 约 +2.5% |
| 冷热函数重排 | 热函数聚集 | 约 +2.3% |

风险：

- 客户可能不接受新编译器（维护/发布流程）。
- 必须整包重编（observer + obproxy + 依赖），不能只换一个二进制。
- 收益依赖 workload；OB 业务热点分散（最大约 4%），不要期待线性放大。
- 记录编译器版本、commit/包版本、全部编译参数。

### 1.2 验证

```
bash scripts/verify_build_opts.sh <observer_binary>
```

对比：同数据/同绑核/同口径，至少 3 轮；分场景列收益；确认功能正确性。

## 2. LSE / NOLSE

- 构建：`cmake/Env.cmake` 中 `OB_DISABLE_LSE=OFF` → `-march=armv8-a+crc+lse`；
`ON` → `-march=armv8-a+crc`（NOLSE）。
- 机制：LSE 是在代码里**内联单条**原子指令（ldadd/cas/swp 等，`[a-z]*` 覆盖
  casal/ldaddal 等内存序后缀）替代 LL/SC 循环；**outline atomics** 是 GCC 在
  `-march=armv8-a`（基线 ISA）下生成的 `__aarch64_*_relax` 运行时桩，属于回退实现，
  不是 LSE 机制，也不改写调用点的指令流。
- 实测：sysbench 锁争用不明显的场景，**NOLSE 比 LSE 高 2%~5%**；锁争用严重时 LSE 更好。
- 风险：CPU 必须支持 LSE（否则 SIGILL）；三方库/工具链需匹配；不能混用。

验证（**不能用“命中 ≥1”判定 LSE**）：`-march=armv8-a` 基线二进制里也可能出现 LSE 指令——
实测 GCC 12.3 `-march=armv8-a+crc` 编译的探针在旧版计数下 `matched_atomic_insn=1`，命中的是
outline-atomics 桩 `__aarch64_ldadd8_relax` 内部的真实 `ldadd`。判定要看**数量级**：
内联 LSE 构建通常是万级命中，桩残留只有个位数；计数前排除 `__aarch64_*_relax` 桩符号。

```bash
# 流式统计：按反汇编助记符列（AArch64 的 $3）匹配，排除 outline-atomics 桩
OBS=./observer                                            # 改成实际 observer 路径
objdump -d "$OBS" | awk '
  /<[^>]+>:/ { skip = ($0 ~ /__aarch64_[^>]*_relax/); next }
  !skip && $3 ~ /^(ldadd|ldclr|ldset|swp|cas)[a-z]*$/ {n++}
  END {print n+0}'
nm -C "$OBS" | grep -c '__aarch64_.*_relax' || true      # 桩存在≠LSE，不计入命中
readelf -p .comment "$OBS"                                # 结合 -march 构建记录
```

> 上面的助记符列统计仅适用 AArch64（x86-64 的 objdump `$3` 是十六进制字节）；
> 用 `scripts/verify_build_opts.sh` 时脚本会先探测架构并跳过不适用项。反汇编失败
> 必须按失败处理，不能把失败的 0 命中当成 NOLSE。

## 3. LTO / AutoFDO / HOTFUNC / BOLT / CSPGO

| 手段 | 构建开关 | 机制 | 预期 | 风险 |
| --- | --- | --- | --- | --- |
| Thin LTO | `ENABLE_THIN_LTO=ON` | 跨模块内联/去虚 | 小幅 | 链接时间/内存 |
| AutoFDO | `ENABLE_AUTO_FDO=ON` + `profile/observer-*.prof` | 采样反馈 | 前端下降 | profile 与业务不匹配会负收益 |
| HOTFUNC | `ENABLE_HOTFUNC=ON` + `profile/hotfuncs-*.txt` | 符号排序 | 1%~3% | 排序文件需匹配版本 |
| BOLT | `ENABLE_BOLT=ON`/`AUTO` + `-DOB_BUILD_OPENSOURCE=OFF` | 基本块重排 | 1%~3%（内部构建约 +3.0%） | 需 `--emit-relocs`，调试复杂；开源 CE 树须加前置开关 |
| CSPGO/PGO | 版本相关 | 插桩/采样反馈 | 前端收益 | 构建周期长 |

注意：

- **BOLT 前置开关**：`cmake/Env.cmake` 在 `OB_BUILD_OPENSOURCE=ON`（默认）时会把
  `BOLT_OPT`/`OB_ENABLE_BOLT` 门控掉（v4.2.5_CE_BP5 的 :46/:90，
  v4.3.5_CE_BP5_HF2 的 :47/:113）。在声明兼容的开源 CE 源码树上只传
  `ENABLE_BOLT=ON/AUTO` 是**空操作**，二进制与基线 md5 完全一致，A/B 只会得到假阴性。
  必须显式加 `-DOB_BUILD_OPENSOURCE=OFF`；切换后先用 md5 与基线对比，**md5 一致即说明
  BOLT 未生效**，不要把它记成“无收益”。表中 +3.0% 是内部构建实测，开源树需额外开关。
- `profile/hotfuncs-aarch64.txt` 的第一批符号是排序依据；业务变化后应重新采集。
- AutoFDO 的 `.prof` 是特定 workload 的采样；换 workload 可能负收益。
- 这些手段可与 LSE/NOLSE 组合，但必须逐项 A/B，不要一次全开。

## 4. `-mtune` 与指令级

| 参数 | 实测 | 结论 |
| --- | --- | --- |
| `-mtune=neoverse-n2` | Go1 类场景约 -2.5% | 不使用 |
| `-mtune=tsv110` | 全场景 -1%~-3.2% | 不使用 |
| third-party `-O3` | 无显著收益 | 不使用 |
| 手写 NEON（字符串/TSC） | 约 0.4%，噪声内 | 收益取决于热点占比 |

原则：先用 Topdown 确认前端/后端瓶颈，再决定是否值得指令级优化。

## 5. 第三方升级

- 全量升级（2021 → 2025）对读路径净贡献接近 0；写路径受 RocksDB 版本影响大。
- 风险：API/ABI、数据格式、编译周期、补丁适配。
- 结论：没有明确收益时不要做；必须做时先在隔离环境验证功能+性能+回滚。

## 6. 编译器优化的边界

实测显示：

- 编译器优化后前端 Bound 下降（920B/950 均有下降，幅度需按当次 topdown 采样填写），但后端 Bound 相对加剧。
- 业务热点多且分散，汇编层无显著单点热点，并行化空间有限。
- 内存加速库、数据预取、反馈式预取、内核反馈优化均无明显收益。

所以：编译器优化是“整体抬升”，不是“消除某个热点”；不要用它替代架构/线程/绑核优化。

## 7. 最小验证记录

```
compiler: <name/version/package>, target=<...>, -march=<...>, LSE=<on/off>
modes: LTO=<thin/none>, AutoFDO=<on/off> profile=<...>, HOTFUNC=<on/off>,
       BOLT=<on/off>, OB_BUILD_OPENSOURCE=<on/off>, PGO/CSPGO=<...>
binary: md5=<...>, size=<...>, inline_lse_insn=<...>（已排除 outline 桩）
A/B: scenario/rounds/tps/p99/cpu, verdict
```
