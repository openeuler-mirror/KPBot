# 平台接入：token、API 与 SSH

> create-repo 与 push 的平台差异速查。用户遇到认证问题时按本文件引导。

## 平台识别

`SYNC status` 的 `platform` 字段由代码仓 origin（或 wiki 仓地址）自动识别：

| platform | 匹配域名 |
| --- | --- |
| `gitcode` | gitcode.com、gitcode.csdn.net、atomgit.com |
| `gitee` | gitee.com |
| `github` | github.com |
| `generic` | 其他（自建 GitLab、gogs、本地 file:// 等） |

`generic` 平台没有 API 建仓能力，wiki 仓只能由用户手动创建后 link。

## Token 配置

`create-repo` 的 token 按以下顺序解析：`--token` 参数 > 环境变量。

| platform | 环境变量 | 获取方式 |
| --- | --- | --- |
| gitcode | `GITCODE_TOKEN`（或 `ATOMGIT_TOKEN`） | GitCode 个人设置 → 私人令牌，勾选创建/管理仓库所需 scope |
| gitee | `GITEE_TOKEN` | gitee.com → 设置 → 私人令牌，勾选 `projects` |
| github | `GITHUB_TOKEN`（或 `GH_TOKEN`） | GitHub → Settings → Developer settings → Fine-grained PAT，授予目标账号的 Contents 读写 |

引导话术要点：token 只需配置一次（shell profile 或当次会话 export）；**永远不要**把 token 粘贴进仓库文件或让 agent 存档。若用户不愿配置 token，直接走"手动建空仓 → link"分支，体验几乎无损。

## 建仓 API 差异

脚本内部行为（供排障理解，无需手调）：

| platform | 端点 | 认证 | 说明 |
| --- | --- | --- | --- |
| gitcode | `POST https://api.gitcode.com/api/v5/user/repos` | `PRIVATE-TOKEN` 头 | Gitee 风格路径 + GitLab 风格头；响应字段做防御性解析 |
| gitee | `POST https://gitee.com/api/v5/user/repos` | `access_token` 表单字段 | `private` 默认 true |
| github | `POST https://api.github.com/user/repos` | `Authorization: Bearer` | `private` 默认 true |

常见失败：

- **401/403**：token 无效或缺少建仓 scope → 重新签发 token。
- **422/400 already exists**：同名仓已存在 → 换名（`-wiki-2`）或直接 link 已有仓。
- **组织命名空间**：API 默认建个人仓；组织仓手动创建。

## SSH vs HTTPS

- **推送优先 SSH**：`git@gitcode.com:owner/repo-wiki.git` 形式，免 token、免交互。link 时若 SSH `git ls-remote` 探测失败但 HTTPS 可用，用 HTTPS 地址，后续 push 走 git 凭证助手（macOS Keychain / git credential manager）。
- **SSH 首次配置**（用户未配时）：`ssh-keygen -t ed25519` → 把 `~/.ssh/id_ed25519.pub` 内容添加到平台 SSH 公钥设置 → `ssh -T git@gitcode.com` 验证。
- **HTTPS 推送凭证**：首次 push 由 git 凭证助手弹窗/提示；CI 等无人值守场景用 SSH deploy key 或 credential store。

## openwiki CLI 的 provider 配置

openwiki CLI 需要自己的模型凭证（与托管平台 token 无关）：

- 交互式配置（一次性）：用户在自己终端运行 `openwiki --init`，向导里选 provider。
- 非交互/CI：环境变量 `OPENWIKI_PROVIDER` + 对应 key（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENWIKI_PROVIDER=openai-compatible` + `OPENAI_COMPATIBLE_BASE_URL` 等），详见 `openwiki --help` 与官方 README。
- 凭证失效：`openwiki auth <provider>` 重新走认证流程。
- ChatGPT 登录型 provider 的 token 有过期时间，过期后报 `token request failed (403)`，重新登录即可。
