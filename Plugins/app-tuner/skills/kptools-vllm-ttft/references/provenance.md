# 源码来源与许可

`payload/vllm-ascend-timing.json` 保存原始源码（`before`）和包含 TTFT 计时修改的源码（`after`），用于上下文匹配和离线校验。它包含以下 Apache-2.0 源码及其修改版本，原有版权声明保留。`scripts/gen_ttft_report.py` 保留其 Apache-2.0 文件头。

| 组件 | Payload baseline | 文件条目 |
|---|---|---:|
| [vLLM](https://github.com/vllm-project/vllm) | `6e448d0ea9bf3d88d898b65449ca6dc2aec170ac` | 16 |
| [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) | `0ce124446a0f59c431da76e31ffcc53489622513` | 7 |

[Apache-2.0 许可全文](LICENSE-Apache-2.0.txt)。Payload 的 `provenance` 还记录诊断源码和后续修改的哈希。导入 KPBot 时保留 payload 和分析脚本内容。Mooncake 使用服务已经安装的运行时。
