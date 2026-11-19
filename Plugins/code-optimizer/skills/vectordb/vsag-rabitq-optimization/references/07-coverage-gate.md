# 阶段 7：覆盖率验收

对选定 base ref 的干净基线和优化分支执行相同的完整覆盖率采集流程，再计算贡献新增或修改的可执行行覆盖率。

## 采集规则

- 两侧使用相同构建参数、测试集合、过滤规则和覆盖率工具版本。
- 一致过滤系统头文件、build outputs、tests 和 external dependencies。
- 先报告 clean repository 与 branch 的整体覆盖率，再报告 changed executable lines。
- 不得降低仓库全局阈值来掩盖历史覆盖欠账。
- changed-line 覆盖率必须 `>= 90%`；未达到时补充有意义的测试，而不是排除生产代码。

既有 VSAG 树中的命令形态为：

```bash
COVERAGE_BASE_REF=<base-ref> bash scripts/coverage/check_cov.sh
```

base ref 必须来自本次阶段 1 审计，不得照抄历史 commit。阶段产出应包含 clean/branch 总行数与命中数、changed executable lines 命中数、百分比、未覆盖行和复现命令。门槛未通过时不得进入阶段 8。


