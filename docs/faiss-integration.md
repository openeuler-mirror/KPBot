# Faiss 工作流集成

code-optimizer 新增 15 个 Faiss / 向量检索技能。入口 `kpbot-code-optimizer` 识别明确的 Faiss 请求，并将完整任务交给 `faiss-optimize` 的 CP0–CP7 工作流；Claude Code 与 OpenCode 使用相同的技能内容。

## 安装与调用

从当前 KPBot 检出安装到待优化项目：

```bash
bash /path/to/KPBot/install.sh project opencode /path/to/faiss
# 或使用 Claude Code
bash /path/to/KPBot/install.sh project claude /path/to/faiss
```

安装器复制完整技能目录，并将 `faiss-optimize/agents/*.md` 注册到目标工具的 agents 目录。安装器不会安装或升级编码工具本身。

在目标项目发起任务，例如：

> 使用 faiss-optimize 优化当前 Faiss 项目的 IVFPQ。测试程序和数据位于指定目录，运行产物保存到独立 run_dir；先确认当前源码、构建后端和允许使用的 CPU / 线程范围。

提供或从项目中确认源码、目标算法、性能目标、测试工具、数据 / 索引与资源限制。完整流程见 [faiss-optimize](../Plugins/code-optimizer/skills/faiss-optimize/SKILL.md) 和 [工作流](../Plugins/code-optimizer/skills/faiss-optimize/workflows/optimize-workflow.md)。
单独的 API 解释、源码调查或算法问题可按需使用相应技能。

## 实验与验证

- CP0–CP7 覆盖环境与范围确认、源码分析、构建和基线、采样、候选实现、正确性验证、性能复验与独立验收。
- analyzer、profiler、candidate-planner、implementer、reviewer 分工交接；主协调者统一维护实验状态。
- 按实际检出检测索引、后端、宏和 SIMD 能力，不假定所有 Faiss 版本提供相同扩展。sra_test 是可选适配器，可使用项目自己的测试程序。
- 冻结数据与查询参数，保留原始指标、交错 A/B 或 ABBA 样本、Recall 和结果一致性证据。候选默认关闭并保留回退路径；提交与推送按任务授权执行。
- `faiss-build/scripts/build_faiss.sh` 的 `--krl on|off` 是可选兼容开关；省略时不向 CMake 注入该参数。具体构建参数以目标项目支持情况为准。

## 安装回归

```bash
bash tests/test_install_platform.sh
```

该测试检查两种平台的技能安装、Faiss 角色注册与 OpenCode 路由。`faiss-optimize/evals/evals.json` 定义 21 个行为场景，它们是评估规格，不代表已经执行模型行为评测。
安装与结构验证通过，也不代表已经完成特定算法、数据集和硬件上的性能验收。

契约回归：

```bash
python3 -m unittest discover -s tests -p 'test_faiss_*contract*.py'
python3 -m unittest discover -s tests -p 'test_vector_report_contract.py'
```

安装 Python `jsonschema` 后还会核验生产状态与正式 schema 的一致性；状态和交接测试同时通过 `python -S` 覆盖缺少可选依赖的模式。用例包括非 Git 源码、空仓库、缺少 Git、SHA-1/SHA-256、非 UTF-8 系统区域设置、状态机与报告门禁。
