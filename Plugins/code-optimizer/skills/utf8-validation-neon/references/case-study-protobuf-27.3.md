# 案例：protobuf-27.3 UTF-8 校验 NEON 路由优化（case-study-protobuf-27.3）

> 完整落地记录。信息来源：本仓库目录下的实测产物（原始数据见 `references/data/`），采集时间 2026-11-19，protobuf-27.3 源码树 `third_party/utf8_range`（上游捆绑副本，MIT-style 许可）。

## 1. 背景

protobuf-27.3 捆绑的 `utf8_range.c`（Google range-sse 算法的移植封装）中 SIMD 加速路径仅以 `__SSE4_1__` 门控；upb 表驱动解析与 C++ 字符串字段校验在 AArch64 上全部落到 `utf8_range_ValidateUTF8Naive` 标量逐字节循环。同目录已存在上游 utf8_range 库的完整 NEON 实现（`range-neon.c`，16 字节分块查表验证 + `<16` 尾部走 `naive.c` 的 `utf8_naive`），但分发结构从未把它接入 protobuf 的构建。

## 2. 环境与方法学声明

| 项 | 值 |
|---|---|
| 机器 | AArch64（鲲鹏 920 / 0xd01），384 核，空闲负载（load ≈ 2.2） |
| 编译器 | gcc（ccache 包装），`-O3 -DNDEBUG`（与 protobuf Release 构建一致） |
| 被测对象 | `utf8_range_Validate(data, len, return_position)`（static inline，经同编译单元 `#include` 内联计时，与其在库内被内联进 Is`Valid`/ValidPrefix 的真实形态一致） |
| 计时 | 校准式：每 case 校准至 0.05s，5 样本取中位（`scripts/validate_bench.c`） |
| 进程 | 每版本 3 个独立进程，取中位；`taskset -c` 绑定同一空闲核 |
| 输入 | 尺寸 {16, 64, 256, 1024, 4096, 65536, 1048576} × {ascii, mixed_utf8（1/2/3/4 字节码点循环填充）, invalid_at_end（末字节 0xFF）} |
| 正确性 | 基准内建 correctness 断言 + fuzz 交叉验证（`scripts/checker.c`，973 组） |

## 3. 改造过程（含踩坑）

1. **第一次改写（失败）**：分发块写成 `#ifndef __aarch64__`，并引用了不存在的 `utf8_range_ValidateUTF8Native`。症状：基线构建产物无 NEON 符号；按极性修复后链接仍失败（undefined reference）。两条教训均写入 SKILL 红线。
2. **极性反转验证（反面证据）**：对 `#ifndef` 变体与基线分别编译，反汇编 `utf8_range_IsValid`/`utf8_range_ValidPrefix`，归一化后 **100% 一致**——极性写反时改动是指令级 no-op，基准偏差 ≤3.8%（噪声）。
3. **正式改写**：按 `router-patch-guide.md` 模板（`#ifdef __aarch64__`，rp=0 路由 `utf8_range()`，rp=1 回退 naive），链接 `range-neon.c` + `naive.c` 成功。
4. **正确性**：973 组 fuzz（空串/短 ASCII、全宽度码点、U+10FFFF/U+D7FF/U+0800 边界、overlong C0 80/C1 BF/E0 80 80/E0 9F BF、代理对 ED A0 80、>U+10FFFF F4 90 80 80、非法首字节 F5/FF/裸续字节、逐偏移截断、16n±1 块边界、中点损坏、全随机字节、1B~100KB）优化前后 **逐位一致**。

## 4. A/B 全量数据（Validate 直接计时，3 进程中位）

聚合数据 `references/data/aggregate_validate_ab.tsv`，每轮原始采样 `references/data/vorg_run{1,2,3}.csv`（基线）/ `references/data/vopt_run{1,2,3}.csv`（优化版）。

| API | 输入 | 尺寸 | 基线 ns | 优化 ns | 加速比 | 吞吐（基线→优化） |
|---|---|---|---|---|---|---|
| V(rp=0) | 混合 UTF-8 | 64B | 41.4 | 21.2 | 1.95x | — |
| V(rp=0) | 混合 UTF-8 | 256B | 165.3 | 50.9 | 3.25x | — |
| V(rp=0) | 混合 UTF-8 | 1KB | 636.9 | 187.4 | 3.40x | — |
| V(rp=0) | 混合 UTF-8 | 4KB | 2518.2 | 724.8 | 3.47x | — |
| V(rp=0) | 混合 UTF-8 | 64KB | 39969.5 | 11426.8 | 3.50x | — |
| V(rp=0) | 混合 UTF-8 | 1MB | 643512.9 | 183335.8 | 3.51x | 1.52 → 5.33 GiB/s |
| V(rp=0) | 尾部非法 | 64B~1MB | 41.1~649377 | 22.6~183590 | 1.82–3.54x | 1.50 → 5.32 GiB/s |
| V(rp=0) | 纯 ASCII | 16B~1MB | 4.2~55897 | 4.6~53109 | 0.92–1.05x | 18 GiB/s 不变 |
| V(rp=1) | 全部 | 16B~1MB | — | — | 0.97–1.03x | 不变（naive 回退，预期） |

API 级复测（`utf8_range_IsValid`，另一套 3 进程）与上表一致：1MB 混合 3.85x（1.47 → 5.66 GiB/s），ASCII ~1.0x，`utf8_range_ValidPrefix` ~1.0x。

## 5. 结论与局限

- **收益**：含多字节内容的 IsValid 校验加速 1.9x（64B）→ 3.5x（≥4KB 平台化），吞吐 1.5 → 5.3 GiB/s；纯 ASCII 与 ValidPrefix 无变化（预期，非回归）。
- **局限 1**：ValidPrefix（返回非法位置）未获加速——NEON 实现无位置语义（0=合法；非 0=非法：SIMD 块内错误返回 -1，尾部经 `utf8_naive` 返回 >0 的 1-based 错误下标）。要加速 rp=1 需实现带位置语义的 NEON 前缀定位（失败块内二分 + 块内标量回扫），属新算法，不在本 Skill 范围。
- **局限 2**：`utf8_range` 的 len 参数为 int，>2GB 输入需分段处理。
- **局限 3**：实测于鲲鹏 920；其他 AArch64 核预期量级一致（向量化/标量比主要由算法决定），但须目标机复测。
- **小输入**：≤16B 场景有 ≤10% 的轻微额外开销（新增 return_position 分支），实测 0.90–0.95x，属可接受噪声。

## 6. 信息来源

- 原始数据：`references/data/`（本仓库目录，采集于 2026-11-19）
- 被测源码：protobuf-27.3 `third_party/utf8_range/`（上游未改动副本 `utf8_range.c` + 上游 `range-neon.c`/`naive.c`）
- 验证脚本：`scripts/checker.c`、`scripts/validate_bench.c`（可直接复跑）
- 置信度：高（fuzz 逐位一致 + 两套独立 A/B 互证 + 反面变体反汇编证据）
