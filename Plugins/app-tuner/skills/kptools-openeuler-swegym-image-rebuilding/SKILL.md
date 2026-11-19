---
name: kptools-openeuler-swegym-image-rebuilding
description: 在 openEuler/aarch64 上重建 SWE-Gym 任务镜像（sweb.eval.arm64.*）。用于在新架构上重建 293 个任务镜像、任务环境因依赖漂移/ wheel 缺失/pip 网络失败而挂掉、或验收 gold=0 需要定位漂移源时。覆盖 era-pin 四层方法（git 时代锚点、freeze 清单、commit-date 窗口、A/B 边界）、build_images.py 管线、SCRIPT_FIXES 锚点修复史、本地 registry 与 gold=1/empty=0 验收口径。
---

# SWE-Gym 镜像重建（openEuler / aarch64 实战经验版）

在 aarch64 机器上从零重建 SWE-Gym 任务镜像的完整方法论。
来源：openEuler aarch64（鲲鹏）实战（2026-10~11），293 个镜像 → 291 个通过 gold 验收。

## 触发场景

- 在新架构/新机器上重建 `sweb.eval.*` 任务镜像
- 已有镜像在新环境跑不过验收（依赖漂移、wheel 缺失、pip 网络污染）
- 数据集基础镜像拉不动/版本对不上

## 三条铁律（违反任何一条必返工）

1. **不重建基础镜像**——conda 官方镜像按 `python=x.y.z` 精确版本拉取。
   2026-10 实测：conda 滚动更新导致 10 个实例环境漂移
   （python 3.12.15 的 `getaddresses` 行为变化、conda-forge py3.8.20 缺
   `tzset` 需回退 defaults 通道等）。基础镜像变了，后面全白干。
2. **Era-pin，绝不装 latest**——任务镜像的依赖必须锚定到"问题发生的那一刻"。
   `pip install` 不带版本号 = 随时间腐烂。
3. **验收唯一仲裁是 gold=1 / empty=0**——跑官方 FAIL_TO_PASS：全过=1，
   全不过=0，部分过=当 0 处理并追查（通常是 SKIPPED 计数 bug 或环境错）。
   构建成功 ≠ 验收通过。

## Era-pin 四层方法（按优先级降序）

| 层 | 方法 | 何时用 |
|---|---|---|
| ① | `git show <base_commit>:requirements*.txt` —— 任务自己的时代锚点 | **首选，最常被忽略**。repo 里就有当年真值 |
| ② | freeze 清单对比（环境固化文件：293 实例 + 65 环境） | ①不可得时；也是日常防漂移的比对基准 |
| ③ | commit-date / CI 时代表推算 | ①②都没有，按提交日期查 PyPI 版本窗口 |
| ④ | A/B 边界搜索 | 版本窗口太大时，二分试装直到 gold 通过 |

**第①层完整走法**（一个 commit 看全部依赖文件，别只盯 requirements）：

```bash
# 从数据集拿 base_commit（swegym_train_293.jsonl → metadata.instance.base_commit）
git show <base_commit>:requirements.txt
git show <base_commit>:setup.py          # install_requires / extras 藏在这
git show <base_commit>:environment.yml   # conda 型任务
git show <base_commit>:pyproject.toml    # poetry/pdm 型
# 远程不 clone 也可：git archive --remote=<repo_url> <base_commit> requirements.txt
```

repo 锚点给的也是"声明值"，仍可能缺间接依赖（见附录 A）——第①层是起点不是终点。

**工作流定位**：freeze 清单应作为构建的**输出**沉淀（构建时固化），而非每次重建
的依赖输入——否则 freeze 本身也会漂移。

## 构建管线要点

- 入口：`build_images.py`（数据集配套的构建脚本），
  `SCRIPT_FIXES` / `ENV_SCRIPT_FIXES` 承载全部时代锚点修复史
- 镜像源：`docker.m.daocloud.io` 镜像站（公网直连 docker.io 被墙时可用）
- 私有 registry：本机 5001 起 `registry:2`，产出 `sweb.eval.arm64.*`
- **沙箱出网必须保持关闭**做验收，否则 pip 会把"自足测试床"污染成
  "现场拉包测试床"，验收结果不可信
- `get_environment_yml_by_commit` 类远程取规格的操作要加超时/重试/降级
  （swegym-pkg 已有补丁版本）

## 已知坑速查表（实战翻案记录）

