# GCC PGO 端到端验收

只需一个 `acceptance.mjs` 脚本。脚本在独立输出目录生成固定 C/C++ 小库、调用方和数据，不依赖下载、不使用用户业务源码、不安装工具，不修改全局配置。

## 执行

在用户选定的 Linux ARM64 开发节点准备 Node.js >= 20、CMake >= 3.24、Make、GCC/G++、同版本 gcov-tool/gcov-dump，以及 glibc、GNU 风格链接器和线程开发环境。工具从该节点 PATH 定位并交给实际执行器复核；不会自动切换或安装版本。

从 Skill 目录执行：

```bash
node tests/acceptance.mjs
# 或指定一个尚不存在的绝对输出目录
node tests/acceptance.mjs --output /absolute/new-pgo-acceptance-run
```

默认输出在 `tests/.acceptance-output/<时间戳>-<PID>`。不会覆盖或自动删除旧结果。每个场景的工具命令超时 120 秒、实验时限 1800 秒、构建并行度 2；这些仅属于固定开发用例，不是用户业务默认设置。运行本脚本表示选择这些固定测试条件。

## 验收矩阵

| 场景 | 预期 |
|---|---|
| missing-training | 未指定训练负载时 input_gate，实验目录未创建 |
| missing-authorization | 未明确 PGO 授权时 input_gate，实验目录未创建 |
| static / shared | 完成 init → baseline → instrument → train → check-profile → optimize → verify → compare |
| static-threads / shared-threads | 两个正常 join 的线程，完整流程完成 |
| empty-profile | 训练进程使用 `_Exit` 跳过计数刷盘；empty_profile，不能产生 P 候选 |
| wrong-shared-library | 训练调用方加载 other 库；shared_identity_unproven |
| data-changed | 初始化后改训练数据；dataset_changed |
| profile-tampered | 训练后注入无关 gcda；profile_changed |
| stage-order | 直接跳至 optimize；stage_order |

四个正向场景必须产生最终报告、profile 清单及完整的六组配对测量。调用方逐项比较库函数与参考实现，并检查完整工作量 checksum，不仅输出固定 correct:true。

故障场景必须命中指定错误码；遇到编译失败、工具缺失等其他错误不算“成功阻断”。各场景独立保存证据，一个失败不会掩盖其他结果。

## 结果解释

- 退出码 **0**：全部 11 项通过。
- 退出码 **1**：至少一项失败，查看 `summary.json` 中的错误与对应 experiment/logs。
- 退出码 **2**：环境不满足而跳过部分验收，或命令参数无效。不能报告验收通过。
- 输出目录已存在等启动错误也会非零退出，不覆盖现场。

`summary.json` 保存总体状态及每项结果，`results.json` 保存已执行进度，各场景保留源码、数据、输入契约及运行证据。若进程意外中断且没有最终 summary，不算验收完成。

`improved / no_gain / regressed / inconclusive` 均可作为正向流程的合法性能结果。本验收验证执行器闭环和拒绝机制，不要求小型 fixture 获得收益，更不能据此推导真实业务提升。

## 当前验证记录

2026-11-17，Windows x64：两项输入门控通过，九项 ARM64 场景跳过，总体 `incomplete`。另有九项 `scripts/self-test.mjs` 检查通过。尚未获得真实 Linux ARM64 验收结果；这份记录不是目标机认证。

本用例仍不替代真实业务数据代表性审查、模型会话行为评估或复杂项目兼容性测试。
