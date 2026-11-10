# KPBot 知识库接入

在运行 KPBot skill 的客户端配置 `kpknow`。以 OpenCode 为例，将以下条目合并到现有 `opencode.json`，保留其他配置。

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kpknow": {
      "type": "remote",
      "url": "https://kpknow.top/mcp-access/mcp",
      "enabled": true
    }
  }
}
```

从旧版迁移时，替换旧端点，移除旧的 `Authorization` 请求头和 `oauth: false`，再为新域名重新授权。证书校验失败时停止连接，确认使用的是上述 HTTPS 地址。

```bash
opencode mcp auth kpknow
opencode mcp list
```

在浏览器中使用自己的 GitCode 账号登录并授权只读访问。服务按 GitCode 仓库访问权限判断是否允许查询。OpenCode 保存和刷新授权凭证，授权失效时重新运行认证命令。看到 `kpknow connected` 后，可以提问“从知识库查一下 NUMA 绑核和内存绑定的区别，并给出来源”。

也可从 [KPKnow 网页入口](https://kpknow.top/) 使用 GitCode 登录。更多信息见 [服务接入页](https://kpknow.top/mcp-access/)。

| 工具 | 能力 |
| --- | --- |
| `kpknow_wiki_search` | 搜索已生成专题文章 |
| `kpknow_wiki_read_page` | 读取页面正文 |
| `kpknow_wiki_index` | 浏览知识目录 |

用户配置的模型负责组织回答。MCP 仅提供上述只读检索工具，后台资料生成独立运行。

## 本地 Agent 验收

在隔离项目安装 KPBot 的 OpenCode 技能，配置模型并完成上面的 OAuth 授权，再设置 `OPENCODE_BIN`、`KPBot_TEST_PROJECT`（使用绝对路径）和 `MODEL`。进入 `cases/agent-wiki-query/` 后执行 `python3 run.py`。可用 `KPBOT_TEST_SCENARIO=natural` 测试自然触发，或用 `KPBOT_TEST_SCENARIO=empty` 测试无命中。

入口和断言路径都相对于用例目录。每次运行将当前场景写入 `results/result.json`，由 Git 忽略。`lookup` 和 `natural` 检查搜索后读页，`empty` 检查零命中、未调用读页且回答明确说明无结果。三种场景均通过 `success` 字段报告各自断言的结果。