| 症状 | 锚定修复 |
|---|---|
| dask/getmoto/MONAI 等大面积 SKIPPED | log_parsers.py 的 `SKIPPED [n]` 解析 bug（swegym-pkg 补丁），不是环境问题 |
| pyarrow ABI 断裂 | `pyarrow==13.*`（25 与旧 pandas/dask 不兼容） |
| moto/boto3 交互 | `boto3==1.26.151` 地板版本 |
| py3.12 getaddresses 行为变化 | `python=3.12.4`（不是 3.12.15） |
| py3.8 缺 tzset | conda **defaults** 通道的 3.8.20（conda-forge 版缺失） |
| funcy API 断裂 | funcy 1.x（2.x 不兼容旧代码） |
| typing_extensions/distributed/fsspec | 逐版本 pin（4.12+ / 2026.8 / 按冻结清单） |
| itk 无 arm64 包 | 用官方 wheel `itk==5.4.3`（存在，勿手编译） |
| GDrive 权重拉取超时（MONAI-7548） | 网络不可达就是不可达，标注 rejected，别死磕 |

## 验收流程

```
1. 起沙箱（gold 口径：干净环境，出网关闭）
2. 注入官方 test_patch（FAIL_TO_PASS 清单）
3. pytest 跑官方测试
4. gold=1 → 入库；gold=0 → 走四层方法定位漂移源 → 修 SCRIPT_FIXES → 重验
```

## 跨架构注意（x86 重建时）

规格（era-pin 锚点、SCRIPT_FIXES 逻辑、验收口径）全部架构无关可直接复用；
基础镜像换 x86_64 变体；wheel 由 pip 按平台自动选择——aarch64 上踩过的
wheel 缺失问题在 x86 上大概率不存在（x86 wheel 生态更全），但 pin 版本
**不要放宽**，否则验收照样挂。

---

## 附录 A：案例——pyarrow ABI 断裂（getmoto__moto-57116）

- **症状**：环境构建成功但 gold=0，pandas/dask 层 pyarrow 断言挂
- **诊断**：requirements 有 pandas pin 但 pyarrow 未 pin → 装了最新 25.x，
  与旧 pandas 的 C 扩展 ABI 不兼容
- **锚定**：层① repo 锚点没写 pyarrow（当年被 pandas 间接拉入）→ 层③ 按
  base_commit 日期定窗口 → 层④ 试装 13.x → gold=1 收口
- **可泛化教训**：**间接依赖必须显式 pin**。C 扩展家族
  （pyarrow/numpy/pandas 三角）全部显式 pin 到发布窗口版本

## 附录 B：案例——基础镜像漂移两类形态

同批 SCRIPT_FIXES 复验时 10 个实例突然 gold=0 → 根因是 conda 基础镜像
滚动更新（"铁律 1"的立法现场）：

- **形态 A**：python 3.12.15 改了 `getaddresses` 行为 → 锚 `python=3.12.4`
- **形态 B**：conda-forge 的 3.8.20 缺 `tzset`，defaults 渠道同版本号是全的
  → 渠道指回 defaults。**同版本号 ≠ 同内容**

诊断信号：**成批失败**（单实例先疑 SCRIPT_FIXES，成批先疑基础层）且失败
与 python 标准库行为相关。SOP 要求构建时记录基础镜像 digest，漂移一眼可辨。
锚定粒度 = 精确小版本 + 明确渠道，必要锁 digest。

## 附录 C：案例——大面积 SKIPPED（判分问题，不是环境问题）

dask/getmoto/MONAI/dvc/pandas 多实例 gold=0，pytest 输出大量
`SKIPPED [n] <name>`。重装环境无效。真根因：swegym-pkg `log_parsers.py`
不认带计数的 SKIPPED 格式 → 测试名丢失 → `case not in sm → failed` 误判。

修复解析器后（环境零改动）57116/6585/7111/6913/10422/3576/8380/3727/
9193/6147 十个实例 gold 全部 0→1。

- 注意：`grading.py` 的 `case not in sm → failed` 是**故意的反截断守卫**，
  不要顺手"修复"它
- 可泛化流程：验收挂掉先分流"环境问题 vs 判分问题"——看 pytest 原始输出
  特征、拿健康实例日志喂解析器做对照

## 附录 D：freeze 沉淀纪律

每锚定一个实例，顺手 `pip freeze` 全量导出存
`build/env-specs/instance/<id>.txt`——本次构建的"事实快照"。下次任何机器
重建，先 diff freeze 再谈重建。四层方法的本质不是"找版本号"，是**缩短
"现在"与"问题发生时刻"的距离**。
