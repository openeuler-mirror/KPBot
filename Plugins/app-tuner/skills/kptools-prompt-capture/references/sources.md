# 来源与修改范围

代码来源为 `prompt_capture` 采集与分析实现，导入日期为 2026-11-16。`source_hashes.json` 保存导入前文件的 SHA-256。运行时不依赖原开发目录。

## 迁入内容

- 参考 proxy 的独立采集段及 writer，提取为 `capture_runtime.py`；未迁入负载均衡、调度、鉴权和 FastAPI 路由。
- `analyze/anatomy/pcap/report/session_view` 与报告 CSS/JS；`join_vllm_log/export_corpus/check_tokenizer/dump_requests`。
- 控制器改为文件控制；采集默认关闭，开启时必须指定时长；增加凭证头限制、文件权限设置、独立模块接口与报告正文开关，会话文件名改用哈希。
- 未迁入生产数据、演示会话、PGO 构建、回放器或上游 proxy 整体替换方案。

参考 README 中的 CPU 开销、压缩比例和模板支持情况仅适用于原测量环境。项目内测试使用 mock 后端和合成数据；实际 Python proxy 与 vLLM 部署仍需单独验证。

参考 `vllm_patch/README.md` 的请求 ID 补丁面向 rosinL/vllm 特定 commit，原说明注明未在真实 vLLM 进程运行。本技能不自动应用该补丁。

## 许可

维护者已确认对迁入的新增采集代码、分析工具与报告 CSS/JS 持有权利或具备相应授权，并允许按 KPBot 的 MulanPSL-2.0 公开分发。对应组件已登记在仓库的 `THIRD PARTY OPEN SOURCE SOFTWARE NOTICE` 中，许可证全文见根目录 `LICENSE`。

本次未整份引入 vllm-ascend 的业务 proxy，仅提取参考实现的新增采集部分。上述授权说明针对实际迁入文件，不改变所引用外部项目的许可证。
