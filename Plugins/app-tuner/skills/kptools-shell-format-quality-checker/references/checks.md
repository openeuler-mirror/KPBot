# Shell 质检参考

| 检查项 | 如何判读和修复 |
| --- | --- |
| `bom`、`encoding`、`nul` | 将文本另存为 UTF-8（无 BOM；ASCII 是 UTF-8 的子集）。有 NUL 或非法字节时先确认源文件编码，不要盲目删除字节。 |
| `crlf`、`bare-cr` | Linux 部署脚本用 LF；CRLF 转 LF，孤立 CR 按原意检查并修复。可在目标部署仓库的 `.gitattributes` 中添加 `*.sh text eol=lf`，防止 Git checkout 再引入 CRLF；该规则不处理 BOM 或续行空白。 |
| `continuation-space` | 检查前后两行：若 `vllm serve ...` 一行本应继续接下一行的 `--kv-transfer-config`，却以反斜杠加空格或 Tab 收尾，`bash -n` 可能仍通过。只删反斜杠后的空白，让反斜杠紧贴 LF；不要把注释、转义的反斜杠或字面内容改成续行。该项比单独看到行尾空白更值得先核对。 |
| `interpreter`、`syntax` | 直接执行入口看 shebang；经 `sh`/`bash` 执行或被加载的文件看实际调用者。工具默认仅自动识别无额外参数的 `#!/bin/bash`、`#!/bin/sh`、`#!/usr/bin/bash`、`#!/usr/bin/sh`、`#!/usr/bin/env bash`、`#!/usr/bin/env sh`。复杂 shebang/解释器选项需要按目标环境另行核对。解析器报错行可能是发现位置而不是缺失引号、括号或 `fi` 的位置。 |
| `shellcheck` | 按 SC 编号和真实上下文处理。`sh` 模式按 POSIX Shell 静态检查；不会执行或加载外部脚本。警告可涉及变量展开分词、通配、未赋值变量等；未装 ShellCheck 时可人工看关键命令的引用，明确标为人工复核。 |
| `trailing-whitespace`、`missing-final-newline`、`filename` | 普通格式警告。删除经过确认无语义的行尾空白，文件末尾补 LF；文件名有空白或没有 `.sh` 扩展名时按团队约定处理。heredoc 或引号中的空白可能是数据，不做批量清理。 |
| `mixed-indentation`、`long-line` | 仅提示，不阻断。Shell 启动参数、内嵌 JSON 及 heredoc 可以有意保留长行或 Tab（例如 `<<-`）。 |

`-n` 不验证参数值、外部命令、动态展开、被 `source` 文件、运行时 Shell 选项或目标容器中的依赖。要检查被加载的文件，明确列入输入。要核对 vLLM-Ascend P/D 节点环境差异，可另用 `kptools-ascend-pd-env`；本质检不替代部署验收。

本地回归用例：`python3 cases/shell-quality-contract/run.py`（从 skill 根目录运行）。

## 强制只读会话

使用 [Claude Code 权限配置](claude-read-only.settings.json)，而不是靠提示词禁止写入。配置使用 Claude Code 的 `permissions.allow` / `permissions.deny`，直接移除 Bash、PowerShell、写入、编辑、子代理和 MCP 工具。先从终端运行 checker，保留 stdout JSON（退出码 1/2 也有诊断结果），再把 JSON 与待检文件路径交给专用会话；不在这个会话里给 Bash 开例外。

```bash
claude --settings "<技能目录>/references/claude-read-only.settings.json" \
  --permission-mode dontAsk --tools "Read,Skill" --strict-mcp-config
```

如插件尚未安装，可用 `--plugin-dir "<app-tuner 目录>"` 加载。启动后用 `/permissions` 确认拒绝规则已加载。用带 `touch sentinel` 和错误续行的临时样本验收：外部 checker 不创建 `sentinel`；在 Claude 会话里尝试编辑、写入和直接运行样本，工具应不可用，文件应保持不变。此配置约束 Claude 工具调用，不是 OS 沙箱；不要在不可信的项目 hooks 或插件环境中把它当成进程隔离。
