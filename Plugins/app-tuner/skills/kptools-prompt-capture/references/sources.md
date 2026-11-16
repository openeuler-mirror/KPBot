# 来源与修改范围

代码来源为 OneStepAhead 中的 `vllm/tokenizer优化/prompt_capture`，导入日期为 2026-11-16。`source_hashes.json` 保存导入前文件的 SHA-256。运行时不依赖该目录。

## 迁入内容

- 参考 proxy 的独立采集段及 writer，提取为 `capture_runtime.py`；未迁入负载均衡、调度、鉴权和 FastAPI 路由。
- `analyze/anatomy/pcap/report/session_view` 与报告 CSS/JS；`join_vllm_log/export_corpus/check_tokenizer/dump_requests`。
- 控制器改为文件控制；采集默认关闭，开启时必须指定时长；增加凭证头限制、文件权限设置、独立模块接口与报告正文开关，会话文件名改用哈希。
- 未迁入生产数据、演示会话、PGO 构建、回放器或上游 proxy 整体替换方案。

参考 README 中的 CPU 开销、压缩比例和模板支持情况仅适用于原测量环境。项目内测试使用 mock 后端和合成数据；实际 Python proxy 与 vLLM 部署仍需单独验证。

参考 `vllm_patch/README.md` 的请求 ID 补丁面向 rosinL/vllm 特定 commit，原说明注明未在真实 vLLM 进程运行。本技能不自动应用该补丁。

## 许可

参考目录未提供覆盖新增采集代码和全部工具的独立许可证，其许可待确认。参考 proxy 声明业务代码来自 vllm-ascend，但该来源说明不涵盖新增采集代码的授权。

代码目前用于本地开发验证。公开发布或向第三方分发前，维护者需确认权利归属与许可，并补充仓库第三方声明；确认前不将其标注为 Apache、MIT 或 KPBot 的仓库许可证。
