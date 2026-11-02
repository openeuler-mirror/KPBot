---
name: faiss-sra-test
description: 构建和运行 sra_test 的 Faiss 测试适配器，核对算法注册、配置映射、加载库及 QPS/Recall 口径。适用于已有 sra_test 工程的测试集成和故障定位。
---

# Faiss sra_test 适配

## 输入

sra_test 工程路径、Faiss 头文件与库、算法注册、配置目录、数据/索引路径和运行资源约束。

## 执行步骤

1. 检查当前工程的注册表、目标、参数解析和输出格式；不同版本的适配能力可不同。
2. 将配置映射到真实索引、包装器和 SearchParameters；核对编译条件是否改变字段解析或 API。
3. 构建匹配当前 Faiss 的测试程序，检查链接与实际加载库。
4. 按已声明的预热、查询、线程和绑核方案运行，保留完整配置、命令和日志。
5. 从当前计时与统计代码确认 QPS/Recall 公式，再进行比较或解析。

## 输出

可复现的构建/运行方式、有效参数映射、库身份、原始日志与指标解释。

## 约束

sra_test 是测试工具适配器，不限定 Faiss 必须使用它。算法不存在时先报告注册或 API 缺口；未知的编译开关、参数和指标不能从另一份工程复制。

## 参考资料

- [工程结构](references/sra-test-architecture.md)
- [构建与运行](references/sra-test-build-and-run.md)
- [算法注册](references/sra-test-algorithm-routing.md)
- [指标与输出](references/sra-test-metrics-and-output.md)
- [故障排查](references/sra-test-troubleshooting.md)
