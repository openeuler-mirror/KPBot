# libco 栈式协程域

libco 及其衍生库的上下文切换、共享栈拷贝与验证。默认目标是 libco；其他栈式协程应用
只有在共享栈、用户态切换、AArch64 等特征匹配时才作为次级参考，不承诺通用优化。

## 成员

| Skill | 职责 |
|---|---|
| [`libco-aarch64-context-switch`](libco-aarch64-context-switch/SKILL.md) | libco AArch64 整数/SP 上下文切换、栈对齐与首次启动检查 |

成员表随串行提交更新；未出现的 skill 尚未提交。

## 目录约定

- 本域只有一个顶层目录：`skills/libco/`。
- 成员直接平铺为 `skills/libco/<skill>/SKILL.md`，不再嵌套包级 `skills/`。
- 域目录必须带本 `DOMAIN.md`，安装器据此识别并安装域内成员。
- 新增成员只在本文件登记，不修改全局路由表；根 README 由维护者统一更新。
- 共享资料放在 `references/`；每个成员自带 `references/` 副本以保证安装后可读；域级测试放在 `cases/`，不参与安装。

## 成员登记纪律

- 每个成员必须包含 `SKILL.md` 和 `evals/evals.json`。
- 描述只写“何时使用 + 作用”，实现细节放在正文。
- 域内成员之间不互相依赖；跨领域复用只作为次级参考。
- 修改成员时同步更新 `cases/skill-contract` 的检查范围。

## Review

- owners: []（待指派）
- 评审范围：`Plugins/code-optimizer/skills/libco/**`
