---
name: kptools-shell-format-quality-checker
description: 对 Linux 部署 Shell 脚本做只读质检。用户要求审查 .sh 文件、部署脚本交付前检查，或排查 CRLF、BOM、反斜杠续行、Shell 语法和引号问题时使用。可用于 vLLM-Ascend P/D、Proxy、Mooncake 启动脚本；不验证服务拓扑、参数语义或部署是否成功。
disallowed-tools: Edit Write NotebookEdit Agent PowerShell
---

# Shell 部署脚本质检

检查用户指定的文件或目录，不执行、`source` 或修改被检脚本，不启动服务或安装工具。Linux 为目标环境；优先在与部署一致的 Linux 环境做语法检查，Git Bash 不是前置条件。

## 权限边界

只用读取工具和下文给出的 checker 命令。不要调用编辑、写入、转交子代理等工具，不要用 Bash 直接运行或加载被检脚本，不要给 checker 命令附加重定向、管道或其他命令。用户另行要求修复时，结束本次只读质检，作为独立任务处理。

Claude Code 在本 skill 生效时按 frontmatter 移除编辑、写入、子代理和 PowerShell 工具；本 skill 不预授 Bash 权限，checker 仍走会话审批。`disallowed-tools` 是 Claude Code 扩展，不是通用 Agent Skills 字段，也不能依赖 OpenCode 执行同样的限制。需要同时禁止 Bash 时，按[受限会话说明](references/checks.md#强制只读会话)启动专用会话；不要把 skill 内的限制当成沙箱或跨回合保护。

## 步骤

1. 确认文件范围和实际调用方式。目录只递归检查 `.sh` 文件，不跟随符号链接；多个路径分别作为参数传入。
2. 用 Python 3 运行本 skill 的 `scripts/check_shell_quality.py`（路径相对本 `SKILL.md` 所在目录）。命令中的 `<技能目录>` 替换成实际目录，输入路径按调用环境加引号。
   受限会话没有 Bash 时，使用用户提供的 checker JSON，确认它对应同一批文件，再读取必要的上下文。没有 JSON 时请用户在会话外运行 checker，并将检查标为未完成；不要尝试绕过工具限制。


   ```bash
   python3 "<技能目录>/scripts/check_shell_quality.py" ./deploy
   python3 "<技能目录>/scripts/check_shell_quality.py" --shell bash ./launch.sh ./env.sh
   python3 "<技能目录>/scripts/check_shell_quality.py" --shell sh ./posix-entry.sh
   ```

   - 默认 `--shell auto`：从受支持的 `sh` / `bash` shebang 识别解释器，适用于直接执行的入口；缺失或无法识别时，不猜测。若确认为 `bash file.sh`、`sh file.sh` 或由已知解释器 `source` 的片段，按实际方式指定 `--shell bash|sh`。同一命令的显式选项适用于所有输入；混合调用方式分批检查。`--shell` 不验证直接执行时的 shebang。
   - 字节检查先于语法检查；遇到 CRLF、孤立 CR、UTF-8 BOM、无效 UTF-8 或 NUL 时，先报告根因，不将后续解析失败重复当作语法错误。
   - 语法检查使用本机对应解释器的 `-n`，不执行脚本；ShellCheck 可选，支持 `--skip-shellcheck`。ShellCheck 未安装、显式跳过或执行失败必须区分，不能用 grep 结果替代。Git Bash 的 `sh -n` 不能代表部署镜像的 `/bin/sh`。
3. 核对 `continuation-space` 的实际上下文。只有确认本应继续同一条命令，才记为 `error`；如果是注释、字面量或不发生续行的 heredoc 内容，不报续行错误。无法确认时列入未完成项，不得报通过。同一行不重复报告普通尾随空白。
4. 按下面的固定模板报告；不要转述脚本中的凭据。只提供修复建议，不实施修改。

## 固定报告格式

```markdown
结论：通过 / 不通过 / 未完成（必须且只能选择一种）
范围：<已检查文件数>；解释器：<实际使用的解释器或未验证>

| file | line | severity | check | evidence | fix |
| --- | --- | --- | --- | --- | --- |
| <文件> | <原始行号或 —> | <error / warning / info> | <检查项> | <脱敏证据> | <修复建议> |

跳过或未完成：
- <文件或检查范围>：<检查项> — <跳过 / 未完成>，<原因>
```

- 有必要检查未完成或有未复核的续行候选，结论为「未完成」，仍列出已确认的错误；否则有确认的 `error` 为「不通过」，其余为「通过」。没有结果的区块写「无」。
- 逐文件列出 ShellCheck 未安装、显式跳过和执行失败：前两者是可选项跳过，不等于 ShellCheck 通过；执行失败是未完成。被字节缺陷阻止的语法检查注明原因，不把同一个根因再次报为语法错误。
- CLI JSON 的 `files`、`findings` 和 `errors` 是证据，不是最终报告。退出码 `0` 表示自动检查完成且未发现阻断问题，`1` 表示存在自动确认的阻断问题，`2` 表示调用失败、检查未完成或有待复核的 `continuation-space`（优先于 `1`）。结合 `errors` 和 `findings` 判明原因。

误报判读、修复原则及 `-n` 的限制见 [质检参考](references/checks.md)。
