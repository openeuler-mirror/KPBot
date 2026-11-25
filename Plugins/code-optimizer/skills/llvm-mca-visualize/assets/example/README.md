# llvm-mca-visualize 示例

| 文件 | 说明 |
|---|---|
| `loop.s` | SAXPY 风格 NEON 循环体：`y[i] = a*x[i] + y[i]`，10 条指令（2× 展开）；**只有循环体、无分支** |
| `llvm-mca-visualize-demo-hip12-iter5.html` | 用 `loop.s` 在 hip12（CPU part `0xd06`）生成的默认输出；自包含离线 HTML，双击可看 |

## 复现

前提：打过 `IssueTraceView` 补丁、支持 `-issue-trace` 的 llvm-mca（构建见 `../../references/build-llvm-mca.md`（或 `../../SKILL.md` 的「安装与自检」））。

```bash
python3 <skill_dir>/scripts/mca_visualize.py \
  --asm <skill_dir>/assets/examples/loop.s \
  --mca-bin ~/llvm-project/build-mca/bin/llvm-mca
```

- 默认 `--iterations 30 --timeline-max-iterations 6`（稳态口径），在当前目录写出
  `llvm-mca-visualize-demo-hip12-iter5.html`。
- uarch 由 `/proc/cpuinfo` 自动检测；非 hip12 时默认文件名随 uarch 变化（可显式 `--uarch` 覆盖）。
- 想输出到别处加 `--out <file.html>`；只要 JSON 用 `--json`。

## 示例口径

- 目标：hip12 / `0xd06`；5 个迭代组（前 2 组默认折叠），每组 10 条 ISA 指令。
- 页面：Tab A 指令流水时序 + 逐周期执行占用；Tab B 端口资源热力图（真实 issue 数据）+ 逐资源端口压力条。
- 数据：逐指令逐迭代的真实 issue cycle/单元（`-issue-trace`），不做跨迭代平均；reserved 组会在成员行画虚线（本示例无 reserved）。
- 压力条：`pressure` 单位是 cycles/迭代；利用率 = `pressure / cycles_per_iteration`（本示例 12.3 cycles/iter，S1 3.00 ≈ 24%）。
- 局限：`loop.s` 只有循环体（MCA 不跟随控制流，故没有 `b.ne`）；整函数分析请用 `--source/--binary`（函数级近似）。
- 离线：HTML 无 CDN/外部资源（仅 SVG `xmlns`），可直接用浏览器打开。
