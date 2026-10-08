# openwiki-sync

OpenWiki 代码仓文档自动生成与同步插件。

## 功能

- **初次**：为代码仓生成 OpenWiki，通过 API 在 GitCode/Gitee/GitHub 创建独立 wiki 仓（或关联已有仓），推送首个 wiki commit
- **关联**：`.openwiki-link.json`（仓内，团队共享）+ `~/.kpbot/openwiki-links.json`（全局，含同步历史）双份记录代码仓 ↔ wiki 仓映射
- **联动**：代码 commit 后自动 `openwiki --update` 增量更新 wiki，并在 wiki 仓生成可追溯的对应 commit（message 内引用代码 commit hash）
- **恢复**：新机器 / 新 clone / link 文件丢失时一键从 wiki 仓还原
- **查询**：把 wiki 作为代码仓的先验知识源，快速定位架构与模块说明

## 架构

```
代码仓                             独立 wiki 仓（唯一持久存储）
├── src/ …                         ├── index.md / quickstart.md / pages/…
├── .openwiki-link.json  ──映射──► ├── .claims/ .last-update.json …（openwiki 增量元数据）
├── openwiki/  (gitignore, 工作副本) └── .source-repo.json（反向关联）
└── .openwiki-sync/wiki/ (镜像缓存)

同步流：restore(拉齐) → openwiki --update(增量) → push(镜像+commit)
```

## 依赖

- git、python3（标准库即可）
- Node ≥ 22.22 + openwiki CLI（`npm install -g openwiki`），首次需配置模型 provider
- 建仓（可选）：`GITCODE_TOKEN` / `GITEE_TOKEN` / `GITHUB_TOKEN`，或手动建仓

## Skill

见 `skills/openwiki-sync/SKILL.md`。

## 测试

`test/` 下为本插件的确定性评估设施（mock openwiki CLI + 5 场景 fixture + 程序化评分），
不随 install.sh 进入用户安装。用法见 `test/README.md`。
