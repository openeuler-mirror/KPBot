# 连接与检索说明

## 配置

要求 Python 3.9+，仅使用标准库。

默认 MCP 地址：`http://121.41.201.129/mcp/c044bfb0-84ae-465d-9eb2-b932ffd82b13`。

**令牌获取：请联系知识库维护者 [chi-dx](https://gitcode.com/chi-dx) 申请访问令牌。** 安装技能不自动获得知识库权限，仓库不提供共享令牌。获得令牌后，在本地启动 Agent 的终端设置：

```bash
export WEKNORA_MCP_TOKEN='<向知识库维护者申请的令牌>'
```

PowerShell 使用 `$env:WEKNORA_MCP_TOKEN = '<向知识库维护者申请的令牌>'`。设置后从该终端启动 Agent，已运行的 Agent 需重启才能继承配置。

切换部署时可选设置 `WEKNORA_MCP_URL` 覆盖默认地址；未设置或为空时使用默认地址。不自动寻找其他端点。不要把真实令牌写进仓库、命令示例或回答。当前默认地址为 HTTP，会明文传输令牌；服务启用 HTTPS 后应更新默认地址。

无需更改 Claude Code / OpenCode 的 MCP 配置。安装 KPBot 后，本技能随安装脚本分发；Claude 插件市场也可安装 `kunpeng-knowledge` 插件。

## 协议与范围

脚本对每次命令完成 MCP initialize、initialized 通知和只读 tools/call，处理 JSON 或 SSE 响应及会话标识，默认超时 30 秒，可用 `--timeout` 修改。仅支持这三个工具，不提供任意工具调用入口，不自动重试或跟随重定向发送凭证。

| 命令 | 远端工具 | 参数 |
| --- | --- | --- |
| search | wiki_search | query、regex（默认 false）、limit（1–50）、可选 knowledge_base_ids |
| read | wiki_read_page | slug、可选 knowledge_base_id（同 slug 多库时指定） |
| index | wiki_index | knowledge_base_id、limit（1–200，按页面类型） |

响应保留服务的 content 和 structuredContent，以保留标题、来源、匹配片段及页面链接；`isError` 和 JSON-RPC error 均作为失败处理。服务当前仅搜索已生成的 Wiki，不能声称搜索了所有原始文档或进行了语义检索。

## 错误处理

- 缺少令牌：提示联系知识库维护者申请并在本地设置 `WEKNORA_MCP_TOKEN`，不要求把令牌粘贴到聊天。
- HTTP 401/403：检查部署方令牌及授权范围。
- 超时、连接失败或无效响应：报告服务不可用，不返回伪造答案。
- 同名页面：使用搜索结果中的知识库 ID 消除歧义。
- 无匹配：拆分短词、尝试中英文同义词；仍无依据则说明范围限制。

接口来源：2026-11-05 对维护者提供的 WeKnora 1.0.0 服务执行 initialize 和 tools/list 的实际返回。默认地址由维护者指定；令牌和具体知识库权限由服务端管理，其他部署应以其服务能力为准。

## 本地 Agent 验收

用 KPBot 的 `install.sh project opencode <隔离项目路径>` 安装完整技能，配置 OpenCode 模型（密钥使用环境变量），再设置 `OPENCODE_BIN`、`KPBot_TEST_PROJECT`、`MODEL` 和上述 MCP 变量，执行：

```bash
python3 cases/agent-wiki-query/run.py
KPBOT_TEST_SCENARIO=natural python3 cases/agent-wiki-query/run.py
KPBOT_TEST_SCENARIO=empty python3 cases/agent-wiki-query/run.py
```

`lookup` 检验显式调用，`natural` 检验不点名技能的知识查询，`empty` 检验未命中。运行器保存脱敏的发现结果、JSONL 工具轨迹、回答和 `result.json` 到用例的 `results/`（不入库）。自动断言检查真实工具输出及执行顺序；仍需人工核对来源、回答边界与内容适用性。Windows + Git Bash 运行仓库安装脚本时设置 `PYTHONUTF8=1`，避免中文元数据被系统默认编码误读。
